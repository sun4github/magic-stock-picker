"""Enter a PAST pipeline verdict into the ledger, from what the run archived.

The verdict ledger began recording after these runs happened, so the analyses
that led to the earliest real purchases are absent from it. Everything needed to
reconstruct the row is still stored — `final_reports` holds the verdict and the
date it was reached — and the prices are still retrievable, so the ledger can be
told about them after the fact.

    python src/backfill_pipeline_verdicts.py --spec backfill.json
    python src/backfill_pipeline_verdicts.py --spec backfill.json --commit

`--spec` is a JSON array of {"ticker", "run_id"} pairs, naming exactly which
analysis to enter for each ticker. It is explicit on purpose: "the latest run"
is the wrong rule, because a ticker is often re-analysed after it was bought,
and a verdict reached later cannot be the one that caused the purchase. Choosing
the run is a judgement about causality and belongs to the person, not to a
MAX(created_at).

WHAT IS RECOVERABLE, AND WHAT IS NOT
------------------------------------
Recoverable, and written:
  * verdict and verdict_date  - `final_reports.verdict` / `created_at`
  * price at verdict time     - the ticker's own EOD close on the verdict date
  * benchmark baseline        - the index close on the same date
  * company/sector/industry   - FMP profile
  * analysis_key              - carried across from the report

NOT recoverable, and deliberately left NULL:
  * prompt_version and git_sha. The hash of the prompts AS THEY WERE on the day
    is gone. Stamping today's hash would be worse than leaving it empty: the
    column exists precisely so that "our judgement got worse" can be told apart
    from "we changed the question", and a wrong version defeats exactly that.
    NULL is honest, and it also marks these rows as backfilled.
  * market_cap and shares_outstanding. Available today, but today's values on a
    row where everything else is as-of the verdict date would silently mix two
    points in time.
  * magic_rank. `run_universe` was not being written when these runs happened.
  * verdict_basis. No archived report contains a basis block; these runs predate
    it. Reconstructing factors from the archived bear/bull cases is a separate,
    explicitly-labelled step - see load_reconstructed_basis.py.

WHY THE CLOSE, AND NOT THE INTRADAY PRICE
-----------------------------------------
The verdict was reached at some hour of the day that nothing recorded. The
day's close is a defensible, reproducible stand-in, and it is the same
convention the weekly snapshot job uses, so a return computed from it is
measured against a like baseline rather than a mix of intraday and close.
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta

import psycopg2
import requests
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.dirname(os.path.abspath(__file__))

LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(os.path.join(LOG_DIR, "cron.log"))],
)
logger = logging.getLogger("verdict_backfill")

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, "webapp", ".env"))

FMP_EOD_URL = "https://financialmodelingprep.com/stable/historical-price-eod/light"
FMP_PROFILE_URL = "https://financialmodelingprep.com/stable/profile"

LEDGER_VERDICTS = ("BUY", "WATCH", "AVOID")


def _config() -> dict:
    path = os.path.join(SRC_ROOT, "specs", "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("verdict_ledger", {}) or {}
    except (FileNotFoundError, yaml.YAMLError):
        logger.warning("Could not read config.yaml; benchmarking against SPY.")
        return {}


def fetch_eod_series(symbol: str, start: date, end: date) -> dict:
    """{date: close} for one symbol over a range, in a single call."""
    try:
        resp = requests.get(FMP_EOD_URL, params={
            "symbol": symbol, "from": start.isoformat(),
            "to": end.isoformat(), "apikey": os.getenv("FMP_API_KEY")}, timeout=30)
        resp.raise_for_status()
        rows = resp.json() or []
    except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
        logger.error("Historical prices for %s failed (%s).", symbol, e)
        return {}
    return {str(r["date"])[:10]: float(r["price"]) for r in rows
            if r.get("date") and isinstance(r.get("price"), (int, float)) and r["price"] > 0}


def close_on_or_before(series: dict, day: date, lookback: int = 10):
    """The close on `day`, else the most recent one before it (holidays, weekends)."""
    for back in range(lookback + 1):
        hit = series.get((day - timedelta(days=back)).isoformat())
        if hit is not None:
            return hit
    return None


def fetch_profile(ticker: str) -> dict:
    try:
        resp = requests.get(FMP_PROFILE_URL, params={
            "symbol": ticker, "apikey": os.getenv("FMP_API_KEY")}, timeout=20)
        resp.raise_for_status()
        rows = resp.json() or []
        if rows:
            return {"company_name": rows[0].get("companyName"),
                    "sector": rows[0].get("sector"),
                    "industry": rows[0].get("industry")}
    except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
        logger.warning("Profile for %s failed (%s); leaving it blank.", ticker, e)
    return {}


def load_spec(path: str) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    if not isinstance(spec, list) or not spec:
        raise SystemExit(f"{path} must be a non-empty JSON array of "
                         '{"ticker": ..., "run_id": ...} objects.')
    for entry in spec:
        if not entry.get("ticker") or not entry.get("run_id"):
            raise SystemExit(f"Every spec entry needs both ticker and run_id; got {entry}")
    return spec


def resolve(cur, spec: list) -> list:
    """Pull each named report, refusing anything that cannot be entered honestly."""
    resolved = []
    for entry in spec:
        ticker = entry["ticker"].strip().upper()
        run_id = entry["run_id"].strip()
        cur.execute(
            """SELECT verdict, created_at::date, analysis_key
                 FROM final_reports WHERE ticker = %s AND run_id = %s
                ORDER BY created_at DESC LIMIT 1""",
            (ticker, run_id))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"No final_report for {ticker} in run {run_id}.")
        verdict = (row[0] or "").strip().upper()
        if verdict not in LEDGER_VERDICTS:
            # Same rule as db_log_verdict: a wrong label is worse for a track
            # record than a missing row.
            logger.warning("%s in run %s has verdict %r, which is not "
                           "ledger-eligible; skipping.", ticker, run_id[:8], verdict)
            continue

        cur.execute("SELECT 1 FROM verdict_log WHERE run_id::text = %s AND ticker = %s",
                    (run_id, ticker))
        if cur.fetchone():
            logger.info("%s / %s is already in the ledger; skipping.", ticker, run_id[:8])
            continue

        resolved.append({"ticker": ticker, "run_id": run_id, "verdict": verdict,
                         "verdict_date": row[1], "analysis_key": row[2]})
    return resolved


def run(spec_path: str, commit: bool) -> dict:
    if not os.getenv("DATABASE_URL"):
        raise SystemExit("DATABASE_URL not set.")
    if not os.getenv("FMP_API_KEY"):
        raise SystemExit("FMP_API_KEY not set; prices could not be recovered.")

    cfg = _config()
    benchmark = str(cfg.get("benchmark_symbol", "SPY")).strip().upper()

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    rows = resolve(cur, load_spec(spec_path))
    if not rows:
        logger.info("Nothing to backfill.")
        cur.close(); conn.close()
        return {"written": 0}

    dates = [r["verdict_date"] for r in rows]
    bench_series = fetch_eod_series(benchmark, min(dates) - timedelta(days=10), max(dates))
    price_series = {t: fetch_eod_series(t, min(dates) - timedelta(days=10), max(dates))
                    for t in sorted({r["ticker"] for r in rows})}

    for r in rows:
        r["price"] = close_on_or_before(price_series.get(r["ticker"], {}), r["verdict_date"])
        r["benchmark_price"] = close_on_or_before(bench_series, r["verdict_date"])

    missing = [r for r in rows if not r["price"]]
    if missing:
        raise SystemExit(
            "No close recovered for: "
            + ", ".join(f"{r['ticker']} on {r['verdict_date']}" for r in missing)
            + "\n  Without a baseline price the row can never be graded, so nothing "
              "was written. Check the symbol and the date.")

    print(f"\n{len(rows)} verdict(s) to enter, benchmarked against {benchmark}:\n")
    for r in rows:
        print(f"    {r['verdict_date']}  {r['ticker']:<6} {r['verdict']:<6} "
              f"@ {r['price']:>9.2f}   {benchmark} {r['benchmark_price']:>8.2f}   "
              f"run {r['run_id'][:8]}")
    print("\n  prompt_version / git_sha stay NULL - the prompts as they were that "
          "day are not recoverable,\n  and today's hash would misattribute these to "
          "the current system.")

    if not commit:
        print("\nDRY RUN - nothing written. Re-run with --commit.")
        cur.close(); conn.close()
        return {"pending": len(rows), "written": 0}

    written = 0
    for r in rows:
        profile = fetch_profile(r["ticker"])
        cur.execute('''
            INSERT INTO verdict_log (
                run_id, ticker, company_name, verdict_date, verdict, verdict_stage,
                price, sector, industry, benchmark_symbol, benchmark_price,
                analysis_key, source
            ) VALUES (%s, %s, %s, %s, %s, 'INDEPENDENT', %s, %s, %s, %s, %s, %s,
                      'PIPELINE')
            RETURNING verdict_id
        ''', (r["run_id"], r["ticker"], profile.get("company_name"), r["verdict_date"],
              r["verdict"], r["price"], profile.get("sector"), profile.get("industry"),
              benchmark, r["benchmark_price"], r["analysis_key"]))
        r["verdict_id"] = cur.fetchone()[0]
        written += 1

    conn.commit()
    cur.close()
    conn.close()

    print()
    for r in rows:
        print(f"    {r['ticker']:<6} -> {r['verdict_id']}")
    return {"pending": len(rows), "written": written}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enter past pipeline verdicts into the ledger from archived reports.")
    parser.add_argument("--spec", required=True,
                        help='JSON array of {"ticker": ..., "run_id": ...}')
    parser.add_argument("--commit", action="store_true",
                        help="Actually write. Without it this is a dry run.")
    args = parser.parse_args()
    logger.info("Verdict backfill: %s", run(args.spec, args.commit))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("Verdict backfill failed with an unhandled exception.")
        sys.exit(1)
