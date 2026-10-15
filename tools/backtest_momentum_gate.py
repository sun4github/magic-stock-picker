"""Would the entry gate have helped? Replays it over the verdict ledger.

For every analysis verdict on record (PIPELINE and ONDEMAND, INDEPENDENT stage —
never MANUAL, which is not this system's judgement), this rebuilds the price chart
as it stood on the verdict date from FMP end-of-day history, runs the same
`momentum.momentum_state` the pipeline now runs, and grades each chart state
against the latest outcome snapshot the ledger already holds.

    python tools/backtest_momentum_gate.py [--min-age DAYS] [--csv out.csv]

Read-only: one SELECT against the ledger and one FMP history call per ticker. No
model calls, no writes.

Two approximations, both stated in the output so nobody mistakes this for the live
gate: the averages and the 52-week range are computed from CLOSES (the live quote
uses FMP's own averages and intraday highs/lows), and the price is the one the
ledger stamped on the verdict row.
"""
import argparse
import csv
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

import psycopg2
import requests
from dotenv import load_dotenv

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
load_dotenv(os.path.join(ROOT, ".env"))

import momentum  # noqa: E402

FMP_EOD_URL = "https://financialmodelingprep.com/stable/historical-price-eod/light"

QUERY = """
SELECT v.verdict_id, v.ticker, v.verdict, v.source, v.verdict_date, v.price,
       o.as_of, o.age_days, o.total_return_pct, o.excess_return_pct
FROM verdict_log v
JOIN LATERAL (
    SELECT as_of, age_days, total_return_pct, excess_return_pct
    FROM verdict_outcome o
    WHERE o.verdict_id = v.verdict_id
    ORDER BY as_of DESC
    LIMIT 1
) o ON TRUE
WHERE v.source IN ('PIPELINE', 'ONDEMAND')
  AND v.verdict_stage = 'INDEPENDENT'
ORDER BY v.verdict_date, v.ticker
"""


def fetch_closes(symbol: str, start: date, end: date) -> list:
    """[(date, close)] oldest first, or []."""
    try:
        resp = requests.get(FMP_EOD_URL, params={
            "symbol": symbol, "from": start.isoformat(), "to": end.isoformat(),
            "apikey": os.getenv("FMP_API_KEY")}, timeout=30)
        resp.raise_for_status()
        rows = resp.json() or []
    except Exception as e:
        print(f"  ! history for {symbol} failed: {e}", file=sys.stderr)
        return []
    out = [(str(r["date"])[:10], float(r["price"])) for r in rows
           if r.get("date") and isinstance(r.get("price"), (int, float)) and r["price"] > 0]
    return sorted(out)


def chart_on(series: list, verdict_date: date, price):
    """Rebuild the quote dict as of the session BEFORE the verdict date."""
    prior = [c for d, c in series if d < verdict_date.isoformat()]
    if not prior:
        return None, []
    p = float(price) if price else prior[-1]
    year = prior[-252:]
    quote = {
        "price": p,
        "priceAvg50": momentum._sma(prior, 50),
        "priceAvg200": momentum._sma(prior, 200),
        "yearHigh": max(year + [p]),
        "yearLow": min(year + [p]),
        "as_of": verdict_date.isoformat(),
    }
    return quote, prior


def _fmt(xs):
    if not xs:
        return "      -       -      -"
    hit = 100.0 * sum(1 for x in xs if x > 0) / len(xs)
    return f"{statistics.mean(xs):+7.1f} {statistics.median(xs):+7.1f} {hit:5.0f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age", type=int, default=0,
                    help="only grade verdicts whose latest snapshot is at least this old")
    ap.add_argument("--csv", help="write one row per verdict here")
    args = ap.parse_args()

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(QUERY)
    rows = cur.fetchall()
    conn.close()
    rows = [r for r in rows if (r[7] or 0) >= args.min_age]
    print(f"{len(rows)} graded analysis verdicts (min age {args.min_age} days)\n")

    span = timedelta(days=430)  # ~220 sessions before the earliest verdict
    by_ticker = defaultdict(list)
    for r in rows:
        by_ticker[r[1]].append(r)

    results = []
    for ticker, trs in sorted(by_ticker.items()):
        first, last = min(r[4] for r in trs), max(r[4] for r in trs)
        series = fetch_closes(ticker, first - span, last)
        for (vid, t, verdict, source, vdate, price, as_of, age,
             total, excess) in trs:
            quote, prior = chart_on(series, vdate, price)
            m = momentum.momentum_state(quote, prior) if quote else {"state": "UNKNOWN"}
            results.append({
                "ticker": t, "verdict": verdict, "source": source,
                "verdict_date": vdate.isoformat(), "state": m["state"],
                "entry_call": momentum.entry_call(verdict, m["state"]),
                "pct_vs_50dma": m.get("pct_vs_50dma"),
                "pct_vs_200dma": m.get("pct_vs_200dma"),
                "dma200_slope_pct": m.get("dma200_slope_pct"),
                "range_position": m.get("range_position"),
                # What the screen's 52-week-high gate (added 2026-08-29) measures.
                "pct_off_52w_high": (round(100.0 * (quote["price"] / quote["yearHigh"] - 1), 1)
                                     if quote else None),
                "age_days": age,
                "total_return_pct": float(total) if total is not None else None,
                "excess_return_pct": float(excess) if excess is not None else None,
            })

    print("Returns to the latest snapshot, in % (excess = vs SPY).")
    print("                              n |   total: mean  median  >0 |  excess: mean  median  >0")
    def table(key_fn, title):
        print(f"-- {title}")
        groups = defaultdict(list)
        for r in results:
            groups[key_fn(r)].append(r)
        for k in sorted(groups, key=str):
            g = groups[k]
            tot = [r["total_return_pct"] for r in g if r["total_return_pct"] is not None]
            exc = [r["excess_return_pct"] for r in g if r["excess_return_pct"] is not None]
            print(f"   {str(k):<26}{len(g):>3} | {_fmt(tot)} | {_fmt(exc)}")
    table(lambda r: r["verdict"], "by verdict")
    table(lambda r: (r["verdict"], r["state"]), "by verdict x chart state")
    buys = [r for r in results if r["verdict"] == "BUY"]
    print("\n-- every BUY")
    for r in sorted(buys, key=lambda r: r["verdict_date"]):
        exc = r["excess_return_pct"]
        print(f"   {r['verdict_date']} {r['ticker']:<6} {r['state']:<14} "
              f"vs50 {r['pct_vs_50dma'] if r['pct_vs_50dma'] is not None else '-':>7} "
              f"vs200 {r['pct_vs_200dma'] if r['pct_vs_200dma'] is not None else '-':>7} "
              f"slope {r['dma200_slope_pct'] if r['dma200_slope_pct'] is not None else '-':>6} "
              f"rng {r['range_position'] if r['range_position'] is not None else '-':>5} "
              f"offhi {r['pct_off_52w_high'] if r['pct_off_52w_high'] is not None else '-':>6} "
              f"| {r['age_days']:>3}d excess {exc if exc is not None else float('nan'):+7.1f}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(results[0].keys()) if results else ["ticker"])
            w.writeheader()
            w.writerows(results)
        print(f"\nWrote {len(results)} rows to {args.csv}")


if __name__ == "__main__":
    main()
