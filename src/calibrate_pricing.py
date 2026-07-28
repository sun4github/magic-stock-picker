"""Derive real per-1M token prices from an actual bill, and check the configured
prices against it.

Why this exists: the agents are configured with the moving alias
`gemini-flash-latest`. Google repoints it at each new Flash release, so any price
written into config.yaml silently goes stale the moment the alias moves. It did —
the alias now resolves to `gemini-3.6-flash` while config priced Gemini 2.5 Flash,
understating spend ~2.4x. Rather than trust published rates, this reconciles the
token counts the pipeline recorded against what Google actually charged.

Usage
-----
Compare the configured prices to a known bill for the last N runs:

    python calibrate_pricing.py --runs 2 --actual 0.55

Solve for the exact rates when you have the two Cloud Billing SKU amounts
(Billing -> Reports, group by SKU, filtered to the run window):

    python calibrate_pricing.py --runs 2 --input-charge 0.38 --output-charge 0.17

Restrict to a specific run:

    python calibrate_pricing.py --run-id 6bed8cef-...
"""
import argparse
import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def fetch_runs(limit, run_id):
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    if run_id:
        cur.execute(
            """SELECT run_id, started_at, model_requests, input_tokens, output_tokens,
                      llm_cost_usd, search_cost_usd, total_cost_usd
                 FROM pipeline_runs WHERE run_id = %s""", (run_id,))
    else:
        cur.execute(
            """SELECT run_id, started_at, model_requests, input_tokens, output_tokens,
                      llm_cost_usd, search_cost_usd, total_cost_usd
                 FROM pipeline_runs ORDER BY started_at DESC LIMIT %s""", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=2, help="how many recent runs to total")
    ap.add_argument("--run-id", help="calibrate against one specific run instead")
    ap.add_argument("--actual", type=float,
                    help="actual LLM charge in USD for those runs (from the bill)")
    ap.add_argument("--input-charge", type=float,
                    help="Cloud Billing amount for the INPUT-token SKU")
    ap.add_argument("--output-charge", type=float,
                    help="Cloud Billing amount for the OUTPUT-token SKU")
    ap.add_argument("--input-tokens", type=float,
                    help="usage count on the INPUT-token SKU row")
    ap.add_argument("--output-tokens", type=float,
                    help="usage count on the OUTPUT-token SKU row")
    ap.add_argument("--cached-charge", type=float,
                    help="Cloud Billing amount for the CACHED-input-token SKU")
    ap.add_argument("--cached-tokens", type=float,
                    help="usage count on the CACHED-input-token SKU row")
    args = ap.parse_args()

    rows = fetch_runs(args.runs, args.run_id)
    if not rows:
        print("No runs found.")
        return 1

    print(f"{'run':10}{'when':21}{'calls':>6}{'input':>12}{'output':>10}{'est LLM$':>11}")
    tin = tout = test = 0
    for r in rows:
        print(f"{r[0][:8]:10}{str(r[1])[:19]:21}{r[2]:>6}{r[3]:>12,}{r[4]:>10,}{float(r[5]):>11.4f}")
        tin += r[3]
        tout += r[4]
        test += float(r[5])
    print(f"{'TOTAL':10}{'':21}{'':>6}{tin:>12,}{tout:>10,}{test:>11.4f}")
    print()

    # Exact solve. Prefer the SKU's OWN usage counts over our recorded totals: the
    # billing export lags by hours, so a report pulled mid-day covers only the runs
    # that had posted by then. Dividing a partial charge by a full day's tokens
    # understates every rate.
    if args.input_charge is not None and args.output_charge is not None:
        in_tokens = args.input_tokens if args.input_tokens else tin
        out_tokens = args.output_tokens if args.output_tokens else tout
        if not args.input_tokens:
            print("NOTE: no --input-tokens given, dividing by OUR recorded totals.")
            print("      If the billing report lags, these rates come out too low.")
            print("      Pass the usage counts from the SKU rows for an exact answer.")
            print()
        p_in = args.input_charge / (in_tokens / 1e6) if in_tokens else 0.0
        p_out = args.output_charge / (out_tokens / 1e6) if out_tokens else 0.0
        print("Derived from the SKU rows:")
        print(f"  input_usd_per_1m:  {p_in:.4f}")
        print(f"  output_usd_per_1m: {p_out:.4f}")
        if args.cached_charge is not None and args.cached_tokens:
            p_cached = args.cached_charge / (args.cached_tokens / 1e6)
            print(f"  cached_input_usd_per_1m: {p_cached:.4f}")
            print(f"  (cached ran {args.cached_tokens / (in_tokens + args.cached_tokens) * 100:.0f}% "
                  f"of prompt tokens)")
        print()
        print("Put these under llm_pricing.models.<resolved-model> in specs/config.yaml")
        print("and set confirmed: true.")
        print()
        # Coverage check: if the SKU token counts are well below what we recorded,
        # the report is a lagged partial and its total is not the day's real spend.
        if args.input_tokens:
            billed_prompt = args.input_tokens + (args.cached_tokens or 0)
            if tin and billed_prompt < tin * 0.9:
                print(f"WARNING: billing shows {billed_prompt:,.0f} prompt tokens but we "
                      f"recorded {tin:,} for these runs.")
                print(f"         The report covers roughly {billed_prompt / tin * 100:.0f}% "
                      f"of them — billing data lags by hours.")
                print(f"         Re-pull the report later; the total will rise.")
        return 0

    # Only a combined total: one equation, two unknowns. Show the ratio the
    # configured prices are off by, and what each rate would be if the other is
    # correct -- enough to sanity-check without pretending to solve it.
    if args.actual is not None:
        if test <= 0:
            print("Estimated cost is zero — is the model priced in config.yaml?")
            return 1
        ratio = args.actual / test
        print(f"actual ${args.actual:.4f} vs estimated ${test:.4f}  ->  {ratio:.2f}x off")
        print()
        from main import MODEL_PRICES  # imported late: pulls in the whole pipeline
        for model, entry in MODEL_PRICES.items():
            p_in = float(entry.get("input_usd_per_1m", 0))
            p_out = float(entry.get("output_usd_per_1m", 0))
            in_cost = (tin / 1e6) * p_in
            out_cost = (tout / 1e6) * p_out
            print(f"  under '{model}' rates (${p_in}/1M in, ${p_out}/1M out):")
            print(f"    input would be  ${in_cost:.4f}, output ${out_cost:.4f}")
            if tout:
                print(f"    if the input rate is right, output must be "
                      f"${(args.actual - in_cost) / (tout / 1e6):.2f}/1M")
            if tin:
                print(f"    if the output rate is right, input must be "
                      f"${(args.actual - out_cost) / (tin / 1e6):.2f}/1M")
        print()
        print("Re-run with --input-charge and --output-charge (the two SKU amounts")
        print("from Cloud Billing) to solve both rates exactly.")
        return 0

    print("Pass --actual, or --input-charge with --output-charge, to calibrate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
