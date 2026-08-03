"""Generate (or regenerate) the Phase E buy case for any stored WATCH report.

    python buy_case.py TICKER [Company] [--run RUN_ID]

The pipeline writes a buy case once, as the last step of the run that produced a
report whose verdict was 'Watch'. That is the right default and the wrong *only*
option, for the same four reasons `sale_advisory.py` exists — plus one that is
particular to this artefact.

- **The report was refined and the buy case could not be.** The critic loop reserves
  the money for a re-derivation before committing to a revision (`refine._Estimator.
  full_round`), but the reservation is an estimate; when it falls short the loop ships
  the previous buy case with a staleness warning rather than nothing.
- **`--skip-buy-case` was used.** A cheap run has no `BUY_CASE` at all, so
  `--buy-check` has nothing to test.
- **The buy-case advisor produced no output.** `analyze_ticker` logs a warning and
  moves on rather than failing the whole ticker — deliberately, but it leaves a hole.
- **The buy case is simply old.** Every threshold in it is anchored to figures, and to
  a PRICE, from the day it was written.
- **And the one that is specific to this document: a buy case ages faster than
  anything else the pipeline produces.** Its first trigger is a price range, measured
  against a quote that moves every session, and the forward earnings estimates the
  range is derived from are revised continuously. A sale advisory written against
  last quarter's filings is still broadly valid this quarter; a buy range written
  against a $44 price and a $5.86 consensus is not, once either has moved. Re-deriving
  one is the ordinary maintenance of a watchlist, not a repair.

Re-running the whole pipeline to recover one artefact costs ~$0.40 and rewrites the
report you already reviewed. This costs ~$0.12-0.18 and touches nothing else.

## Watch only, and no flag to override it

If the named report's verdict is Buy or Avoid, this command refuses. That is the same
rule the pipeline applies (`buy_case_agent.is_watch`), enforced in the same words, and
it is deliberately not overridable: a Buy needs no entry conditions, and writing entry
conditions for an Avoid manufactures a route back into a name the analysis argued
against. If you disagree with the verdict, the answer is `refine.py` — put the report
in front of the critic and let the verdict move on the evidence — not a buy case
written underneath a verdict that says no.

## Where the output goes, and why

Identical to `sale_advisory.py`, and for identical reasons. The new buy case is stored
**under the run_id you name**, so `--buy-check --run <that id>` finds it and the
webapp shows it on that run's Buy Case tab; a second `BUY_CASE` row for the same run
is expected, and every reader takes the newest. The COST gets its own `pipeline_runs`
row, so the spend stays visible to the rolling daily budget guard without rewriting
the record of what the target run cost. That row carries no `ticker_runs` row and no
`refines_run_id`, so it never appears as a browsable run and is never mistaken for a
critic refinement.
"""
import json
import uuid

import main
import refine
import buy_case_agent
from mcp_server import (
    fmp_quarterly_trends,
    db_create_pipeline_run,
    db_get_final_report,
    db_get_agent_output,
)

logger = main.logger


def generate_buy_case(ticker: str, company_name: str = None,
                      run_id: str = None, ignore_budget: bool = False) -> None:
    """Derive a fresh buy case from a stored WATCH report and store it on that run.

    `run_id` names the report to derive from; omitted, the ticker's most recent
    report is used. Works against a pipeline run or a refinement run alike — both
    write to `final_reports` the same way.
    """
    ticker = ticker.strip().upper()

    # 1. The report to derive from.
    try:
        report = json.loads(db_get_final_report(ticker, run_id or ""))
    except Exception as e:
        logger.error(f"[{ticker}] Could not load a report: {e}")
        return
    if not report.get("found"):
        where = f"under run {run_id}" if run_id else "on record"
        logger.error(
            f"[{ticker}] No final report {where}. A buy case is derived from a report, "
            f"so there must be one first — run `python main.py {ticker}`."
        )
        return

    target_run = report["run_id"]
    verdict = (report.get("verdict") or "").upper()
    company_name = company_name or ticker

    # 2. The Watch gate, before any money is spent. Same rule as the pipeline's, from
    # the same function, so the two can never disagree about what a Watch is.
    if not buy_case_agent.is_watch(verdict):
        logger.error(
            f"[{ticker}] The report in run {target_run} reaches a verdict of "
            f"{verdict or 'UNKNOWN'}, not Watch, so no buy case is written for it. A "
            f"'Buy' needs no entry conditions — the entry condition is now — and an "
            f"'Avoid' should not be given any. If you think the verdict is wrong, put "
            f"the report through the critic instead: `python refine.py {ticker} --run "
            f"{target_run}`. If an older report for this ticker WAS a Watch, name it "
            f"with --run."
        )
        return

    logger.info(
        f"Generating a buy case for {ticker} ({company_name}) from the report in run "
        f"{target_run} (verdict {verdict}, {report.get('age_hours')}h old)."
    )

    # Say plainly what is being replaced. Regenerating over a perfectly good buy case
    # is the normal way to use this command — its price range ages by the session —
    # but knowing one already exists is what tells the operator whether this run is a
    # repair or a refresh.
    try:
        existing = json.loads(db_get_agent_output(ticker, buy_case_agent.BUY_CASE_TYPE,
                                                  target_run))
    except Exception:
        existing = {"found": False}
    if existing.get("found"):
        logger.info(f"[{ticker}] This run already has a buy case (written "
                    f"{(existing.get('created_at') or '')[:19]}). The new one will "
                    f"supersede it — readers always take the newest.")
    else:
        logger.info(f"[{ticker}] This run has no buy case yet.")

    # 3. The rolling daily ceiling still applies. There is no per-invocation ceiling:
    # this is one deliberate call for a known artefact, so a second number to tune
    # would be ceremony.
    if main.BUDGET_ENABLED and not ignore_budget:
        prior = main._prior_day_spend()
        projected = prior + refine.SEED_BUY_CASE_USD
        if projected >= main.BUDGET_PER_DAY_USD:
            logger.error(
                f"[{ticker}] Refusing to start: the {main.BUDGET_DAY_WINDOW_HOURS}h "
                f"ceiling ${main.BUDGET_PER_DAY_USD:.2f} would be reached "
                f"(${prior:.2f} already spent, this needs about "
                f"${refine.SEED_BUY_CASE_USD:.2f}). Use --no-budget to override."
            )
            return

    # 4. Inputs. Figures and the price are recomputed rather than read back from the
    # report's prose, so the new triggers are anchored to CURRENT values — which is
    # the whole point of re-deriving one. All deterministic, none costs tokens.
    report_body = refine.strip_generated_sections(report["markdown_report"])
    if not report_body.strip():
        logger.error(f"[{ticker}] The stored report has no analyst prose to derive a "
                     f"buy case from; nothing done.")
        return
    candidate = refine._load_candidate(ticker)
    quarterly_data = fmp_quarterly_trends(ticker)
    # One quote, used for both the figures block the agent reads and the price block
    # its triggers are measured against.
    price = main._price_snapshot(ticker)
    verified_figures = main._format_verified_figures(candidate, price)
    price_data = buy_case_agent.price_data_block(ticker, price)
    # Logged before anything is billed: the price is what the whole document turns on,
    # and seeing it here is what lets an operator abort a refresh that is about to be
    # written against a quote that has barely moved since the last one.
    logger.info(f"[{ticker}] {price_data.splitlines()[0]}")

    # 5. Its own cost row — see the module docstring for why this is not folded into
    # the target run's totals.
    case_run_id = str(uuid.uuid4())
    main._check_db(db_create_pipeline_run(case_run_id, [ticker]),
                   "create buy-case run")

    usage = main._new_usage()
    stored = buy_case_agent.write_buy_case(
        target_run, ticker, company_name, report_body, verified_figures,
        quarterly_data, candidate, usage,
        origin_note=_origin_note(target_run, case_run_id),
        metadata={"origin": "regenerated_standalone", "case_run_id": case_run_id,
                  "superseded_existing": bool(existing.get("found")), "verdict": verdict},
        price_data=price_data,
    )
    if not stored:
        main._finalize_run(case_run_id, usage, "Buy Case Run", status="FAILED")
        return

    main._collect_embedding_usage(usage)
    logger.info(
        f"[{ticker}] Buy case stored on run {target_run}. "
        f"`python main.py --buy-check {ticker} --run {target_run}` will now use it."
    )
    main._finalize_run(case_run_id, usage, "Buy Case Run")


def _origin_note(target_run: str, case_run_id: str) -> str:
    """Say where this buy case came from, since it is not the one the run produced.

    Without it, a buy case generated days after its report is indistinguishable from
    one written alongside it — and here the difference is sharper than for a sale
    advisory, because the price range is anchored to a quote from the day of
    generation. A reader comparing the range against the report's own valuation
    discussion needs to know they are looking at two different days."""
    return (
        f"> **Generated separately from the report's own run.** Derived from the "
        f"report stored under run `{target_run}` by a later `buy_case.py` run "
        f"(`{case_run_id}`), and anchored to the price and figures current at that "
        f"time — not to the ones the report was written against. It supersedes any "
        f"buy case that run produced.\n\n"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate or regenerate the buy case for a stored Watch report",
        epilog=(
            "A buy case names the price range and the measurable events at which a\n"
            "'Watch' becomes a 'Buy'. The pipeline writes one per Watch verdict; this\n"
            "command writes one for any Watch report, on demand.\n"
            "\n"
            "  python buy_case.py CROX                      from CROX's latest report\n"
            "  python buy_case.py CROX --run <RUN_ID>       from a SPECIFIC run's report\n"
            "  python buy_case.py CROX \"Crocs Inc\"          with an explicit company name\n"
            "\n"
            "Use it when a run has no buy case (--skip-buy-case was used, or the advisor\n"
            "produced nothing), when a critic refinement left one stale, or — the common\n"
            "case — when an existing buy case's price range has been overtaken by the\n"
            "market and you want it re-anchored to today's price and estimates.\n"
            "\n"
            "Refuses on a report whose verdict is Buy or Avoid. Costs ~$0.12-0.18 against\n"
            "the rolling daily budget. The buy case is stored on the run you name, so\n"
            "`main.py --buy-check --run <that id>` picks it up; the cost gets its own row\n"
            "so the named run's own totals are not rewritten."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("ticker", help="Ticker whose Watch report the buy case is derived from")
    parser.add_argument("company", nargs="?", help="Optional company name")
    parser.add_argument("--run", metavar="RUN_ID", default=None,
                        help="Derive from a SPECIFIC run's report instead of the latest")
    parser.add_argument("--no-budget", action="store_true",
                        help="Ignore the rolling daily spend ceiling for this invocation")
    args = parser.parse_args()

    generate_buy_case(args.ticker, args.company, args.run, args.no_budget)
