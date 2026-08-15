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

# This module is BOTH the CLI entry point and the module every other entry point
# imports for its shared plumbing (`critic_agent`, `refine`, `sale_advisory`,
# `buy_case_agent` all `import main`). Run as `python main.py`, it is loaded under the
# name `__main__`, so a later `import main` — which happens the moment the pipeline
# reaches the buy case — would EXECUTE THIS FILE A SECOND TIME under a second name:
# a second read of the config, a second set of agent objects, and, worst of it, a
# second StreamHandler and FileHandler attached to the same logger singleton, giving
# every subsequent log line twice on the console and splitting the run across two log
# files. Aliasing the two names in `sys.modules` before any of that can happen makes
# `import main` resolve to this very module. It is a no-op on the ordinary path,
# where `sys.modules["main"]` is already populated by the import that is running.
sys.modules.setdefault("main", sys.modules[__name__])

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
os.makedirs("logs", exist_ok=True)
os.makedirs("reports", exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = f"logs/run_{timestamp}.log"

logger = logging.getLogger("InvestmentAgentPipeline")
logger.setLevel(logging.INFO)

c_handler = logging.StreamHandler()
f_handler = logging.FileHandler(log_filename)

formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
c_handler.setFormatter(formatter)
f_handler.setFormatter(formatter)

logger.addHandler(c_handler)
logger.addHandler(f_handler)

logger.info(f"Initialized run logging to console and '{log_filename}'")

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




# The buy-case advisor's prompt is not reachable from PIPELINE_AGENTS: its agent
# lives in `buy_case_agent`, which imports this module, so this module cannot import
# it back at load time. Its instruction FILE is right here on disk, though, and that
# is what actually changes when the prompt is tuned — so the fingerprint reads the
# file rather than the agent. Read once at import: it is per-ticker input to
# `_analysis_key` and re-reading it 30 times a run buys nothing.
def _read_prompt_files(*names) -> str:
    out = []
    for name in names:
        try:
            with open(os.path.join(os.path.dirname(__file__), name), "r", encoding="utf-8") as fh:
                out.append(fh.read())
        except OSError as e:
            # Not fatal here — the module that owns these files fails loudly on its
            # own import if one is missing. All this costs is a fingerprint that no
            # longer moves when that prompt is edited, so say it once.
            logger.warning(f"Could not read {name} for the reuse fingerprint ({e}); "
                           f"edits to it will not invalidate reused reports.")
    return "".join(out)


_DOWNSTREAM_PROMPTS = _read_prompt_files(os.path.join("prompts", "buy-case.md"))


def _prompt_version() -> str:
    """Short hash of every agent instruction plus the shared prompt fragments.

    Any edit to a prompt changes this, which invalidates reuse — otherwise tuning
    the analyst's verdict stance would silently keep serving reports written under
    the old rules. The buy-case instructions are included via `_DOWNSTREAM_PROMPTS`
    for the same reason, since a reused report also reuses the buy case copied
    alongside it.

    `prompts/buy-check.md` is deliberately NOT included: nothing that command
    produces is stored under a reuse key, so editing it invalidates nothing."""
    blob = "".join(a.instruction for a in PIPELINE_AGENTS)
    blob += RECENCY_MANDATE + VERIFIED_FIGURES_MANDATE + REFERENCE_DATA_BLOCKS
    blob += _DOWNSTREAM_PROMPTS
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _analysis_key(ticker: str, candidate: dict, skip_sale_advisor: bool = False,
                  skip_buy_case: bool = False) -> str:
    """Fingerprint of the inputs a report is produced from.

    Deliberately built from the BALANCE SHEET DATE rather than today's date: two
    runs a day apart against the same filing are the same analysis. A new filing
    moves the date and forces a fresh run, as does any prompt edit.

    `skip_sale_advisor` is part of the fingerprint because a run without Phase C
    produces a STRICTLY SMALLER artifact — no SALE_CASE. Without this, a cheap
    screening run would be reused to satisfy a later full run, and
    `db_copy_ticker_outputs` would copy a set of outputs missing the sale advisory;
    the ticker would then appear complete in the web UI with an empty Sale tab, and
    `--sell-check` would find no conditions to test. Keeping the two variants under
    different keys means neither can be served in place of the other.

    `skip_buy_case` is in the fingerprint for exactly the same reason, one artifact
    along: a run made with it produces no BUY_CASE for a Watch, so reusing it to
    satisfy a full run would leave `--buy-check` with nothing to test. Note the
    asymmetry with Phase C — a run WITHOUT this flag still writes no buy case unless
    the verdict lands on Watch, which is a property of the analysis rather than of
    the flag, so the two variants genuinely differ only in what was ASKED for.

    Returns "" when the balance sheet date is unknown, which disables reuse for
    that ticker — better to pay again than to serve a report whose provenance we
    cannot establish."""
    bs_date = (candidate or {}).get("BalanceSheetDate")
    if not bs_date:
        return ""
    variant = "|no-phase-c" if skip_sale_advisor else ""
    variant += "|no-buy-case" if skip_buy_case else ""
    raw = f"{ticker}|{bs_date}|{_prompt_version()}{variant}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]




async def _run_pipeline_async(run_id: str, ticker: str, company_name: str,
                              sec_data: str, metrics_data: str, screen_context: str,
                              quarterly_data: str = "", verified_figures: str = "",
                              skip_sale_advisor: bool = False):
    """Run the analysis graph for one ticker. Returns (state, usage).

    The four roles are run as SEPARATE runners over ONE shared session rather than
    as a single SequentialAgent, so a 429 can be retried at the agent that failed.
    Retrying the whole graph (the previous behaviour) discarded completed bear and
    bull work and paid for it again — at ~$0.39/ticker a single mid-sequence rate
    limit cost a full extra run, and those wasted tokens were never even counted.

    Handoff still happens through session state: each agent's `output_key` writes
    into the shared session, and the next agent templates that key. That is exactly
    what SequentialAgent did internally, so behaviour is unchanged."""
    session_service = InMemorySessionService()
    session_id = f"{run_id}:{ticker}"

    # Seed per-ticker context. sec_data/metrics_data/quarterly_data come from direct
    # tool calls (100% fidelity); screen_context carries the Magic Formula bull
    # signal; verified_figures carries the deterministic balance-sheet ground truth
    # every agent is forbidden to contradict. All are read by the agents via
    # {sec_data}/{metrics_data}/{quarterly_data}/{screen_context}/{verified_figures}.
    await session_service.create_session(
        app_name=APP_NAME,
        user_id="orchestrator",
        session_id=session_id,
        state={
            "ticker": ticker,
            "company_name": company_name or ticker,
            "sec_data": sec_data,
            "metrics_data": metrics_data,
            "quarterly_data": quarterly_data,
            "verified_figures": verified_figures,
            "screen_context": screen_context,
        },
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=f"Run the skeptical decomposer analysis for {company_name} ({ticker}).")],
    )

    usage = _new_usage()
    # Phase C (the sale advisor) is the last role and nothing downstream in the graph
    # reads its output, so dropping it is a clean truncation rather than a hole in the
    # middle. It is ~1/4 of the per-ticker cost.
    agents = [a for a in PIPELINE_AGENTS if a is not sale_advisor_agent] \
        if skip_sale_advisor else PIPELINE_AGENTS
    for agent in agents:
        # Tokens are attributed per ROLE as well as per ticker. Without this split a
        # regression in one agent's prompt is invisible: the ticker total moves and
        # there is nothing to say which of the four roles moved it. It also makes the
        # cost of history replay legible, since that lands on the LATER agents only.
        agent_usage = _new_usage()
        # Per-agent retry. Tokens spent on a failed attempt ARE counted: they were
        # billed, and hiding them is how the previous accounting understated the
        # true cost of a rate-limited run.
        for attempt in range(MAX_AGENT_RETRIES):
            runner = _build_runner(agent, session_service)
            try:
                async for event in runner.run_async(
                    user_id="orchestrator", session_id=session_id, new_message=message
                ):
                    _add_event_usage(agent_usage, event)
                break
            except Exception as e:
                if _is_rate_limit_error(e) and attempt < MAX_AGENT_RETRIES - 1:
                    wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                    logger.warning(
                        f"[{ticker}] {agent.name} rate limited (429). Backing off {wait}s "
                        f"(attempt {attempt + 1}/{MAX_AGENT_RETRIES}). Earlier agents in "
                        f"this ticker are NOT re-run."
                    )
                    await asyncio.sleep(wait)
                    continue
                # No _merge_usage here on purpose: `analyze_ticker` discards this
                # accumulator on a pipeline failure and returns zero usage, so
                # anything merged in would be thrown away regardless. A ticker that
                # dies mid-pipeline therefore reports $0 even though earlier roles
                # were billed. Known and accepted — the path has never fired.
                logger.error(f"[{ticker}] {agent.name} failed: {e}")
                raise
        _log_usage(f"{ticker} {agent.name}", agent_usage)
        _merge_usage(usage, agent_usage)
        # Gentle throttle between agents, replacing the old per-ticker sleep.
        await asyncio.sleep(INTER_AGENT_DELAY_SECONDS)

    final = await session_service.get_session(
        app_name=APP_NAME, user_id="orchestrator", session_id=session_id
    )
    return (dict(final.state) if final else {}), usage


def run_pipeline(run_id: str, ticker: str, company_name: str, sec_data: str,
                 metrics_data: str, screen_context: str,
                 quarterly_data: str = "", verified_figures: str = "",
                 skip_sale_advisor: bool = False):
    """Synchronous wrapper around the Phase-B graph. Returns (state, usage).

    Rate-limit retries happen INSIDE `_run_pipeline_async`, per agent — there is
    deliberately no retry loop here. Wrapping this call in a second retry would
    re-run every completed role on a late 429 and re-pay for it, which is the waste
    the per-agent retry exists to remove. Tokens burned on a failed attempt are
    counted rather than silently dropped, so a rate-limited run reports what it
    actually cost.
    """
    state, usage = asyncio.run(_run_pipeline_async(
        run_id, ticker, company_name, sec_data, metrics_data, screen_context,
        quarterly_data, verified_figures, skip_sale_advisor,
    ))
    time.sleep(INTER_CALL_DELAY_SECONDS)  # gentle throttle between tickers
    return state, usage


async def _run_sell_check_async(ticker: str, company_name: str,
                                sale_conditions: str, metrics_data: str,
                                quarterly_data: str = ""):
    """Run the standalone sell_check_agent for one ticker. Returns (state, usage);
    state['sell_check'] holds the evaluation report."""
    session_service = InMemorySessionService()
    runner = _build_runner(sell_check_agent, session_service)
    session_id = f"sellcheck:{ticker}:{timestamp}"

    await session_service.create_session(
        app_name=APP_NAME,
        user_id="orchestrator",
        session_id=session_id,
        state={
            "ticker": ticker,
            "company_name": company_name or ticker,
            "sale_conditions": sale_conditions,
            "metrics_data": metrics_data,
            "quarterly_data": quarterly_data,
        },
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=f"Check whether the prior sale conditions for {company_name} ({ticker}) are now met.")],
    )

    usage = _new_usage()
    async for event in runner.run_async(
        user_id="orchestrator", session_id=session_id, new_message=message
    ):
        _add_event_usage(usage, event)

    final = await session_service.get_session(
        app_name=APP_NAME, user_id="orchestrator", session_id=session_id
    )
    return (dict(final.state) if final else {}), usage


def _run_sell_check(ticker: str, company_name: str, sale_conditions: str,
                    metrics_data: str, quarterly_data: str = ""):
    """Synchronous wrapper around the sell-condition check with 429 backoff."""
    for attempt in range(MAX_AGENT_RETRIES):
        try:
            return asyncio.run(_run_sell_check_async(
                ticker, company_name, sale_conditions, metrics_data, quarterly_data,
            ))
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < MAX_AGENT_RETRIES - 1:
                wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    f"[{ticker}] Rate limited (429). Backing off {wait}s "
                    f"(attempt {attempt + 1}/{MAX_AGENT_RETRIES})."
                )
                time.sleep(wait)
                continue
            logger.error(f"[{ticker}] Sell-condition check failed: {e}")
            raise


# Reading the status string a db_* tool hands back moved to `persistence.results`,
# next to the tools whose return contract it interprets. It had to move: `usage`
# calls it when finalizing a run, and `usage` cannot import this module. Re-exported
# because `refine`, `buy_case`, `buy_case_agent` and `sale_advisory` call it as
# `main._check_db`.
from persistence import _check_db  # noqa: E402,F401  (re-exported)


# --- 3. PHASE B: PER-TICKER ANALYSIS (shared by full + on-demand runs) ---
def _with_run_header(md: str, run_id: str, ticker: str) -> str:
    """Stamp the run_id onto a stored report so it is visible (and copyable) at the
    top of the web viewer. When recording a purchase, this run_id is what you save
    against the lot so the sell-condition check can pin the exact thesis you bought
    under (`--run RUN_ID`; see specs/agent_architecture.md §8.D). Rendered as a
    blockquote at the very top; it does not disturb '## ...' section parsing."""
    if not md:
        return md
    banner = f"> **Run ID:** `{run_id}`  \n> **Ticker:** {ticker}\n\n"
    return banner + md


def _extract_verdict(report_text: str) -> str:
    """Extract BUY/WATCH/AVOID from the report's '## Final Verdict' section.

    Anchors on the explicit 'Verdict: X' declaration rather than a naive
    substring search — the justification prose often mentions the word 'buy'
    when weighing the bull case, which would misclassify the verdict.
    """
    section = report_text.split("## Final Verdict")[-1]
    m = re.search(r"verdict[^A-Za-z]{0,12}(buy|watch|avoid)", section, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    lowered = section.lower()
    positions = {w: lowered.find(w) for w in ("buy", "watch", "avoid") if lowered.find(w) != -1}
    return min(positions, key=positions.get).upper() if positions else "WATCH"


# Matches the '## Verdict Basis' section and everything after it. Anchored to the
# heading rather than to the fence so a model that emits stray prose around the
# block still gets the whole tail removed from the reader-facing report.
_VERDICT_BASIS_RE = re.compile(r"\n#{1,6}\s*Verdict Basis\b.*", re.IGNORECASE | re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


def _split_verdict_basis(report_text: str, ticker: str = "") -> tuple:
    """Split the analyst's report into (reader-facing report, basis factor list).

    The basis block is machine-only, so it is removed from the text that gets
    stored, rendered and shown: the report is written for a reader with no
    finance background, and a JSON array at the foot of it is noise to every
    human who opens it. Stripping happens here, before the reconciliation gate
    and the critic see the text, so no downstream consumer has to know the
    section ever existed.

    A missing or malformed block costs the ledger one row and nothing else — it
    never fails the analysis. The report is the product; the bookkeeping is not.
    """
    if not report_text:
        return report_text, []
    m = _VERDICT_BASIS_RE.search(report_text)
    if not m:
        logger.warning(f"[{ticker}] No '## Verdict Basis' section in the analyst's "
                       f"report; this verdict will be logged with no factors.")
        return report_text, []

    tail, cleaned = m.group(0), report_text[:m.start()].rstrip() + "\n"
    fence = _JSON_FENCE_RE.search(tail)
    raw = fence.group(1) if fence else None
    if raw is None:
        # Fall back to the outermost bracket pair: models occasionally emit the
        # array without fencing it, and the data is perfectly good when they do.
        start, end = tail.find("["), tail.rfind("]")
        raw = tail[start:end + 1] if 0 <= start < end else None
    if raw is None:
        logger.warning(f"[{ticker}] Verdict Basis section contained no JSON array.")
        return cleaned, []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[{ticker}] Could not parse the Verdict Basis JSON ({e}); "
                       f"this verdict will be logged with no factors.")
        return cleaned, []
    if not isinstance(parsed, list):
        logger.warning(f"[{ticker}] Verdict Basis JSON was not an array.")
        return cleaned, []

    known = set(LEDGER_FACTOR_TAXONOMY)
    factors, off_vocabulary = [], []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        key = str(item.get("factor_key") or "").strip()
        if not key:
            continue
        # An unrecognised key is KEPT, not dropped. It is recorded as given so
        # the monthly taxonomy review can see what the analyst reached for; a
        # dropped observation is unrecoverable, an untidy key is a rename.
        if key not in known and not key.upper().startswith("OTHER_"):
            off_vocabulary.append(key)
            key = f"OTHER_{key}"
        factors.append({
            "factor_key": key,
            "stance": str(item.get("stance") or "").strip().upper(),
            "materiality": str(item.get("materiality") or "").strip().upper(),
            "observation": str(item.get("observation") or "").strip(),
        })
    if off_vocabulary:
        logger.info(f"[{ticker}] Verdict basis used {len(off_vocabulary)} key(s) outside "
                    f"the taxonomy, stored under OTHER_: {', '.join(off_vocabulary)}")
    return cleaned, factors


_BENCHMARK_CACHE = {}


def _benchmark_price() -> float:
    """Today's benchmark level, fetched once per process and reused.

    Cached by date rather than fetched per ticker: a 30-ticker run would
    otherwise make 30 identical quote calls, and every verdict in that run would
    be baselined against a slightly different intraday level for no reason. The
    benchmark is what a verdict's excess return is measured against years later,
    so it is captured now — recovering a historical index level for an arbitrary
    past date later means trusting a provider to still serve it.

    Returns 0.0 on failure, which db_log_verdict stores as NULL. A verdict with
    no baseline is still gradeable on absolute return; refusing to log it would
    lose the observation entirely."""
    today = datetime.now().date().isoformat()
    if _BENCHMARK_CACHE.get("date") == today:
        return _BENCHMARK_CACHE.get("price", 0.0)
    price = 0.0
    try:
        snap = json.loads(fmp_price_snapshot(LEDGER_BENCHMARK))
        price = float(snap.get("price") or 0.0)
    except Exception as e:
        logger.warning(f"Could not fetch the {LEDGER_BENCHMARK} benchmark price ({e}); "
                       f"verdicts this run will be logged without a baseline.")
    _BENCHMARK_CACHE.update({"date": today, "price": price})
    return price


def _git_sha() -> str:
    """Short commit SHA of the working tree, or "" outside a checkout.

    Stored beside `_prompt_version()` on every logged verdict. The hash says two
    prompt versions differ; the commit says HOW they differ, which is the part a
    human needs when the track record splits either side of an edit."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=os.path.dirname(__file__),
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()[:40] if out.returncode == 0 else ""
    except Exception:
        return ""


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

    usage = analyze_ticker(run_id, ticker, company_name, screen_context, candidate, force,
                           skip_sale_advisor, skip_buy_case)

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

    # Deferred import: `buy_case_agent` imports this module for the shared
    # cost/logging plumbing (as `critic_agent` does), so importing it at the top of
    # this file would be a cycle. See the `sys.modules` alias at the top of the file
    # for why `import main` inside it resolves back here instead of re-running this
    # file. Done first, before the database round-trip, so a broken prompt file or a
    # bad import fails immediately rather than after the lookup has succeeded.
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
                ("--buy-check", args.buy_check),
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
    elif args.from_csv:
        run_from_csv(args.from_csv, args.force, args.skip_sale_advisor, args.skip_buy_case)
    elif args.ticker:
        run_single_ticker(args.ticker, args.company, args.force, args.skip_sale_advisor,
                          args.skip_buy_case)
    else:
        run_orchestrator(args.force, args.skip_sale_advisor, args.skip_buy_case)
