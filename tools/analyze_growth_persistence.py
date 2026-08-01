"""Re-derive the evidence behind `max_growth_rate_for_peg` (and the other PEG
thresholds) from live FMP data.

    python tools/analyze_growth_persistence.py                  # 22 per sector
    python tools/analyze_growth_persistence.py --per-sector 40  # bigger, slower

WHY THIS LIVES IN tools/ AND NOT src/
-------------------------------------
It is NOT part of the pipeline. Nothing in `main.py`, `mcp_server.py` or the
screener imports it, and it never runs during a screen or an analysis — it computes
no PEG and screens no company. It is an offline instrument for deciding what the
thresholds in config.yaml should be, run by hand when you are tuning them. Keeping
it out of `src/` makes that separation visible rather than a matter of convention.

WHY THIS EXISTS
---------------
The growth cap answers one question: *above what rate does past growth stop telling
you anything about future growth?* That is an empirical question, and the answer
drifts — it depends on where the market is in a cycle, which sectors are earning
peak margins, and how far the sample reaches down the market-cap ladder.

The thresholds shipped in config.yaml were set from ONE sample taken 2026-07-31
(189 companies, 9 sectors, largest by market cap in each). Re-run this before
changing any of them, and re-run it periodically so a threshold tuned to one market
does not quietly persist into a different one. See agent_architecture.md §10.I.

THE METHOD (no forecast required)
---------------------------------
For every company with enough history, compare:

    past growth      = CAGR(FY-6 -> FY-3)   what you would have known 3 years ago
    realised growth  = CAGR(FY-3 -> FY0)    what actually happened next

Both windows are historical, so this measures persistence without predicting
anything. Bucket by the first, report the second.

COST: one FMP screener call plus one annual income-statement call per sampled
company (~190 calls at the default). FMP Starter is subscription-metered, so this
costs time, not money. No LLM calls, no database writes.
"""
import argparse
import os
import statistics as st
import sys
import time
from collections import defaultdict

# The screener lives in src/ and is imported as a plain module (it is a script, not
# an installed package), so make it importable from this directory. Doing it here
# rather than requiring the caller to set PYTHONPATH keeps the documented command
# runnable from the repository root exactly as the README prints it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import magic_formula_starter_screener as s  # noqa: E402


# NOTE ON DUPLICATION — deliberately none.
#
# Every piece of growth math here is IMPORTED from the screener
# (`annualised_growth`, `pick_eps_field`, `base_window_margin`), never reimplemented.
# A tuning study that measured growth differently from the code being tuned would
# recommend thresholds for a formula the screener does not use, and nothing would
# fail loudly enough to notice. This file contributes only sampling, bucketing and
# presentation — it computes no PEG and screens nothing.


def _pct(x):
    return "n/a" if x is None else f"{x * 100:6.1f}%"


def _quantile(sorted_values, q):
    if not sorted_values:
        return None
    return sorted_values[min(int(q * len(sorted_values)), len(sorted_values) - 1)]


def gather(per_sector, min_market_cap, universe_limit):
    """Stratified sample: the `per_sector` largest eligible companies in each sector,
    with up to 8 years of annual income statements each."""
    print("Fetching the screener universe...")
    universe = s.fetch_screener_universe(s.API_KEY, min_market_cap, universe_limit)

    by_sector = defaultdict(list)
    for c in universe:
        by_sector[c.get("sector") or "Unknown"].append(c)

    sample = []
    for _, rows in sorted(by_sector.items()):
        # Largest first: the micro-cap tail has the thinnest EPS history, and a
        # sample dominated by it would measure data quality, not persistence.
        rows.sort(key=lambda r: r.get("marketCap") or 0, reverse=True)
        sample.extend(rows[:per_sector])

    print(f"Sampling {len(sample)} companies across {len(by_sector)} sectors "
          f"(<= {per_sector} each). One annual call per company...")

    out = []
    for i, c in enumerate(sample):
        try:
            rows = s.fmp_get(
                "https://financialmodelingprep.com/stable/income-statement",
                params={"symbol": c["symbol"], "limit": 8, "apikey": s.API_KEY},
                context=f"{c['symbol']} annual",
            )
        except s.FMPError as e:
            print(f"  [skip] {c['symbol']}: {e}")
            continue
        if not isinstance(rows, list) or len(rows) < 4:
            continue
        rows = [r for r in rows if isinstance(r, dict)]
        # Same field-selection rule the screener applies: diluted preferred, and the
        # SAME measure across every year or none at all.
        field = s.pick_eps_field(rows)
        out.append({
            "symbol": c["symbol"],
            "sector": c.get("sector") or "?",
            "marketCap": c.get("marketCap") or 0,
            "rows": rows,
            "eps": [s._annual_eps(r, field) for r in rows] if field else [],
        })
        time.sleep(0.12)
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(sample)} ... ({len(out)} usable)")
    return out


def report_persistence(data, years):
    """The table that sets the cap: what fast growers actually did next."""
    rows = []
    for c in data:
        eps = c["eps"]
        if len(eps) < 2 * years + 1:
            continue
        past = s.annualised_growth(eps[years], eps[2 * years], years)
        fwd = s.annualised_growth(eps[0], eps[years], years)
        if past is None or fwd is None:
            continue
        rows.append({"symbol": c["symbol"], "sector": c["sector"],
                     "past": past, "fwd": fwd})

    print(f"\n{'=' * 78}\nGROWTH PERSISTENCE  ({len(rows)} companies with "
          f"{2 * years + 1}+ years and positive endpoints)\n{'=' * 78}")
    if not rows:
        print("  Not enough history in this sample to measure persistence.")
        return None

    buckets = [(-99, 0.0, "shrinking"), (0.0, 0.10, "0-10%"), (0.10, 0.25, "10-25%"),
               (0.25, 0.50, "25-50%"), (0.50, 1.00, "50-100%"), (1.00, 99, ">100%")]
    print(f"{'growth 3 yrs ago':<18}{'n':>4}{'median next 3y':>16}{'mean':>9}"
          f"{'kept >50%':>12}{'went negative':>16}")
    for lo, hi, label in buckets:
        grp = [r["fwd"] for r in rows if lo <= r["past"] < hi]
        if not grp:
            continue
        print(f"{label:<18}{len(grp):>4}{_pct(st.median(grp)):>16}{_pct(st.mean(grp)):>9}"
              f"{sum(1 for f in grp if f > 0.50) / len(grp) * 100:>11.0f}%"
              f"{sum(1 for f in grp if f < 0) / len(grp) * 100:>15.0f}%")

    realised = sorted(r["fwd"] for r in rows)
    print(f"\nDISTRIBUTION OF ACTUALLY-REALISED {years}-YEAR EPS CAGR "
          f"(what companies really deliver):")
    for q in (0.50, 0.75, 0.90, 0.95, 0.99):
        print(f"  p{int(q * 100):<3} {_pct(_quantile(realised, q))}")

    p95 = _quantile(realised, 0.95)
    print(f"\n--- RECOMMENDATION -----------------------------------------------------")
    print(f"  Set max_growth_rate_for_peg near the 95th percentile of realised growth,")
    print(f"  which in THIS sample is {p95 * 100:.0f}%.  Currently configured: "
          f"{s.MAX_GROWTH_FOR_PEG * 100:.0f}%.")
    fast = [r for r in rows if r["past"] > 0.50]
    if fast:
        print(f"  Of the {len(fast)} companies that HAD been growing >50%/yr, "
              f"{sum(1 for r in fast if r['fwd'] < 0) / len(fast) * 100:.0f}% went on to")
        print(f"  NEGATIVE growth (median {_pct(st.median([r['fwd'] for r in fast])).strip()}). "
              f"If that share is high, the cap is doing real work.")
        print(f"  CAUTION: that is only {len(fast)} companies — treat the direction as")
        print(f"  established and the exact figures as indicative.")
    print(f"  Remember: max_peg x max_growth_rate_for_peg x 100 is a HARD P/E ceiling")
    print(f"  for the whole screen — currently "
          f"{s.MAX_PEG * s.MAX_GROWTH_FOR_PEG * 100:.0f}. Move the two together.")
    return p95


def report_base_margin(data, years):
    """The distribution behind `min_base_net_margin`."""
    margins = []
    for c in data:
        if len(c["rows"]) < 2 * years:
            continue
        margin = s.base_window_margin(c["rows"][years:2 * years])
        if margin is not None:
            margins.append(margin)
    margins.sort()
    print(f"\n{'=' * 78}\nBASE-WINDOW NET MARGIN  ({len(margins)} companies)\n{'=' * 78}")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90):
        print(f"  p{int(q * 100):<3} {_pct(_quantile(margins, q))}")
    print(f"\n  min_base_net_margin should sit BELOW the ordinary range so it catches")
    print(f"  only genuinely breakeven base periods. p25 here is "
          f"{_pct(_quantile(margins, 0.25)).strip()}; configured: "
          f"{s.MIN_BASE_NET_MARGIN * 100:.0f}%.")


def report_formula(data, years):
    """Endpoint vs sums on the companies where the two most disagree."""
    hot = []
    for c in data:
        eps = c["eps"]
        if len(eps) < 2 * years or any(e is None for e in eps[:2 * years]):
            continue
        end = s.annualised_growth(eps[0], eps[years], years)
        sums = s.annualised_growth(
            sum(eps[0:years]), sum(eps[years:2 * years]), years)
        if end is not None and end > 0.50:
            hot.append((c["symbol"], c["sector"], end, sums))
    print(f"\n{'=' * 78}\nFORMULA CHECK — companies the ENDPOINT form calls >50% growers"
          f"\n{'=' * 78}")
    if not hot:
        print("  None in this sample.")
        return
    for sym, sector, end, sums in sorted(hot, key=lambda r: -r[2])[:15]:
        print(f"  {sym:<6}{sector[:20]:<21} endpoint {_pct(end)}   sums {_pct(sums)}")
    tamed = sum(1 for _, _, _, sums in hot if sums is not None and sums < 0.50)
    print(f"\n  {tamed}/{len(hot)} fall below 50% once all {2 * years} years are counted.")
    print(f"  A large share here means the endpoint form was reading one-off spikes,")
    print(f"  which is the case for eps_growth_method: \"sums\" (currently "
          f"{s.EPS_GROWTH_METHOD!r}).")


def main():
    ap = argparse.ArgumentParser(
        description="Re-derive the evidence behind the PEG thresholds from live FMP data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--per-sector", type=int, default=22,
                    help="companies sampled per sector, largest first (default 22)")
    ap.add_argument("--min-market-cap", type=float, default=300_000_000,
                    help="floor for the sampled universe (default $300M)")
    ap.add_argument("--universe-limit", type=int, default=2500,
                    help="how many companies to pull from the FMP screener (default 2500)")
    args = ap.parse_args()

    if s.API_KEY == "YOUR_FMP_API_KEY_HERE":
        print("Error: FMP_API_KEY is not configured.")
        return 1

    data = gather(args.per_sector, args.min_market_cap, args.universe_limit)
    if not data:
        print("No usable companies gathered.")
        return 1

    years = s.EPS_GROWTH_YEARS
    print(f"\nSample: {len(data)} companies. Current settings — "
          f"max_peg={s.MAX_PEG}, max_growth_rate_for_peg={s.MAX_GROWTH_FOR_PEG}, "
          f"min_base_net_margin={s.MIN_BASE_NET_MARGIN}, "
          f"eps_growth_method={s.EPS_GROWTH_METHOD!r}")

    report_persistence(data, years)
    report_base_margin(data, years)
    report_formula(data, years)

    print(f"\n{'=' * 78}")
    print("To change a threshold, edit screening_parameters in specs/config.yaml, then")
    print("re-run `python main.py --screen-only` to see the effect on the candidate")
    print("list before paying for any Phase B analysis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
