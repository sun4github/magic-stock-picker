"""Generate (or regenerate) the Phase C sale advisory for any stored report.

    python sale_advisory.py TICKER [Company] [--run RUN_ID]

The pipeline writes a sale advisory once, as the last step of the run that produced
the report. That is the right default and the wrong *only* option, because a report
can outlive its advisory in several ordinary ways:

- **The report was refined and the advisory could not be.** The critic loop reserves
  the money for a re-derivation before committing to the revision that makes the
  advisory stale (`refine._Estimator.full_round`), but the reservation is an
  estimate; when it falls short the loop ships the previous advisory with a
  staleness warning rather than nothing. This command is how that gets repaired.
- **`--skip-sale-advisor` was used.** A cheap Phase-B-only run has no `SALE_CASE` at
  all, so `--sell-check` has nothing to test.
- **The sale advisor produced no output.** `analyze_ticker` logs a warning and moves
  on rather than failing the whole ticker — deliberately, but it leaves a hole.
- **The advisory is simply old.** Its triggers are anchored to figures from the day
  it was written; re-deriving it against today's figures is sometimes what you want
  before acting on it.

Re-running the whole pipeline to recover one artefact costs ~$0.40 and rewrites the
report you already reviewed. This costs ~$0.08-0.10 and touches nothing else.

## Where the output goes, and why

The new advisory is stored **under the run_id you name**, not under a run of its
own. That is the entire point: an advisory belongs to the report it was derived
from, so this is what makes `--sell-check --run <that id>` find it and what makes
the webapp show it on that run's Sale Advisory tab. A second `SALE_CASE` row for the
same run is expected — every reader takes the newest.

The COST, however, gets its own `pipeline_runs` row. Adding it to the target run's
totals would rewrite the record of what that run cost, which is the one thing the
run history is for. A separate row keeps the spend visible to the rolling daily
budget guard without falsifying history. It carries no `ticker_runs` row and no
`refines_run_id`, so it never appears as a browsable run and is never mistaken for a
critic refinement.

## Why a separate command rather than a flag on refine.py

Refinement is about the report's *reasoning*; this is about one derived artefact
being absent or out of date. Tying regeneration to the critic loop would mean
needing a critic review to fix something the critic has nothing to do with — and,
concretely, re-running `refine.py` would NOT fix a stale advisory: the critic would
agree immediately (the report was already corrected), no revision would run, and the
"no revision → carry the existing advisory forward" rule would carry the stale one
forward again, this time without its warning label.
"""
import os
import json
import uuid

import main
import refine
from mcp_server import (
    fmp_quarterly_trends,
    db_create_pipeline_run,
    db_store_agent_output,
    db_get_final_report,
    db_get_agent_output,
)

logger = main.logger


def generate_sale_advisory(ticker: str, company_name: str = None,
                           run_id: str = None, ignore_budget: bool = False) -> None:
    """Derive a fresh sale advisory from a stored report and store it on that run.

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
            f"[{ticker}] No final report {where}. A sale advisory is derived from a "
            f"report, so there must be one first — run `python main.py {ticker}`."
        )
        return

    target_run = report["run_id"]
    company_name = company_name or ticker
    logger.info(
        f"Generating a sale advisory for {ticker} ({company_name}) from the report in "
        f"run {target_run} (verdict {report.get('verdict')}, "
        f"{report.get('age_hours')}h old)."
    )

    # Say plainly what is being replaced. Regenerating over a perfectly good advisory
    # is a legitimate thing to want (its thresholds age), but it should never be a
    # surprise, and knowing an advisory already exists is what tells the operator
    # whether this run is a repair or a refresh.
    try:
        existing = json.loads(db_get_agent_output(ticker, "SALE_CASE", target_run))
    except Exception:
        existing = {"found": False}
    if existing.get("found"):
        logger.info(f"[{ticker}] This run already has a sale advisory (written "
                    f"{(existing.get('created_at') or '')[:19]}). The new one will "
                    f"supersede it — readers always take the newest.")
    else:
        logger.info(f"[{ticker}] This run has no sale advisory yet.")

    # 2. The rolling daily ceiling still applies. There is no per-invocation ceiling:
    # this is one deliberate call for a known artefact, so a second number to tune
    # would be ceremony. The daily window is the guard that actually protects against
    # a bad afternoon.
    if main.BUDGET_ENABLED and not ignore_budget:
        prior = main._prior_day_spend()
        projected = prior + refine.SEED_ADVISORY_USD
        if projected >= main.BUDGET_PER_DAY_USD:
            logger.error(
                f"[{ticker}] Refusing to start: the {main.BUDGET_DAY_WINDOW_HOURS}h "
                f"ceiling ${main.BUDGET_PER_DAY_USD:.2f} would be reached "
                f"(${prior:.2f} already spent, this needs about "
                f"${refine.SEED_ADVISORY_USD:.2f}). Use --no-budget to override."
            )
            return

    # 3. Inputs. Figures are recomputed rather than read back from the report's prose,
    # so the advisory's thresholds are anchored to CURRENT values — which is the whole
    # point of re-deriving one. Both calls are deterministic and cost no tokens.
    report_body = refine.strip_generated_sections(report["markdown_report"])
    if not report_body.strip():
        logger.error(f"[{ticker}] The stored report has no analyst prose to derive "
                     f"an advisory from; nothing done.")
        return
    candidate = refine._load_candidate(ticker)
    # The advisory's triggers are business events rather than price levels, but its
    # prose routinely refers to what the stock costs — one stated price, from the same
    # place every other document takes it, beats each agent finding its own.
    verified_figures = main._format_verified_figures(candidate, main._price_snapshot(ticker))
    quarterly_data = fmp_quarterly_trends(ticker)

    # 4. Its own cost row — see the module docstring for why this is not folded into
    # the target run's totals.
    advisory_run_id = str(uuid.uuid4())
    main._check_db(db_create_pipeline_run(advisory_run_id, [ticker]),
                   "create sale-advisory run")

    logger.info(f"[{ticker}] Running the sale advisor...")
    try:
        sale_data, usage = refine.run_sale_advisor(
            ticker, company_name, report_body, verified_figures, quarterly_data
        )
    except Exception as e:
        logger.error(f"[{ticker}] Sale advisor failed: {e}")
        main._finalize_run(advisory_run_id, main._new_usage(), "Sale Advisory Run",
                           status="FAILED")
        return

    cost = main._log_usage(f"{ticker} sale advisor", usage)
    if not sale_data.strip():
        logger.error(f"[{ticker}] Sale advisor produced no output; nothing stored. "
                     f"The attempt still cost ${cost:.4f}.")
        main._finalize_run(advisory_run_id, usage, "Sale Advisory Run", status="FAILED")
        return

    # 5. Same gate the pipeline runs over an advisory: its thresholds must not
    # contradict the figures they are supposed to be anchored to.
    recon = main._reconcile_agent_figures(sale_data, candidate, "Sale advisory")
    for f in recon:
        logger.warning(
            main.reconciliation_warning(ticker, f)
        )
    if not recon:
        logger.info(f"[{ticker}] Reconciliation gate passed on the new advisory.")

    stored = main._with_run_header(
        _origin_note(target_run, advisory_run_id) + sale_data, target_run, ticker
    )
    main._check_db(
        db_store_agent_output(
            target_run, ticker, "SALE_CASE", stored,
            json.dumps({"ticker": ticker, "origin": "regenerated_standalone",
                        "advisory_run_id": advisory_run_id,
                        "superseded_existing": bool(existing.get("found"))}),
        ),
        f"{ticker} sale advisory (standalone)",
    )
    out_path = os.path.join("reports", f"{ticker}_Sale_Advisory.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(stored)

    main._collect_embedding_usage(usage)
    logger.info(
        f"[{ticker}] Sale advisory stored on run {target_run} and saved to "
        f"{out_path}. `--sell-check {ticker} --run {target_run}` will now use it."
    )
    main._finalize_run(advisory_run_id, usage, "Sale Advisory Run")


def _origin_note(target_run: str, advisory_run_id: str) -> str:
    """Say where this advisory came from, since it is not the one the run produced.

    Without it, an advisory generated days after its report looks indistinguishable
    from one written alongside it — and the difference matters, because this one's
    thresholds are anchored to figures from the day it was regenerated, not from the
    day the report was written."""
    return (
        f"> **Generated separately from the report's own run.** Derived from the "
        f"report stored under run `{target_run}` by a later `sale_advisory.py` run "
        f"(`{advisory_run_id}`), and anchored to the figures current at that time — "
        f"not to the figures the report was written against. It supersedes any "
        f"advisory that run produced.\n\n"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate or regenerate the sale advisory for a stored report",
        epilog=(
            "The sale advisory names the measurable business events that would break a\n"
            "report's investment thesis. The pipeline writes one per run; this command\n"
            "writes one for any run, on demand.\n"
            "\n"
            "  python sale_advisory.py CROX                      from CROX's latest report\n"
            "  python sale_advisory.py CROX --run <RUN_ID>       from a SPECIFIC run's report\n"
            "  python sale_advisory.py CROX \"Crocs Inc\"          with an explicit company name\n"
            "\n"
            "Use it when a run has no advisory (--skip-sale-advisor was used, or the\n"
            "advisor produced nothing), when a critic refinement left one stale, or when\n"
            "you want an old advisory's thresholds re-anchored to current figures.\n"
            "\n"
            "Costs ~$0.08-0.10 against the rolling daily budget. The advisory is stored\n"
            "on the run you name, so --sell-check --run <that id> picks it up; the cost\n"
            "gets its own row so the named run's own totals are not rewritten."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("ticker", help="Ticker whose report the advisory is derived from")
    parser.add_argument("company", nargs="?", help="Optional company name")
    parser.add_argument("--run", metavar="RUN_ID", default=None,
                        help="Derive from a SPECIFIC run's report instead of the latest")
    parser.add_argument("--no-budget", action="store_true",
                        help="Ignore the rolling daily spend ceiling for this invocation")
    args = parser.parse_args()

    generate_sale_advisory(args.ticker, args.company, args.run, args.no_budget)
