import os
import re
import sys
import time
import json
import asyncio
import hashlib
import logging
import subprocess
from datetime import datetime
import uuid
import yaml
import pandas as pd
from dotenv import load_dotenv

# This module is the CLI entry point, and now ONLY that. It used to be the shared
# plumbing as well — `refine`, `buy_case`, `buy_case_agent`, `sale_advisory` and
# `critic_agent` all did `import main` to reach the agents, the cost accounting and
# the configured logger — and that made it a module which imports its own importers.
#
# Run as `python main.py`, this file is loaded under the name `__main__`, so a later
# `import main` did not find it in `sys.modules` and EXECUTED THE WHOLE FILE A SECOND
# TIME under a second name: a second read of the config, a second set of agent
# objects, and a second StreamHandler and FileHandler bolted onto the same logger
# singleton — every log line printed twice, the run split across two files. There was
# a `sys.modules.setdefault("main", sys.modules[__name__])` here that aliased the two
# names so `import main` resolved to this very module.
#
# It is gone because the cycle is gone: everything shared lives in `settings`,
# `agents`, `usage`, `report`, `pipeline`, `persistence` and `run_log`, and those
# modules import each other, never this one. Nothing on the import graph reaches back
# here, so there is no second execution to defend against. `run_log` keeps its own
# guard against double-configuring the logger, which is the one piece of that damage
# that could still be caused another way.
from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Import custom MCP tool wrappers
from mcp_server import (
    run_magic_formula_screener,
    compute_ticker_magic_metrics,
    check_ticker_screen_gates,
    fetch_sec_10k_data,
    fmp_metrics_extractor,
    fmp_company_profile,
    fmp_quarterly_trends,
    fmp_price_snapshot,
    fmp_stock_news,
    web_search_tool,
    db_create_pipeline_run,
    db_update_pipeline_status,
    db_finalize_pipeline_run,
    db_store_agent_output,
    db_store_final_report,
    db_store_ticker_run,
    db_get_sale_case,
    db_get_buy_case,
    db_get_final_report,
    db_find_reusable_report,
    db_copy_ticker_outputs,
    db_spend_since,
    db_log_run_universe,
    db_log_verdict,
    drain_embedding_usage,
)
# The screener's CSV output path — the default source for --from-csv mode. The
# dollar formatter and the PEG ceiling used to be imported here too; they went with
# the report code to `report/`, which is the only thing that ever rendered them.
from magic_formula_starter_screener import OUTPUT_FILENAME

load_dotenv()

# --- 1. LOGGER SETUP ---
# Console + logs/run_<stamp>.log, configured in `run_log` so that every entry point
# can have it without importing this one. That used to be the reason `refine`,
# `buy_case` and `sale_advisory` did `import main` at all.
from run_log import log_filename, logger  # noqa: F401  (log_filename re-exported)

# Configuration — everything from specs/config.yaml, plus the constants tuned
# alongside it — lives in `settings`. Imported here rather than read here, and
# re-exported, because `refine`, `buy_case`, `sale_advisory`, `critic_agent`,
# `buy_case_agent` and the tests read these as `main.NAME`.
#
# Imported AFTER the logger setup above, deliberately: settings logs an error when
# the ledger is enabled with an empty factor taxonomy, and that error should reach
# the run log rather than the bare-logger fallback.
from settings import (  # noqa: F401  (re-exported for `main.NAME` callers)
    AGENT_MODEL,
    BASE_BACKOFF_SECONDS,
    BUDGET_DAY_WINDOW_HOURS,
    BUDGET_ENABLED,
    BUDGET_ON_EXCEED,
    BUDGET_PER_DAY_USD,
    BUDGET_PER_RUN_USD,
    BudgetExceeded,
    EMBED_MODEL,
    EMBED_PRICE_INPUT_PER_1M,
    INTER_AGENT_DELAY_SECONDS,
    INTER_CALL_DELAY_SECONDS,
    LEDGER,
    LEDGER_BENCHMARK,
    LEDGER_ENABLED,
    LEDGER_FACTOR_TAXONOMY,
    MAX_AGENT_RETRIES,
    MODEL_PRICES,
    REUSE_ENABLED,
    REUSE_MAX_AGE_HOURS,
    TAVILY_PRICE_PER_1K,
    _UNCONFIRMED_WARNED,
    _UNPRICED_MODELS,
    config,
    top_n,
)

# --- 2. AGENT DEFINITIONS (Phase B) ---
# The five agents, the prompt fragments they share, and the Runner that executes
# them are in `agents/`. Re-exported here because `critic_agent`, `refine`,
# `buy_case_agent` and three test modules read them as `main.NAME` — and because
# `_prompt_version` below hashes PIPELINE_AGENTS' assembled instructions into every
# report's reuse fingerprint, so this module remains where that fingerprint is
# defined even though the text it hashes now lives next door.
from agents import (  # noqa: F401  (re-exported for `main.NAME` callers)
    APP_NAME,
    CONTEXT_CACHE,
    CONTEXT_CACHE_ENABLED,
    PIPELINE_AGENTS,
    PIPELINE_INCLUDE_CONTENTS,
    RECENCY_MANDATE,
    REFERENCE_DATA_BLOCKS,
    RESEARCH_SOURCE_BLOCKS,
    VERDICT_BASIS_MANDATE,
    VERIFIED_FIGURES_MANDATE,
    _build_runner,
    _is_rate_limit_error,
    analyst_agent,
    bear_agent,
    bull_agent,
    sale_advisor_agent,
    sell_check_agent,
)


# --- Token usage / cost accounting ---
# Counting tokens, pricing them, and the ceiling that stops a run — all in `usage`.
# Re-exported here because `refine`, `buy_case`, `buy_case_agent`, `sale_advisory`,
# `critic_agent` and the tests call them as `main._new_usage`, `main._log_usage`,
# `main._check_budget`.
from usage import (  # noqa: F401  (re-exported for `main._name` callers)
    _add_event_usage,
    _check_budget,
    _collect_embedding_usage,
    _embedding_cost,
    _finalize_run,
    _llm_cost,
    _log_usage,
    _merge_usage,
    _model_bucket,
    _model_cost,
    _model_price,
    _new_usage,
    _prior_day_spend,
    _search_cost,
    _total_cost,
)




# --- 3. THE RUN ITSELF ---
# The reuse fingerprint, the agent execution loop, verdict parsing, and
# `analyze_ticker` — one ticker end to end — are in `pipeline/`. What is left in this
# file is the CLI and the workflows that decide WHICH tickers to run; that is the
# seam the split follows.
#
# Re-exported because `refine`, `buy_case`, `buy_case_agent`, `sale_advisory` and
# four test modules reach for these as `main._name`, and because
# `main._prompt_version()` is the reuse fingerprint's public address.
#
# `_DOWNSTREAM_PROMPTS` is deliberately NOT re-exported. It is the one value here
# that something reassigns at runtime (a test, to prove a prompt edit invalidates
# reuse), and a re-exported copy would accept the assignment while the fingerprint
# went on reading the original — the change would appear to work and do nothing.
# Reach for it at `pipeline.reuse._DOWNSTREAM_PROMPTS`, where it lives.
from pipeline import (  # noqa: F401  (re-exported for `main._name` callers)
    _BENCHMARK_CACHE,
    _JSON_FENCE_RE,
    _VERDICT_BASIS_RE,
    _analysis_key,
    _benchmark_price,
    _extract_verdict,
    _git_sha,
    _prompt_version,
    _read_prompt_files,
    _run_pipeline_async,
    _run_sell_check,
    _run_sell_check_async,
    _split_verdict_basis,
    analyze_ticker,
    run_pipeline,
)

# Reading the status string a db_* tool hands back lives in `persistence.results`,
# next to the tools whose return contract it interprets. Re-exported because
# `refine`, `buy_case`, `buy_case_agent` and `sale_advisory` call it as
# `main._check_db`.
from persistence import _check_db  # noqa: F401  (re-exported)


# The report itself — every deterministic section, the VERIFIED_FIGURES block the
# agents are given, and the gate that checks their prose against it — lives in
# `report/`. It was ~1,180 lines sitting in the middle of this file, between the
# per-ticker analysis above and the orchestrator workflows below, related to
# neither: it turns a candidate dict into text and touches no pipeline state.
#
# Imported flat and re-exported, because `refine`, `buy_case`, `buy_case_agent`,
# `sale_advisory` and the tests all reach for these as `main._name`. Keeping that
# surface intact is what let the move be verified rather than merely reviewed —
# `tools/snapshot_report_surface.py` renders every function over a fixture corpus,
# and before/after diffed empty.
from report import (  # noqa: F401  (re-exported for `main._name` callers)
    RECONCILE_TOLERANCE,
    _FIELD_TOLERANCE,
    _MARKET_DERIVED_FIELDS,
    _MULTIPLIER,
    _RATIO_TO_PCT_FIELD,
    _RECONCILED_FIELDS,
    _RECONCILIATION_HEADING,
    _amount_for_label,
    _eps_window_phrase,
    _finding_basis,
    _format_magic_formula_section,
    _format_price_section,
    _format_reconciliation_section,
    _format_screen_context,
    _format_verified_figures,
    _growth_window,
    _line_bounds,
    _no_peg_reason,
    _normalize_candidate,
    _present,
    _price_snapshot,
    _reconcile_agent_figures,
    _verified_figures,
    _with_run_header,
    reconciliation_warning,
)


# --- 4. ORCHESTRATOR WORKFLOWS ---
def _run_phase_b(top_candidates: list, source_label: str, force: bool = False,
                 skip_sale_advisor: bool = False, skip_buy_case: bool = False):
    """Create a pipeline run and analyze each candidate through Phase B.
    Shared by the full-pipeline and CSV modes."""
    run_id = str(uuid.uuid4())
    logger.info(f"Starting Workflow Run: {run_id} (candidates from {source_label})")

    # Parent pipeline_runs row BEFORE any agent_outputs/final_reports inserts —
    # those tables have a foreign key onto pipeline_runs(run_id).
    tickers = [c.get("Symbol") for c in top_candidates if c.get("Symbol")]
    _check_db(db_create_pipeline_run(run_id, tickers), "create pipeline run")

    # Snapshot the screen's own output before any agent runs. It costs nothing
    # (every figure is already computed) and it is otherwise unrecoverable:
    # magic_formula_rankings_live.csv is overwritten next run, and
    # pipeline_runs.top_30_tickers keeps only the bare symbols. Written for the
    # WHOLE candidate list, including names a budget stop may never reach, so
    # the ledger has a control group to measure the agents' filtering against.
    if LEDGER_ENABLED:
        try:
            logger.info(db_log_run_universe(run_id, json.dumps(top_candidates, default=str)))
        except Exception as e:
            logger.warning(f"Universe ledger write failed ({e}); the run is unaffected.")

    prior_day_usd = _prior_day_spend() if BUDGET_ENABLED else 0.0
    if BUDGET_ENABLED:
        logger.info(
            f"Budget: ${BUDGET_PER_RUN_USD:.2f}/run, ${BUDGET_PER_DAY_USD:.2f}/"
            f"{BUDGET_DAY_WINDOW_HOURS}h (${prior_day_usd:.2f} already spent in that "
            f"window), on_exceed={BUDGET_ON_EXCEED}."
        )

    run_usage = _new_usage()
    completed = 0
    halted = None
    for idx, candidate in enumerate(top_candidates):
        ticker = candidate.get("Symbol")
        company_name = candidate.get("CompanyName")
        try:
            _check_budget(run_usage, prior_day_usd, f"{ticker} ({idx + 1}/{len(top_candidates)})")
        except BudgetExceeded as e:
            halted = str(e)
            logger.error(f"BUDGET: {halted}")
            break
        logger.info(f"--- Processing {idx+1}/{len(top_candidates)}: {ticker} ({company_name}) ---")
        screen_context = _format_screen_context(candidate)
        _merge_usage(run_usage,
                     analyze_ticker(run_id, ticker, company_name, screen_context, candidate,
                                    force, skip_sale_advisor, skip_buy_case))
        completed += 1

    if halted:
        logger.warning(
            f"Stopped after {completed}/{len(top_candidates)} tickers. Everything "
            f"analyzed so far is saved under run {run_id}."
        )
    _finalize_run(run_id, run_usage, "Workflow Run",
                  status="BUDGET_EXCEEDED" if halted else "COMPLETED")


def run_orchestrator(force: bool = False, skip_sale_advisor: bool = False,
                     skip_buy_case: bool = False):
    """Full pipeline: Phase A screener -> Phase B analysis over the Top N."""
    # Phase A: Screener. We invoke the screener tool directly (deterministic JSON);
    # this also writes the rankings CSV that --from-csv can reuse later.
    logger.info("Executing Phase A: Screener...")
    try:
        top_candidates = json.loads(run_magic_formula_screener())
    except Exception as e:
        logger.error(f"Failed to get screener results: {e}")
        return

    top_candidates = top_candidates[:top_n]
    logger.info(f"Phase A Complete. Retrieved {len(top_candidates)} candidates.")
    _run_phase_b(top_candidates, "Phase A screener", force, skip_sale_advisor, skip_buy_case)


def run_screen_only():
    """Phase A ONLY: run the screener, write the rankings CSV, and stop.

    The inverse of `--from-csv`. No LLM calls, no database writes, no pipeline_runs
    row — so nothing appears in the web UI and nothing is billed. FMP is
    subscription-metered rather than per-call, so this mode costs time and nothing
    else, which makes it the one to run on a watching cadence: refresh the rankings
    as often as you like, and pay for Phase B/C only in the periods you actually act
    on the list (`python main.py --from-csv`).
    """
    logger.info("Executing Phase A ONLY (screen-only): no agents, no database writes.")
    try:
        candidates = json.loads(run_magic_formula_screener())
    except Exception as e:
        logger.error(f"Failed to get screener results: {e}")
        return

    if not candidates:
        logger.error("Screener returned no candidates. Nothing written.")
        return

    logger.info(f"Phase A complete: {len(candidates)} candidates ranked. "
                f"Rankings CSV: {OUTPUT_FILENAME}")
    # Informational, not a warning. `--from-csv` uses `df.head(top_n)`, so a short list
    # is analysed IN FULL rather than failing — top_n is a ceiling, never a quota. The
    # operator still wants to know before scheduling a paid run, because a short list
    # usually means a gate is mistuned.
    if len(candidates) < top_n:
        logger.info(
            f"{len(candidates)} candidates cleared the screen, fewer than the "
            f"top_n_candidates={top_n} Phase B would analyse. That is fine — Phase B "
            f"will analyse all {len(candidates)}. If you expected more, review the "
            f"eligibility gates under `screening_parameters:` in specs/config.yaml "
            f"(min_roa is the strictest by a wide margin)."
        )

    header = (f"{'Rank':>4}  {'Symbol':<8} {'ROC':>10} {'EarnYld':>9} {'ROA':>9} "
              f"{'P/E':>7} {'EPS gr':>9} {'PEG':>6}")
    logger.info(f"Top {min(top_n, len(candidates))} candidates:")
    logger.info(header)
    for c in candidates[:top_n]:
        pe = c.get("PE_Ratio")
        peg = c.get("PEG_Ratio")
        logger.info(
            f"{str(c.get('Final_Rank', '')):>4}  {str(c.get('Symbol', '')):<8} "
            f"{str(c.get('ROC_Pct', '')):>10} {str(c.get('EY_Pct', '')):>9} "
            f"{str(c.get('ROA_Pct', '') or '-'):>9} "
            f"{(f'{pe:.1f}' if isinstance(pe, (int, float)) else '-'):>7} "
            f"{str(c.get('EPSGrowth_Pct', '') or '-'):>9} "
            f"{(f'{peg:.2f}' if isinstance(peg, (int, float)) else '-'):>6}"
        )
    logger.info("Screen-only run finished. To analyze these, run: python main.py --from-csv")


def run_from_csv(csv_path: str = None, force: bool = False,
                 skip_sale_advisor: bool = False, skip_buy_case: bool = False):
    """Skip Phase A: load the screener's rankings CSV from a previous run and run
    Phase B over the top N. Phase A (the full FMP universe scan) is slow, so this
    reuses its output when you just want to (re)run the analysis."""
    csv_path = csv_path or OUTPUT_FILENAME
    logger.info(f"Skipping Phase A. Loading candidates from '{csv_path}'...")
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        logger.error(f"Failed to read CSV '{csv_path}': {e}")
        return

    if "Final_Rank" in df.columns:
        df = df.sort_values("Final_Rank")
    top_candidates = df.head(top_n).to_dict(orient="records")
    if not top_candidates:
        logger.error(f"No candidates found in '{csv_path}'.")
        return

    logger.info(f"Loaded top {len(top_candidates)} candidates from CSV.")
    _run_phase_b(top_candidates, f"CSV '{csv_path}'", force, skip_sale_advisor, skip_buy_case)


def run_gate_check(ticker: str):
    """Check a single ticker against the screen's eligibility gates. No analysis,
    no money, no DB writes — just PASS/FAIL against the same gates the screener
    applies, with the exact rejecting gate on a failure.

    Prints the result as JSON (the operator MCP tool shells out and reads stdout).
    Reuses `check_ticker_screen_gates` from the agents' toolbelt so this can never
    disagree with the full screen's own logic.
    """
    ticker = ticker.strip().upper()
    logger.info(f"Checking {ticker} against screen eligibility gates...")
    result = json.loads(check_ticker_screen_gates(ticker))
    # Print as a SINGLE line of compact JSON for the caller (operator MCP tool) to
    # parse. The logger writes to stderr, so stdout carries only this line.
    print(json.dumps(result, separators=(",", ":")))
    return result


def run_single_ticker(ticker: str, company_name: str = None, force: bool = False,
                      skip_sale_advisor: bool = False, skip_buy_case: bool = False):
    """On-demand Phase B: run the skeptical analysis for a single arbitrary
    ticker, bypassing Phase A. Logged as its own one-company pipeline run."""
    ticker = ticker.strip().upper()
    company_name = company_name or ticker
    run_id = str(uuid.uuid4())
    logger.info(f"Starting On-Demand Run: {run_id} for {ticker} ({company_name})")

    # A single ticker cannot breach the per-run ceiling, but it can push the rolling
    # window over. Check once, up front, rather than after paying for it.
    if BUDGET_ENABLED:
        try:
            _check_budget(_new_usage(), _prior_day_spend(), f"{ticker}")
        except BudgetExceeded as e:
            logger.error(f"BUDGET: {e}")
            return

    # Parent pipeline_runs row for just this company (FK target for outputs).
    _check_db(db_create_pipeline_run(run_id, [ticker]), "create pipeline run")

    # The screener didn't run for a single ticker, so compute ROC + Earnings Yield
    # on demand (the bull agent needs them). Fall back gracefully if unavailable.
    logger.info(f"[{ticker}] Computing Magic Formula ROC / Earnings Yield...")
    try:
        candidate = json.loads(compute_ticker_magic_metrics(ticker))
    except Exception as e:
        candidate = {"error": str(e)}
    if candidate.get("error") or "ROC_Pct" not in candidate:
        reason = candidate.get("reason") or "unknown"
        logger.warning(f"[{ticker}] Could not compute ROC/EY (reason={reason}); proceeding without the bull signal.")
        # The reason is usually a substantive fact about the business (e.g. it is
        # loss-making), not a data outage — pass it to the agents so the bear/bull
        # cases can weigh it instead of silently ignoring a missing signal.
        screen_context = (
            "This ticker was analyzed on demand and its Magic Formula ROC/Earnings Yield "
            "could NOT be computed, so no value/quality (bull) signal is available. "
            f"Reason: {candidate.get('message') or 'not determinable from the available financial data.'}\n"
            "Treat this as a material fact about the company, not a data glitch, and base "
            "the bull case on the fundamentals and research below."
        )
    else:
        candidate.setdefault("CompanyName", company_name)
        screen_context = _format_screen_context(candidate)

    # on_demand=True: the screen did not choose this name, so its verdict is
    # logged as ONDEMAND rather than PIPELINE. This run writes no run_universe
    # rows, and Q1 measures approvals against exactly those rows.
    usage = analyze_ticker(run_id, ticker, company_name, screen_context, candidate, force,
                           skip_sale_advisor, skip_buy_case, on_demand=True)

    _finalize_run(run_id, usage, "On-Demand Run")


def _extract_recommendation(text: str) -> str:
    """Pull SELL/HOLD from the sell-check report's 'Recommendation:' line."""
    m = re.search(r"recommendation[^A-Za-z]{0,12}(sell|hold)", text, re.IGNORECASE)
    return m.group(1).upper() if m else "UNKNOWN"


def run_sell_check(ticker: str, company_name: str = None, run_id: str = None):
    """On-demand sell-condition check for a single stock. Loads a stored SALE_CASE
    (the sale advisor's measurable sell triggers) and tests, against CURRENT data,
    whether any/all are now met — advising Sell vs. Hold for someone who already owns
    the stock.

    `run_id` pins the check to a SPECIFIC run's SALE_CASE — use the run you purchased
    under, so a held position is evaluated against the exact thesis it was bought
    under (sale conditions are exit criteria for a specific entry thesis; see
    specs/agent_architecture.md §8.D). When omitted, the most recent SALE_CASE is used.

    This is a lightweight flow: it reads the prior sale conditions from the DB and
    writes a local report file, but does NOT create a pipeline_runs / ticker_runs
    record (so it never appears in the web UI's run lists)."""
    ticker = ticker.strip().upper()
    company_name = company_name or ticker
    pin = f" (pinned to run {run_id})" if run_id else ""
    logger.info(f"Starting Sell-Condition Check for {ticker} ({company_name}){pin}")

    # 1. Load the sale-advisory conditions (a specific run's, or the most recent).
    data = json.loads(db_get_sale_case(ticker, run_id or ""))
    if data.get("error"):
        logger.error(f"[{ticker}] {data['error']}")
        return
    sale_conditions = data.get("sale_conditions", "") or ""
    logger.info(
        f"[{ticker}] Loaded sale conditions from run {data.get('run_id')} "
        f"(dated {data.get('created_at')})."
    )

    # 2. Gather CURRENT fundamentals (direct tool call — 100% fidelity, 0 tokens).
    # Quarterly trends alongside the annual metrics: sale conditions are usually
    # written in quarterly terms ("two consecutive quarters of..."), which annual
    # figures cannot answer either way.
    logger.info(f"[{ticker}] Fetching current FMP metrics + quarterly trends...")
    metrics_data = fmp_metrics_extractor(ticker)
    quarterly_data = fmp_quarterly_trends(ticker)

    # 3. Evaluate the conditions against current data + live news/web research.
    logger.info(f"[{ticker}] Evaluating sale conditions against current data...")
    try:
        state, usage = _run_sell_check(
            ticker, company_name, sale_conditions, metrics_data, quarterly_data,
        )
    except Exception as e:
        logger.error(f"[{ticker}] Sell-condition check aborted: {e}")
        return

    _log_usage(f"{ticker} sell-check", usage)
    result = state.get("sell_check", "") or ""
    if not result:
        logger.error(f"[{ticker}] Sell-condition check produced no output.")
        return

    out_path = os.path.join("reports", f"{ticker}_Sell_Check.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)

    recommendation = _extract_recommendation(result)
    logger.info(f"[{ticker}] Sell-condition check complete — Recommendation: {recommendation}. Saved to {out_path}")


def run_buy_check(ticker: str, company_name: str = None, run_id: str = None,
                  origin: str = "manual", escalation_reason: str = ""):
    """On-demand buy-condition check for a single stock. The `--sell-check` of the
    other side: loads a stored BUY_CASE (the entry price range and buy triggers
    written when the verdict was Watch) and tests, against CURRENT data, whether they
    are now met — advising Buy vs. Wait for someone who does NOT own the stock.

    `run_id` pins the check to a SPECIFIC run's BUY_CASE. Unlike a sale advisory,
    which is pinned to the run you PURCHASED under, the natural default here is the
    most recent buy case — you are deciding today, against the newest reading of the
    company. Pin a run when you want to test the conditions a particular report set,
    for instance to see whether a case you rejected in March would have fired since.

    Lightweight, like the sell-check: it creates no pipeline_runs / ticker_runs
    record, so it never appears in the web UI's RUN LISTS. It does store the check
    itself against the run whose buy case it tested, which is what gives the Buy
    Check tab its history — the local report file is overwritten by the next check
    and so cannot answer when a trigger first fired.

    `origin` and `escalation_reason` are provenance for the weekly sweep
    (cron/run_buy_check_sweep.py), which reaches here only for tickers its
    pre-filter escalated. Returns the recommendation string, or None if the check
    could not be completed."""
    ticker = ticker.strip().upper()
    company_name = company_name or ticker
    pin = f" (pinned to run {run_id})" if run_id else ""
    logger.info(f"Starting Buy-Condition Check for {ticker} ({company_name}){pin}")

    # Deferred import, though no longer to break a cycle — `buy_case_agent` imports
    # from `agents`, `usage` and `report` now, not from here. It stays deferred
    # because it pulls in ADK and the Gemini SDK, and doing that first, before the
    # database round-trip, means a broken prompt file or a bad import fails
    # immediately rather than after the lookup has already succeeded.
    import buy_case_agent

    # 1. The stored buy case (a specific run's, or the most recent).
    data = json.loads(db_get_buy_case(ticker, run_id or ""))
    if data.get("error"):
        logger.error(f"[{ticker}] {data['error']}")
        return
    buy_conditions = data.get("buy_conditions", "") or ""
    logger.info(
        f"[{ticker}] Loaded buy conditions from run {data.get('run_id')} "
        f"(dated {data.get('created_at')})."
    )

    # A buy case is only ever written for a Watch, so its own run's verdict is always
    # Watch. What can have changed is the LATEST view of the company: a report written
    # since may have moved to Buy (the wait is over by the analyst's own reckoning) or
    # to Avoid (the triggers are testing a case the pipeline has since abandoned).
    # Warn rather than refuse — the operator asked for these conditions to be checked,
    # and the newer verdict is information, not an error.
    try:
        latest = json.loads(db_get_final_report(ticker, ""))
    except Exception:
        latest = {"found": False}
    latest_verdict = (latest.get("verdict") or "").upper()
    if latest.get("found") and latest_verdict and latest_verdict != "WATCH":
        logger.warning(
            f"[{ticker}] The most recent report (run {latest.get('run_id')}, "
            f"{latest.get('age_hours')}h old) reaches a verdict of {latest_verdict}, "
            f"not Watch. These buy conditions were written for an older Watch report "
            f"and are being checked as asked — but read that newer report before "
            f"acting on the result."
        )

    # 2. Current data, by direct call (100% fidelity, 0 tokens). The price is the
    # authority for the price trigger and is fetched here rather than left to a tool
    # call, exactly as it is when the buy case is written.
    logger.info(f"[{ticker}] Fetching current price + FMP metrics + quarterly trends...")
    # Fetched as a dict first, then rendered, so the price can be recorded on the
    # stored check alongside the recommendation. Re-quoting for the record would
    # risk stamping a check with a price a minute away from the one it actually
    # tested — the single number the whole exercise turns on.
    try:
        price_snapshot = json.loads(buy_case_agent.fmp_price_snapshot(ticker))
    except Exception as e:
        logger.warning(f"[{ticker}] Price snapshot failed ({e}).")
        price_snapshot = {"error": str(e)}
    price_data = buy_case_agent.price_data_block(ticker, price_snapshot)
    metrics_data = fmp_metrics_extractor(ticker)
    quarterly_data = fmp_quarterly_trends(ticker)

    # 3. Evaluate the conditions against current data + live news/web research.
    logger.info(f"[{ticker}] Evaluating buy conditions against current data...")
    try:
        result, usage = buy_case_agent.run_buy_check(
            ticker, company_name, buy_conditions, metrics_data, quarterly_data, price_data,
        )
    except Exception as e:
        logger.error(f"[{ticker}] Buy-condition check aborted: {e}")
        return

    _log_usage(f"{ticker} buy-check", usage)
    if not result:
        logger.error(f"[{ticker}] Buy-condition check produced no output.")
        return

    out_path = os.path.join("reports", f"{ticker}_Buy_Check.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)

    recommendation = buy_case_agent.extract_buy_recommendation(result)

    # Persist the check, not just the file. The file is overwritten by the next
    # check, so on its own it answers "what does this say today" and nothing
    # else — no history of when a trigger first fired, no record of the
    # reasoning behind an alert, and nothing the verdict ledger could ever grade.
    # "Our triggers fired and then the stock did X" is the fastest-arriving
    # accuracy signal this system can produce, and it needs the checks kept.
    #
    # Stored against the run whose buy case was TESTED, which is what pins a
    # check to the conditions it was checking. Repeated checks of the same buy
    # case accumulate as separate rows — that is the history, so nothing here
    # deduplicates.
    #
    # No embedding: these are operational records read by ticker and date, never
    # by semantic similarity, and a weekly sweep would otherwise add an embedding
    # call per ticker per week forever.
    if data.get("run_id"):
        _check_db(
            db_store_agent_output(
                data["run_id"], ticker, "BUY_CHECK",
                _with_run_header(result, data["run_id"], ticker),
                json.dumps({
                    "ticker": ticker,
                    # Denormalised so the ledger and the web UI can filter and
                    # count without re-parsing markdown prose.
                    "recommendation": recommendation,
                    "checked_on": datetime.now().date().isoformat(),
                    "price_at_check": _present(price_snapshot.get("price")),
                    # "manual" for `--buy-check`, "sweep" for the weekly job.
                    # Kept because the two have different selection biases: the
                    # sweep only checks tickers its pre-filter escalated, so a
                    # base rate computed over both without distinguishing them
                    # would be measuring two different populations.
                    "origin": origin,
                    "escalation_reason": escalation_reason or None,
                }),
                embed=False,
            ),
            f"{ticker} buy check",
        )

    logger.info(f"[{ticker}] Buy-condition check complete — Recommendation: "
                f"{recommendation}. Saved to {out_path}")
    # Returned so a caller (the weekly sweep) does not have to read the report
    # file back off disk to learn the answer it just paid for.
    return recommendation


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Magic Formula Skeptical Decomposer",
        epilog=(
            "Modes:\n"
            "  python main.py                     full pipeline (Phase A screener + Phase B + Phase C)\n"
            "  python main.py --screen-only       Phase A ONLY: refresh the rankings CSV, no agents, no cost\n"
            "  python main.py --from-csv          skip Phase A; run Phase B/C on top N from the rankings CSV\n"
            "  python main.py --from-csv PATH     same, from a specific CSV file\n"
            "  python main.py --from-csv --top-n 12   analyze only the top 12 (cheaper; ~$0.37/ticker)\n"
            "  python main.py TICKER [Company]    on-demand Phase B/C for one ticker\n"
            "  python main.py --sell-check TICKER [Company]            test if TICKER's latest sale conditions are now met (Sell/Hold)\n"
            "  python main.py --sell-check TICKER --run RUN_ID         same, but against a SPECIFIC run's conditions (the run you bought under)\n"
            "  python main.py --buy-check TICKER [Company]             test if TICKER's buy conditions are now met (Buy/Wait)\n"
            "  python main.py --buy-check TICKER --run RUN_ID          same, but against a SPECIFIC run's buy case\n"
            "\n"
            "\n"
            "  python refine.py TICKER             (separate command) put an existing report\n"
            "                                     through independent critic review\n"
            "  python sale_advisory.py TICKER      (separate command) generate or repair one\n"
            "                                     report's sale advisory\n"
            "  python buy_case.py TICKER           (separate command) generate or repair one\n"
            "                                     Watch report's buy case\n"
            "\n"
            "Phase A is subscription-metered (time only); Phase B/C/E is the billed part.\n"
            "--screen-only and --from-csv are inverses, so the two can run on different\n"
            "cadences: screen often, analyze only when you intend to act on the list.\n"
            "--skip-sale-advisor drops Phase C from any Phase B run (~1/4 of the cost).\n"
            "Phase E (the buy case: entry price range + buy triggers) runs automatically\n"
            "for a ticker whose verdict is WATCH, and only then; --skip-buy-case drops it.\n"
            "Re-running --from-csv with a larger --top-n within the reuse window bills\n"
            "only the NEW tickers: the ones already analyzed are reused for ~$0, so you\n"
            "can start shallow and deepen without paying twice for the same work."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("ticker", nargs="?", help="On-demand analysis for a single ticker (skips Phase A)")
    parser.add_argument("company", nargs="?", help="Optional company name for the single-ticker run")
    parser.add_argument(
        "--from-csv", nargs="?", const=OUTPUT_FILENAME, metavar="PATH", default=None,
        help=f"Skip Phase A; read candidates from the screener CSV (default: {OUTPUT_FILENAME}) and run Phase B/C on the top N",
    )
    parser.add_argument(
        "--top-n", type=int, metavar="N", default=None,
        help=f"How many ranked candidates to analyze this run, overriding "
             f"agents.screener_agent.top_n_candidates (currently {top_n})",
    )
    parser.add_argument(
        "--screen-only", action="store_true",
        help="Run Phase A only: refresh the rankings CSV and stop. No agents, no DB writes, no cost",
    )
    parser.add_argument(
        "--skip-sale-advisor", action="store_true",
        help="Run Phase B without Phase C (no sale advisory / SALE_CASE), saving ~1/4 of the per-ticker cost",
    )
    parser.add_argument(
        "--skip-buy-case", action="store_true",
        help="Run Phase B without Phase E (no buy case / BUY_CASE for a Watch verdict)",
    )
    parser.add_argument(
        "--sell-check", action="store_true",
        help="Test whether TICKER's sale conditions are now met, and advise Sell/Hold (no full analysis)",
    )
    parser.add_argument(
        "--buy-check", action="store_true",
        help="Test whether TICKER's buy conditions are now met, and advise Buy/Wait (no full analysis)",
    )
    parser.add_argument(
        "--gate-check", action="store_true",
        help="Check whether TICKER passes the screen's eligibility gates (ROA/PEG etc). No analysis, no cost; prints JSON",
    )
    parser.add_argument(
        "--run", metavar="RUN_ID", default=None,
        help="With --sell-check / --buy-check: evaluate against a SPECIFIC run's SALE_CASE (the run you purchased under) or BUY_CASE, instead of the latest",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-analyze even when an identical recent report exists (bypasses the reuse check)",
    )
    parser.add_argument(
        "--no-budget", action="store_true",
        help="Disable the spend ceilings in specs/config.yaml for this invocation",
    )
    args = parser.parse_args()

    if args.no_budget:
        BUDGET_ENABLED = False
        logger.warning("Budget guard DISABLED for this run (--no-budget).")

    if args.top_n is not None:
        if args.top_n < 1:
            parser.error("--top-n must be at least 1")
        # Rebinding the module global, matching how --no-budget overrides its config
        # value above. Depth is the main cost lever (~$0.37/ticker), and it is the one
        # setting that legitimately changes run to run, so it belongs on the command
        # line rather than requiring a config edit before every run.
        top_n = args.top_n
        logger.info(f"Analyzing the top {top_n} candidates this run (--top-n override).")

    if args.run and not (args.sell_check or args.buy_check):
        parser.error("--run is only valid together with --sell-check or --buy-check")

    # The two checks answer opposite questions for opposite readers (one assumes the
    # stock is owned, the other that it is not) and each writes its own report file.
    # Running both from one invocation would be ambiguous about which answer the exit
    # was reporting, so ask for one at a time.
    if args.sell_check and args.buy_check:
        parser.error("--sell-check and --buy-check test different stored conditions; "
                     "run one at a time")

    # --screen-only never reaches Phase B, so any flag that only shapes Phase B is a
    # sign the command was not the one intended. Fail loudly rather than silently
    # ignoring it and leaving the operator to think Phase C ran (or didn't).
    if args.screen_only:
        conflicting = [
            name for name, given in (
                ("--from-csv", args.from_csv), ("--sell-check", args.sell_check),
                ("--buy-check", args.buy_check), ("--gate-check", args.gate_check),
                ("--skip-sale-advisor", args.skip_sale_advisor),
                ("--skip-buy-case", args.skip_buy_case), ("--force", args.force),
                ("TICKER", args.ticker),
            ) if given
        ]
        if conflicting:
            parser.error(
                f"--screen-only runs Phase A alone and cannot be combined with: "
                f"{', '.join(conflicting)}"
            )

    if args.screen_only:
        run_screen_only()
    elif args.sell_check:
        if not args.ticker:
            parser.error("--sell-check requires a TICKER (e.g. python main.py --sell-check CROX)")
        if args.skip_sale_advisor:
            parser.error("--skip-sale-advisor is not valid with --sell-check (it runs no Phase C)")
        run_sell_check(args.ticker, args.company, args.run)
    elif args.buy_check:
        if not args.ticker:
            parser.error("--buy-check requires a TICKER (e.g. python main.py --buy-check CROX)")
        if args.skip_buy_case:
            parser.error("--skip-buy-case is not valid with --buy-check (it runs no Phase E)")
        run_buy_check(args.ticker, args.company, args.run)
    elif args.gate_check:
        if not args.ticker:
            parser.error("--gate-check requires a TICKER (e.g. python main.py --gate-check CROX)")
        if args.skip_sale_advisor or args.skip_buy_case or args.run:
            parser.error("--gate-check runs no Phase B/C/E; cannot combine with --skip-*, --run, --force")
        run_gate_check(args.ticker)
    elif args.from_csv:
        run_from_csv(args.from_csv, args.force, args.skip_sale_advisor, args.skip_buy_case)
    elif args.ticker:
        run_single_ticker(args.ticker, args.company, args.force, args.skip_sale_advisor,
                          args.skip_buy_case)
    else:
        run_orchestrator(args.force, args.skip_sale_advisor, args.skip_buy_case)
