"""One ticker, end to end: the reuse check, the data, the agents, the persistence.

This is the billed path. Everything before it decides WHICH tickers to spend on;
this decides what a single ticker costs and what is written down about it. In order:
normalise the candidate, check whether a previous run can be reused, gather the
deterministic data (no LLM, no tokens), run the reasoning graph, then persist —
report, verdict, reconciliation findings, ledger row.

`buy_case_agent` is imported inside the function, not at module scope. It imports
`main`, and `main` imports this package; the late import is what keeps that from
being a cycle at load time.
"""
import json
import logging
import os
import time
from datetime import datetime

from magic_formula_starter_screener import OUTPUT_FILENAME
from mcp_server import (
    compute_ticker_magic_metrics,
    db_copy_ticker_outputs,
    db_find_reusable_report,
    db_get_final_report,
    db_log_verdict,
    db_store_agent_output,
    db_store_final_report,
    db_store_ticker_run,
    fetch_sec_10k_data,
    fmp_metrics_extractor,
    fmp_quarterly_trends,
)
from persistence import _check_db
from report import (
    _format_magic_formula_section,
    _format_price_section,
    _format_reconciliation_section,
    _format_screen_context,
    _format_verified_figures,
    _normalize_candidate,
    _present,
    _price_snapshot,
    _reconcile_agent_figures,
    _verified_figures,
    reconciliation_warning,
)
from settings import (
    LEDGER_BENCHMARK,
    LEDGER_ENABLED,
    REUSE_ENABLED,
    REUSE_MAX_AGE_HOURS,
)
from usage import _collect_embedding_usage, _log_usage, _merge_usage, _new_usage

from .execution import run_pipeline
from .reuse import _analysis_key, _prompt_version
from .verdict import (
    _benchmark_price,
    _extract_verdict,
    _git_sha,
    _split_verdict_basis,
    _with_run_header,
)

logger = logging.getLogger("InvestmentAgentPipeline")


def analyze_ticker(run_id: str, ticker: str, company_name: str, screen_context: str = "",
                   candidate: dict = None, force: bool = False,
                   skip_sale_advisor: bool = False, skip_buy_case: bool = False) -> dict:
    """Run the Phase-B agent sequence for one ticker and persist its
    outputs and final report. Returns the ticker's token-usage dict (zero usage
    if the pipeline failed).

    `screen_context` carries the Magic Formula bull signal (rank / ROC / earnings
    yield) so the verdict can weigh it against the bear case. Empty for tickers
    that did not come through the screen (on-demand runs).

    `candidate` is the raw screener/on-demand metrics dict (EY_Pct, ROC_Pct,
    EBIT_Basis, Final_Rank, ...) used to deterministically render the '## Magic
    Formula Metrics' section of the final report -- kept separate from the LLM
    prompt so those figures are always present and correct even if the analyst
    agent's own writeup omits or misstates them.

    Assumes the parent pipeline_runs row for `run_id` already exists (the
    agent_outputs/final_reports tables have a FK onto it).
    """
    # Reconcile the two candidate shapes (screener CSV raw ratios vs single-ticker
    # percent strings) before anything reads them. Does not touch BalanceSheetDate,
    # so the reuse fingerprint below is unaffected.
    candidate = _normalize_candidate(candidate)

    # Reuse check FIRST, before any billed work. The fingerprint covers the ticker,
    # the balance-sheet date its figures came from, and the prompt version, so a new
    # filing or any prompt edit forces a fresh analysis.
    analysis_key = _analysis_key(ticker, candidate, skip_sale_advisor, skip_buy_case)
    if REUSE_ENABLED and not force and analysis_key:
        try:
            prior = json.loads(db_find_reusable_report(ticker, analysis_key, REUSE_MAX_AGE_HOURS))
        except Exception as e:
            logger.warning(f"[{ticker}] Reuse lookup failed ({e}); analyzing fresh.")
            prior = {"found": False}
        if prior.get("found"):
            logger.info(
                f"[{ticker}] Reusing the report from run {prior['run_id'][:8]} "
                f"({prior['age_hours']}h old, verdict {prior['verdict']}): same filing "
                f"({candidate.get('BalanceSheetDate')}) and same prompts, so a fresh run "
                f"would cost ~$0.37 to reproduce it. Use --force to re-analyze."
            )
            # Give THIS run its own rows. Indexing the ticker without copying the
            # report and agent outputs would list it in the web UI and then fail to
            # load anything for it.
            _check_db(
                db_copy_ticker_outputs(prior["run_id"], run_id, ticker),
                f"{ticker} copy reused outputs",
            )
            _check_db(
                db_store_ticker_run(run_id, ticker, company_name or ticker, prior["verdict"],
                                    _present((candidate or {}).get("Final_Rank")),
                                    # The REUSED report's price, not today's: this run
                                    # serves that document, price section and all.
                                    prior.get("share_price"), prior.get("price_as_of")),
                f"{ticker} ticker_run (reused)",
            )
            report_path = os.path.join("reports", f"{ticker}_Final_Report_{prior['verdict'].title()}.md")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(prior["markdown_report"])
            return _new_usage()

    logger.info(f"[{ticker}] Gathering SEC + metrics + quarterly trends (direct tool calls)...")
    # Pure data relays: call the deterministic tools directly (100% fidelity, 0 tokens).
    sec_data = fetch_sec_10k_data(ticker)
    metrics_data = fmp_metrics_extractor(ticker)
    # Quarterly trends are a SEPARATE feed from metrics_data, which is annual-only.
    # Without this the agents cannot see recent-quarter deterioration at all.
    quarterly_data = fmp_quarterly_trends(ticker)
    # One quote per ticker, shared by the report's price section, the agents'
    # VERIFIED_FIGURES, and (on a Watch) the buy case's triggers — see `_price_snapshot`.
    price = _price_snapshot(ticker)
    verified_figures = _format_verified_figures(candidate, price)

    # The reconciliation gate can only check figures it has ground truth for. A
    # rankings CSV written before those columns existed yields a candidate with no
    # debt/cash/EV, which would quietly disable the gate for that ticker. Say so
    # loudly: a guardrail that switches itself off without comment is the same
    # class of problem it was added to fix. Re-run the screener to refresh the CSV.
    missing = [k for k in ("TotalDebt", "Cash", "EnterpriseValue")
               if k not in _verified_figures(candidate)]
    if missing:
        logger.warning(
            f"[{ticker}] Incomplete verified figures (missing {', '.join(missing)}). "
            f"The reconciliation gate cannot check those figures for this ticker — "
            f"re-run the screener to regenerate {OUTPUT_FILENAME} with the full columns."
        )

    graph_desc = "bear -> bull -> analyst" if skip_sale_advisor else "bear -> bull -> analyst -> sale advisor"
    logger.info(f"[{ticker}] Running {graph_desc} graph...")
    try:
        state, usage = run_pipeline(
            run_id, ticker, company_name, sec_data, metrics_data, screen_context,
            quarterly_data, verified_figures, skip_sale_advisor,
        )
    except Exception as e:
        logger.error(f"[{ticker}] Skipping after pipeline failure: {e}")
        return _new_usage()

    _log_usage(ticker, usage)

    bear_data = state.get("bear_data", "") or ""
    bull_data = state.get("bull_data", "") or ""
    sale_data = state.get("sale_data", "") or ""
    report_text = state.get("final_report", "") or ""
    # Split off the machine-only basis block immediately, so every downstream
    # consumer — reconciliation gate, critic, stored report, web UI — sees only
    # the prose the analyst wrote for a human.
    report_text, verdict_factors = _split_verdict_basis(report_text, ticker)

    # Persist each step's raw output (spec section 5B). SEC/metrics are raw
    # provenance (reproducible, low search value) -> stored without an embedding.
    # Bear/bull/sale are the analytical content -> embedded for semantic search.
    _check_db(db_store_agent_output(run_id, ticker, "SEC_DATA", sec_data, json.dumps({"ticker": ticker}), embed=False), f"{ticker} SEC output")
    _check_db(db_store_agent_output(run_id, ticker, "QUANT_METRICS", metrics_data, json.dumps({"ticker": ticker}), embed=False), f"{ticker} metrics output")
    # The analytical reports carry a run_id banner (bear/bull/sale/final) so the
    # viewer shows the run_id you record against a lot on purchase. SEC/metrics are
    # raw provenance and are stored as-is.
    _check_db(db_store_agent_output(run_id, ticker, "BEAR_CASE", _with_run_header(bear_data, run_id, ticker), json.dumps({"ticker": ticker})), f"{ticker} bear case")
    _check_db(db_store_agent_output(run_id, ticker, "BULL_CASE", _with_run_header(bull_data, run_id, ticker), json.dumps({"ticker": ticker})), f"{ticker} bull case")
    # Phase C sale advisory (thesis-breaking events for an assumed owner).
    if sale_data:
        sale_stored = _with_run_header(sale_data, run_id, ticker)
        _check_db(db_store_agent_output(run_id, ticker, "SALE_CASE", sale_stored, json.dumps({"ticker": ticker})), f"{ticker} sale case")
        sale_path = os.path.join("reports", f"{ticker}_Sale_Advisory.md")
        with open(sale_path, "w", encoding='utf-8') as f:
            f.write(sale_stored)
    elif skip_sale_advisor:
        # Deliberate omission, not a failure. Said at INFO so a --skip-sale-advisor
        # run does not fill the log with warnings that look like something broke.
        logger.info(f"[{ticker}] Phase C skipped (--skip-sale-advisor): no SALE_CASE written. "
                    f"`--sell-check {ticker}` will have no conditions from this run to test.")
    else:
        logger.warning(f"[{ticker}] Sale advisor produced no output; skipping SALE_CASE persistence.")

    if not report_text:
        logger.error(f"[{ticker}] No final report produced; skipping report persistence.")
        _collect_embedding_usage(usage)
        return usage

    # Reconciliation gate. Check the model-written sections against the figures the
    # pipeline read from the filings itself. This runs AFTER generation rather than
    # constraining it, because the failure being guarded against (a fabricated
    # headline figure propagating bear -> bull -> verdict -> sell triggers) is only
    # visible once the prose exists.
    recon_findings = []
    for label, body in (
        ("Bear case", bear_data),
        ("Bull case", bull_data),
        ("Final report", report_text),
        ("Sale advisory", sale_data),
    ):
        recon_findings.extend(_reconcile_agent_figures(body, candidate, label))
    if recon_findings:
        for f in recon_findings:
            logger.warning(
                reconciliation_warning(ticker, f, with_context=True)
            )
        # Counted by basis, because the two mean different things: a filings
        # disagreement is an error in the report, a market one may be the clock.
        filed = sum(1 for f in recon_findings if f.get("basis") != "market price")
        moved = len(recon_findings) - filed
        parts = ([f"{filed} contradict the filings"] if filed else []) +                 ([f"{moved} disagree with the current market price"] if moved else [])
        logger.warning(
            f"[{ticker}] {len(recon_findings)} flagged figure(s) in this report: "
            f"{', '.join(parts)}. A warning table has been added to the report."
        )
    else:
        logger.info(f"[{ticker}] Reconciliation gate passed (no contradicted figures).")

    # Extract the verdict from the original text (banner/metrics section prepended
    # below don't affect the '## Final Verdict' split), then persist the report
    # with the deterministic Magic Formula Metrics section + run header prepended.
    verdict = _extract_verdict(report_text)
    # Price first, then the ratios derived from it, then the analyst's prose. The
    # order is the argument: what it costs, how that was turned into the screen's
    # figures, then what to make of it.
    report_with_metrics = (
        f"{_format_price_section(price, ticker)}\n"
        f"{_format_magic_formula_section(candidate)}\n{report_text}"
        f"{_format_reconciliation_section(recon_findings)}"
    )
    report_stored = _with_run_header(report_with_metrics, run_id, ticker)
    # Store the fingerprint alongside the report so a later run can reuse it.
    _check_db(db_store_final_report(run_id, ticker, verdict, report_stored, analysis_key),
              f"{ticker} final report")
    # Index this run under the ticker for the web UI. The screen rank rides along so
    # the run view can order by conviction; None for on-demand single-ticker runs.
    _check_db(
        db_store_ticker_run(run_id, ticker, company_name or ticker, verdict,
                            _present((candidate or {}).get("Final_Rank")),
                            price.get("price"), price.get("as_of")),
        f"{ticker} ticker_run",
    )

    # The verdict ledger. Deliberately AFTER the report is safely persisted and
    # deliberately not wrapped in _check_db: this is bookkeeping, and a ledger
    # failure must never cost a report that has already been paid for. Only the
    # fresh-analysis path reaches here — the reuse path returns long before this
    # point, which is what keeps a re-run ticker from entering the same
    # judgement into the track record twice.
    if LEDGER_ENABLED:
        try:
            db_log_verdict(
                run_id, ticker, verdict, json.dumps(verdict_factors),
                company_name=company_name or ticker,
                verdict_stage="INDEPENDENT",
                price=_present(price.get("price")) or 0.0,
                market_cap=_present((candidate or {}).get("LiveMarketCap")) or 0.0,
                shares_outstanding=_present((candidate or {}).get("SharesOutstanding")) or 0.0,
                sector=(candidate or {}).get("Sector") or "",
                industry=(candidate or {}).get("Industry") or "",
                magic_rank=int(_present((candidate or {}).get("Final_Rank")) or 0),
                benchmark_symbol=LEDGER_BENCHMARK,
                benchmark_price=_benchmark_price(),
                prompt_version=_prompt_version(),
                git_sha=_git_sha(),
                analysis_key=analysis_key or "",
            )
        except Exception as e:
            logger.warning(f"[{ticker}] Verdict ledger write failed ({e}); the "
                           f"analysis is unaffected.")

    report_path = os.path.join("reports", f"{ticker}_Final_Report_{verdict.title()}.md")
    with open(report_path, "w", encoding='utf-8') as f:
        f.write(report_stored)

    # Phase E: the buy case, for a WATCH verdict only. It runs here rather than inside
    # the reasoning graph because the verdict it is conditional on does not exist
    # until `_extract_verdict` has read the analyst's finished prose, which is a
    # Python decision taken after the graph has finished. The report is already
    # persisted at this point, so a failure here costs the buy case and nothing else.
    #
    # The import is deferred on purpose: `buy_case_agent` imports this module for the
    # shared cost/logging plumbing (as `critic_agent` does), so importing it at the
    # top of this file would be a cycle. See the `sys.modules` alias at the top for
    # why `import main` inside it resolves back here rather than re-running this file.
    import buy_case_agent
    if buy_case_agent.is_watch(verdict) and not skip_buy_case:
        buy_case_agent.write_buy_case(
            run_id, ticker, company_name or ticker, report_text, verified_figures,
            quarterly_data, candidate, usage,
            metadata={"origin": "pipeline", "verdict": verdict},
            # The same quote the report's price section was printed from, so the buy
            # triggers cannot be measured against a different one.
            price_data=buy_case_agent.price_data_block(ticker, price),
        )
    elif buy_case_agent.is_watch(verdict):
        logger.info(f"[{ticker}] Verdict is Watch, but Phase E was skipped "
                    f"(--skip-buy-case): no BUY_CASE written. `--buy-check {ticker}` "
                    f"will have no entry conditions from this run to test.")
    else:
        logger.info(f"[{ticker}] Verdict is {verdict}, not Watch — no buy case is "
                    f"written. A Buy needs no entry conditions and an Avoid should "
                    f"not be given any.")

    # Fold in the embedding calls made by the db_store_* tools above.
    _collect_embedding_usage(usage)

    logger.info(f"[{ticker}] Completed Analysis ({verdict}) and saved to {report_path}")
    return usage
