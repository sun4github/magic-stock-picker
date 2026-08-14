"""Give externally-sourced purchases a place in the verdict ledger.

The pipeline writes its own verdicts as it reaches them. Purchases made on an
outside recommendation have no such moment — they exist only as lots in the
position tracker — so this script mirrors them into `verdict_log` as BUY rows
with `source = 'MANUAL'`, which is what lets the weekly snapshot job grade them
on exactly the same arithmetic as the pipeline's own picks.

    python src/sync_external_picks.py            # dry run, writes nothing
    python src/sync_external_picks.py --commit

Safe to run repeatedly, and MEANT to run before cron/run_outcome_snapshot.py on
the same schedule. Put both on one crontab line, in this order:

    0 18 * * 5 cd /path/to/magic-stock-picker && .venv/bin/python \
        src/sync_external_picks.py --commit && \
        .venv/bin/python src/cron/run_outcome_snapshot.py

Order matters. The sync is what tells the snapshot job which subjects exist and
which have finished, so running the snapshot first means a week's delay on both.

TWO THINGS IT DOES, AND WHY BOTH ARE NEEDED
-------------------------------------------
1. Mirrors purchases that have no ledger row yet.
2. SETTLES rows whose position has since been sold.

The second is not a variation of the first. A purchase is mirrored when it is
made; the sale happens later, by which point a verdict_log row already exists
and step 1 skips it forever. Without step 2 a sold position would be re-priced
every week as though still held, its return drifting with a market it had left,
and the settled result silently replaced by a hypothetical one — because every
query here reads the LATEST snapshot. Marks taken after the sale date are
deleted for the same reason.

So: record purchases and sales in the tracker whenever you like, and let this
run on its schedule. Nothing has to be run by hand at the moment of a trade.

WHAT A MIRRORED ROW MEANS
-------------------------
`verdict_date` is the PURCHASE date and `price` is the FILL price, not a
recommendation date and not the price when the recommendation was published.
That is a deliberate choice about which question gets answered: this grades the
holder's money, including whatever was lost between reading the idea and acting
on it. Grading the source's judgement instead would need the recommendation
date, which the position tracker does not know.

`prompt_version` and `git_sha` are NULL because no prompt produced these, and
there are no `verdict_basis` rows because no factors were articulated. Both are
expected. Nothing here is inferred by a model — the whole ledger grades by
arithmetic, and a mirrored purchase is arithmetic all the way down.

WHY THE BACKFILL IS WORTH DOING AT ONCE
---------------------------------------
`total_return_pct` is computed from the baseline stored on the row rather than
accumulated from snapshots, and the benchmark close on any past date is still
retrievable. So entering years of past purchases yields correct total AND
excess return immediately, instead of a year from now.

DEPENDENCIES, AND WHY THEY ARE THIN
-----------------------------------
Same reasoning as cron/run_outcome_snapshot.py: this talks to PostgreSQL and FMP
directly rather than importing mcp_server, so it can run on the Raspberry Pi
without pandas, google-adk or the Gemini SDK. Its whole dependency set is
`requests`, `psycopg2-binary`, `python-dotenv` and `pyyaml`. Please do not
"fix" the small amount of duplication by importing the rest of the repo.
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
logger = logging.getLogger("external_sync")

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, "webapp", ".env"))

FMP_EOD_URL = "https://financialmodelingprep.com/stable/historical-price-eod/light"
FMP_PROFILE_URL = "https://financialmodelingprep.com/stable/profile"

_DEFAULTS = {"benchmark_symbol": "SPY", "external_source": "MANUAL"}

# Statuses whose lots are still held as far as the tracker knows.
HELD_STATUSES = ("OPEN", "TARGET_REACHED")

# A CLOSED lot is mirrored too, but ONLY when it carries a sale_price. Measured
# to today's price a sold position reports a return nobody earned; measured to
# its exit it is a finished, gradeable pick. A lot closed without a recorded
# price is skipped and reported, because the alternative is to invent the
# number that decides whether the pick was good.
CLOSED_STATUS = "CLOSED"


def _config() -> dict:
    path = os.path.join(SRC_ROOT, "specs", "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = (yaml.safe_load(fh) or {}).get("verdict_ledger", {}) or {}
        if cfg:
            return cfg
        logger.warning("No `verdict_ledger:` block in %s; using defaults %s", path, _DEFAULTS)
    except FileNotFoundError:
        logger.warning("%s not found; using defaults %s", path, _DEFAULTS)
    except yaml.YAMLError as e:
        logger.error("Could not parse %s (%s). Fix it or remove it.", path, e)
        raise SystemExit(2)
    return dict(_DEFAULTS)


def preflight(commit: bool) -> None:
    needed = ["DATABASE_URL"] + (["FMP_API_KEY"] if commit else [])
    missing = [k for k in needed if not os.getenv(k)]
    if missing:
        logger.error(
            "Missing required environment variable(s): %s.\n"
            "  Set them in %s (or webapp/.env on a viewer-only checkout).",
            ", ".join(missing), os.path.join(PROJECT_ROOT, ".env"))
        raise SystemExit(2)


def fetch_eod_series(symbol: str, start: date, end: date) -> dict:
    """{date: close} for `symbol` over the range, in ONE call.

    One request rather than one per purchase: the range is fetched whole and
    indexed by date, so a hundred backfilled buys still cost a single call.
    """
    api_key = os.getenv("FMP_API_KEY")
    try:
        resp = requests.get(
            FMP_EOD_URL,
            params={"symbol": symbol, "from": start.isoformat(),
                    "to": end.isoformat(), "apikey": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json() or []
    except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
        logger.error("Historical prices for %s failed (%s).", symbol, e)
        return {}
    out = {}
    for row in rows:
        day, price = row.get("date"), row.get("price")
        if day and isinstance(price, (int, float)) and price > 0:
            out[str(day)[:10]] = float(price)
    return out


def _pct(now: float, then: float):
    """Percentage move, or None when there is no baseline to move from.

    Same rounding as cron/run_outcome_snapshot.py, deliberately: a finished
    pick's final row and a held pick's weekly rows land in the same column and
    must be computed the same way, or the two are not comparable.
    """
    if not then:
        return None
    return round((now - then) / then * 100.0, 4)


def close_on_or_before(series: dict, day: date, lookback: int = 10):
    """The close on `day`, or the most recent one before it.

    Purchases land on non-trading days more often than seems plausible — a
    holiday, a weekend-dated statement line, an off-by-one in a broker export.
    Walking back finds the last real close instead of dropping the row.
    """
    for back in range(lookback + 1):
        hit = series.get((day - timedelta(days=back)).isoformat())
        if hit is not None:
            return hit
    return None


def fetch_profile(ticker: str) -> dict:
    """Company name / sector / industry, best effort. Never fatal.

    market_cap is deliberately NOT taken from here. The profile reports it as of
    today, while every other figure on a mirrored row is as of the purchase
    date, and a column mixing the two is worse than one left empty.
    """
    try:
        resp = requests.get(FMP_PROFILE_URL,
                            params={"symbol": ticker, "apikey": os.getenv("FMP_API_KEY")},
                            timeout=20)
        resp.raise_for_status()
        rows = resp.json() or []
        if rows:
            return {"company_name": rows[0].get("companyName"),
                    "sector": rows[0].get("sector"),
                    "industry": rows[0].get("industry")}
    except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
        logger.warning("Profile lookup for %s failed (%s); leaving it blank.", ticker, e)
    return {}


def pending_purchases(cur, external_source: str) -> list:
    """Externally-sourced purchases with no ledger row yet.

    Grouped by (ticker, purchase_date) rather than taken per lot: the same pick
    bought across three accounts on one day is ONE decision with one return, and
    entering it three times would let it outvote every other pick in the
    average. The fill price is share-weighted across those lots, which is the
    price the holder actually paid in aggregate.
    """
    cur.execute(
        """
        SELECT l.ticker,
               l.purchase_date,
               SUM(l.shares)                            AS shares,
               SUM(l.shares * l.buy_price) / SUM(l.shares) AS buy_price,
               SUM(l.shares * l.buy_price)              AS cost_basis,
               COUNT(*)                                 AS lot_count,
               -- A purchase counts as exited only when EVERY lot of it left.
               -- One lot sold out of two is a trimmed position, not a finished
               -- pick, and the part still held is still being measured.
               BOOL_AND(l.status::text = 'CLOSED' AND l.sale_price IS NOT NULL) AS exited,
               MAX(l.closed_on)                         AS exit_date,
               SUM(l.shares * l.sale_price) / NULLIF(SUM(l.shares), 0) AS exit_price
          FROM stock_tracker.lots l
         WHERE l.source <> 'PIPELINE'
           -- ::text because status is a stock_tracker enum type, and this repo
           -- deliberately does not depend on that type existing.
           AND (l.status::text = ANY(%s)
                OR (l.status::text = %s AND l.sale_price IS NOT NULL))
           AND NOT EXISTS (
                 SELECT 1 FROM verdict_log v
                  WHERE v.source = %s
                    AND v.ticker = l.ticker
                    AND v.verdict_date = l.purchase_date)
         GROUP BY l.ticker, l.purchase_date
         ORDER BY l.purchase_date, l.ticker
        """,
        (list(HELD_STATUSES), CLOSED_STATUS, external_source),
    )
    return cur.fetchall()


def newly_closed(cur, external_source: str) -> list:
    """Ledger rows still marked open whose position has since been SOLD.

    This is the ongoing case, and it is not the same as the backfill. A purchase
    is mirrored once, when it is made; the sale happens later. By then a
    verdict_log row already exists, so `pending_purchases` skips it forever —
    which would leave a sold position being re-priced every week as though it
    were still held, and its return would drift with a market it had left.

    Matched on (ticker, purchase date), the same natural key the mirror uses.
    Requires EVERY lot of that purchase to be closed and priced: one lot sold
    out of two is a trimmed position, not a finished pick.
    """
    cur.execute(
        """
        SELECT v.verdict_id, v.ticker, v.verdict_date, v.price, v.benchmark_price,
               MAX(l.closed_on)                                        AS exit_date,
               SUM(l.shares * l.sale_price) / NULLIF(SUM(l.shares), 0) AS exit_price
          FROM verdict_log v
          JOIN stock_tracker.lots l
            ON l.ticker = v.ticker
           AND l.purchase_date = v.verdict_date
           AND l.source <> 'PIPELINE'
         WHERE v.source = %s
           AND v.exit_date IS NULL
         GROUP BY v.verdict_id, v.ticker, v.verdict_date, v.price, v.benchmark_price
        HAVING BOOL_AND(l.status::text = 'CLOSED' AND l.sale_price IS NOT NULL)
         ORDER BY MAX(l.closed_on)
        """,
        (external_source,),
    )
    return cur.fetchall()


def unpriced_exits(cur) -> list:
    """Closed lots with no sale price — reported, never guessed at."""
    cur.execute(
        """
        SELECT l.ticker, l.purchase_date, l.closed_on
          FROM stock_tracker.lots l
         WHERE l.source <> 'PIPELINE'
           AND l.status::text = 'CLOSED'
           AND l.sale_price IS NULL
         ORDER BY l.closed_on
        """)
    return cur.fetchall()


def has_position_tracker(cur) -> bool:
    cur.execute("SELECT to_regclass('stock_tracker.lots') IS NOT NULL")
    return bool(cur.fetchone()[0])


def run_sync(commit: bool) -> dict:
    cfg = _config()
    preflight(commit)
    benchmark = str(cfg.get("benchmark_symbol", "SPY")).strip().upper()
    external_source = str(cfg.get("external_source", "MANUAL")).strip().upper()

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    # This repo tracks no positions of its own, so the tracker's schema may
    # simply not be here. That is a normal installation, not an error.
    if not has_position_tracker(cur):
        logger.info(
            "No stock_tracker.lots table in this database, so there are no external "
            "purchases to mirror. That is expected unless you keep positions in the "
            "companion tracker app.")
        cur.close(); conn.close()
        return {"pending": 0, "written": 0}

    rows = pending_purchases(cur, external_source)
    closures = newly_closed(cur, external_source)
    if not rows and not closures:
        logger.info("Every externally-sourced purchase already has a ledger row, and "
                    "no open one has been sold since.")
        cur.close(); conn.close()
        return {"pending": 0, "written": 0, "closed": 0}

    dates = [r[1] for r in rows]
    if rows:
        logger.info("%d purchase(s) to mirror, %s to %s.", len(rows), min(dates), max(dates))
    if closures:
        logger.info("%d already-mirrored pick(s) have since been sold.", len(closures))

    unpriced = unpriced_exits(cur)
    exited = [r for r in rows if r[6]]

    if not commit:
        if rows:
            print(f"\n{len(rows)} purchase(s) would be mirrored into verdict_log "
                  f"as source={external_source}:\n")
        for (ticker, purchase_date, shares, buy_price, cost_basis,
             lot_count, is_exited, exit_date, exit_price) in rows:
            note = f"  ({lot_count} lots)" if lot_count > 1 else ""
            if is_exited:
                held = (exit_date - purchase_date).days
                pct = (float(exit_price) - float(buy_price)) / float(buy_price) * 100
                note += f"   EXITED {exit_date} @ {float(exit_price):.2f} ({pct:+.1f}%, {held}d)"
            print(f"    {purchase_date}  {ticker:<6} {float(shares):>10.4f} @ "
                  f"{float(buy_price):>9.2f}  = {float(cost_basis):>9.2f}{note}")
        if rows:
            print(f"\n  {len(exited)} of these are finished picks, graded buy-to-exit.")
        if closures:
            print(f"\n{len(closures)} pick(s) already in the ledger have since been sold "
                  f"and would be settled:\n")
            for _vid, ticker, bought, buy_price, _bp, exit_date, exit_price in closures:
                pct = (float(exit_price) - float(buy_price)) / float(buy_price) * 100
                print(f"    {ticker:<6} bought {bought}  exited {exit_date} @ "
                      f"{float(exit_price):>9.2f}  ({pct:+.1f}%, {(exit_date - bought).days}d)")
            print("\n  Their weekly marks stop here, and any mark taken AFTER the sale "
                  "date is removed:\n  it prices a position that was no longer held.")
        print(f"\n  Benchmark baseline would be {benchmark} on each purchase date"
              + (" and each exit date." if exited or closures else "."))
        if unpriced:
            print(f"\n  {len(unpriced)} closed lot(s) have NO recorded sale price and are "
                  f"skipped:\n    " + ", ".join(f"{t} ({d})" for t, d, _ in unpriced[:10]))
            print("    Record what they sold for and they become gradeable.")
        print("\nDRY RUN - nothing written. Re-run with --commit.")
        cur.close(); conn.close()
        return {"pending": len(rows), "written": 0, "closed": len(closures)}

    # One call covers every purchase AND exit date, however far back they go.
    span = dates + [r[2] for r in closures]
    last = max(dates + [r[7] for r in exited if r[7]] + [r[5] for r in closures])
    series = fetch_eod_series(benchmark, min(span) - timedelta(days=10), last)
    if not series:
        logger.error("No %s history returned; excess return would be NULL for every "
                     "row. Refusing to write a scoreboard that cannot compare to the "
                     "market. Fix the API access and re-run.", benchmark)
        cur.close(); conn.close()
        raise SystemExit(1)

    profiles, written, finals, no_baseline = {}, 0, 0, []
    for (ticker, purchase_date, shares, buy_price, cost_basis,
         lot_count, is_exited, exit_date, exit_price) in rows:
        bench_price = close_on_or_before(series, purchase_date)
        if bench_price is None:
            # Recorded anyway: an absolute return is still worth having, and a
            # missing index close must not cost the whole row.
            no_baseline.append((ticker, purchase_date))
        if ticker not in profiles:
            profiles[ticker] = fetch_profile(ticker)
        profile = profiles[ticker]

        cur.execute(
            """
            INSERT INTO verdict_log (
                ticker, company_name, verdict_date, verdict, verdict_stage,
                price, sector, industry, benchmark_symbol, benchmark_price, source,
                exit_date, exit_price
            ) VALUES (%s, %s, %s, 'BUY', 'INDEPENDENT', %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING verdict_id
            """,
            (ticker, profile.get("company_name"), purchase_date, buy_price,
             profile.get("sector"), profile.get("industry"),
             benchmark if bench_price else None, bench_price, external_source,
             exit_date if is_exited else None, exit_price if is_exited else None),
        )
        got = cur.fetchone()
        if not got:
            continue
        written += 1
        if not is_exited:
            continue

        # The finished pick's one and only outcome row, priced at the exit
        # rather than at today. The snapshot job will never revisit it, because
        # exit_date is set — so this is the number that stands.
        bench_exit = close_on_or_before(series, exit_date)
        total = _pct(float(exit_price), float(buy_price))
        excess = None
        if total is not None and bench_exit and bench_price:
            bench_move = _pct(bench_exit, float(bench_price))
            excess = round(total - bench_move, 4) if bench_move is not None else None
        cur.execute(
            """
            INSERT INTO verdict_outcome (
                verdict_id, ticker, as_of, age_days, price,
                benchmark_symbol, benchmark_price, total_return_pct, excess_return_pct
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (got[0], ticker, exit_date, (exit_date - purchase_date).days, exit_price,
             benchmark if bench_exit else None, bench_exit, total, excess),
        )
        finals += cur.rowcount

    # Settle picks that were mirrored while open and have since been sold.
    settled = 0
    for verdict_id, ticker, bought, buy_price, bench_then, exit_date, exit_price in closures:
        cur.execute("UPDATE verdict_log SET exit_date = %s, exit_price = %s "
                    "WHERE verdict_id = %s", (exit_date, exit_price, verdict_id))

        # Any weekly mark taken after the sale prices a position that was no
        # longer held. Left in place it would also be the LATEST snapshot, and
        # every query here reads the latest one — so the settled result would be
        # silently overridden by a hypothetical one.
        cur.execute("DELETE FROM verdict_outcome WHERE verdict_id = %s AND as_of > %s",
                    (verdict_id, exit_date))

        bench_exit = close_on_or_before(series, exit_date)
        total = _pct(float(exit_price), float(buy_price))
        excess = None
        if total is not None and bench_exit and bench_then:
            bench_move = _pct(bench_exit, float(bench_then))
            excess = round(total - bench_move, 4) if bench_move is not None else None
        cur.execute(
            """
            INSERT INTO verdict_outcome (
                verdict_id, ticker, as_of, age_days, price,
                benchmark_symbol, benchmark_price, total_return_pct, excess_return_pct
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (verdict_id, ticker, exit_date, (exit_date - bought).days, exit_price,
             benchmark if bench_exit else None, bench_exit, total, excess),
        )
        settled += 1

    conn.commit()
    cur.close()
    conn.close()

    if settled:
        logger.info("%d pick(s) settled at their exit and removed from weekly marking.",
                    settled)
    if unpriced:
        logger.warning("%d closed lot(s) have no recorded sale price and stay out of the "
                       "ledger: %s", len(unpriced),
                       ", ".join(f"{t} ({d})" for t, d, _ in unpriced[:8]))
    if finals:
        logger.info("%d finished pick(s) graded buy-to-exit.", finals)

    if no_baseline:
        logger.warning("No %s close found for %d purchase(s); their excess return "
                       "will stay NULL: %s", benchmark, len(no_baseline),
                       ", ".join(f"{t} {d}" for t, d in no_baseline[:5]))
    return {"pending": len(rows), "written": written, "graded_at_exit": finals,
            "settled_since_sold": settled, "unpriced_exits": len(unpriced),
            "benchmark": benchmark}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror externally-sourced purchases into the verdict ledger.")
    parser.add_argument("--commit", action="store_true",
                        help="Actually write. Without it this is a dry run.")
    args = parser.parse_args()

    result = run_sync(args.commit)
    if args.commit:
        logger.info("External pick sync complete. %s", result)
        if result.get("written"):
            logger.info("Next: cron/run_outcome_snapshot.py will price these on its "
                        "weekly run, giving each one a return against the benchmark.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("External pick sync failed with an unhandled exception.")
        sys.exit(1)
