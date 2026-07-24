import os
import re
import time
import json
import asyncio
import logging
from datetime import datetime
import uuid
import yaml
import pandas as pd
from dotenv import load_dotenv

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Import custom MCP tool wrappers
from mcp_server import (
    run_magic_formula_screener,
    compute_ticker_magic_metrics,
    fetch_sec_10k_data,
    fmp_metrics_extractor,
    fmp_stock_news,
    web_search_tool,
    db_create_pipeline_run,
    db_update_pipeline_status,
    db_finalize_pipeline_run,
    db_store_agent_output,
    db_store_final_report
)
# The screener's CSV output path (default source for --from-csv mode).
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

# Load Configuration
config_path = os.path.join(os.path.dirname(__file__), "specs", "config.yaml")
with open(config_path, "r") as f:
    config = yaml.safe_load(f)

top_n = config.get("agents", {}).get("screener_agent", {}).get("top_n_candidates", 30)

# LLM pricing (USD per 1M tokens) for estimating per-run spend from token usage.
_pricing = config.get("llm_pricing", {})
PRICE_INPUT_PER_1M = float(_pricing.get("input_usd_per_1m", 0.30))
PRICE_OUTPUT_PER_1M = float(_pricing.get("output_usd_per_1m", 2.50))

# Web search (Tavily) pricing (USD per 1,000 requests) for per-run cost estimates.
TAVILY_PRICE_PER_1K = float(config.get("search_pricing", {}).get("tavily_usd_per_1k", 16.00))

# Load research instructions. research-instructions.md drives the BEAR (skeptical)
# agent; bullish-research-instructions.md drives the BULL agent. Neither file
# contains '{' so both are safe to embed in state-templated instructions.
instructions_path = os.path.join(os.path.dirname(__file__), "research-instructions.md")
with open(instructions_path, "r") as f:
    research_instructions = f.read()

bullish_path = os.path.join(os.path.dirname(__file__), "bullish-research-instructions.md")
with open(bullish_path, "r") as f:
    bullish_instructions = f.read()

# --- 2. AGENT DEFINITIONS (Phase B) ---
# Two kinds of steps:
#   1. Pure data relays (SEC 10-K, FMP metrics) — deterministic tool calls invoked
#      DIRECTLY in analyze_ticker (100% fidelity, 0 tokens), seeded into session
#      state as sec_data / metrics_data (shared by both advocates below).
#   2. Reasoning — a SequentialAgent of THREE roles that hand off via session state:
#         bear_agent  (research-instructions.md) -> state['bear_data']
#         bull_agent  (bullish-research-instructions.md) -> state['bull_data']
#                     (runs after bear so it can refute it)
#         analyst_agent (neutral judge) reads {bear_data}/{bull_data} -> final_report
# ticker, company_name, sec_data, metrics_data, screen_context are seeded before
# the graph runs; each agent templates the keys it references.
APP_NAME = "skeptical_decomposer"

# --- Advocate 1: BEAR (skeptical research) ---
bear_agent = LlmAgent(
    name="bear_agent",
    model="gemini-flash-latest",
    instruction=(
        research_instructions
        + "\n\n## How to run this research\n"
        "You are building the BEAR case for {company_name} ({ticker}). Use your tools: "
        "call fmp_stock_news for recent factual news, and web_search_tool for bear-case "
        "risks, short-seller theses, and competitive/regulatory threats. Answer the "
        "questions above skeptically, using the gathered research plus the financial "
        "data below. Do not invent facts; cite sources.\n\n"
        "<MAGIC_FORMULA_CONTEXT>\n{screen_context}\n</MAGIC_FORMULA_CONTEXT>\n\n"
        "<SEC_DATA>\n{sec_data}\n</SEC_DATA>\n\n"
        "<METRICS_DATA>\n{metrics_data}\n</METRICS_DATA>\n"
    ),
    tools=[fmp_stock_news, web_search_tool],
    output_key="bear_data",
)

# --- Advocate 2: BULL (runs after bear; §4 refutes the bear case) ---
bull_agent = LlmAgent(
    name="bull_agent",
    model="gemini-flash-latest",
    instruction=(
        bullish_instructions
        + "\n\n## How to run this research\n"
        "You are building the BULL case for {company_name} ({ticker}). The required "
        "inputs — Earnings Yield and ROC — are in MAGIC_FORMULA_CONTEXT below, and the "
        "Bear Case Research Output is in BEAR_CASE below. Use your tools: call "
        "fmp_stock_news for positive catalysts, and web_search_tool for bull-thesis "
        "evidence (moat/pricing power, growth catalysts, historical resilience). Answer "
        "every bullish question above; for Section 4, directly refute the specific "
        "points raised in BEAR_CASE. Do not invent facts; cite sources.\n\n"
        "<MAGIC_FORMULA_CONTEXT>\n{screen_context}\n</MAGIC_FORMULA_CONTEXT>\n\n"
        "<SEC_DATA>\n{sec_data}\n</SEC_DATA>\n\n"
        "<METRICS_DATA>\n{metrics_data}\n</METRICS_DATA>\n\n"
        "<BEAR_CASE>\n{bear_data}\n</BEAR_CASE>\n"
    ),
    tools=[fmp_stock_news, web_search_tool],
    output_key="bull_data",
)

# --- Judge: NEUTRAL analyst (no skeptical prompt) weighs bull vs bear ---
analyst_agent = LlmAgent(
    name="analyst_agent",
    model="gemini-flash-latest",
    instruction=(
        "You are a NEUTRAL, balanced investment analyst — neither skeptical-by-default "
        "nor promotional. You are given a fully-argued BEAR case and a fully-argued BULL "
        "case for {company_name} ({ticker}), plus the Magic Formula value/quality "
        "context. Weigh them fairly and write a Markdown report with EXACTLY these "
        "sections:\n"
        "## Bull Case (summary)\n"
        "## Bear Case (summary)\n"
        "## Final Verdict\n"
        "In the Final Verdict, explicitly weigh the bull case against the bear case, then "
        "state exactly one verdict for an investor who does NOT currently own the stock:\n"
        "- 'Verdict: Buy'   = attractive enough to initiate a position.\n"
        "- 'Verdict: Watch' = not compelling enough to buy now; watchlist.\n"
        "- 'Verdict: Avoid' = actively unattractive; do not buy.\n"
        "Follow the verdict with a one-paragraph justification of how the two cases net "
        "out. Do NOT default to the middle — choose 'Watch' only if genuinely balanced. "
        "Do not invent facts beyond the two cases provided.\n\n"
        "<MAGIC_FORMULA_CONTEXT>\n{screen_context}\n</MAGIC_FORMULA_CONTEXT>\n\n"
        "<BEAR_CASE>\n{bear_data}\n</BEAR_CASE>\n\n"
        "<BULL_CASE>\n{bull_data}\n</BULL_CASE>\n"
    ),
    tools=[],
    output_key="final_report",
)

# The Phase-B reasoning graph: bear -> bull -> neutral analyst. (SEC and metrics are
# gathered by direct tool calls in analyze_ticker, not by agents.)
skeptical_pipeline = SequentialAgent(
    name="skeptical_pipeline",
    sub_agents=[bear_agent, bull_agent, analyst_agent],
)

# --- 2b. AGENT EXECUTION HELPER ---
# Gemini API returns HTTP 429 (RESOURCE_EXHAUSTED) when the per-minute request
# or token quota is exceeded — especially for gemini-2.5-pro, whose free/standard
# tier limits are low and which the analysis agent hits once per ticker with a
# large prompt. We retry with exponential backoff and throttle between calls.
MAX_AGENT_RETRIES = 5
BASE_BACKOFF_SECONDS = 20
INTER_CALL_DELAY_SECONDS = 2


def _is_rate_limit_error(err: Exception) -> bool:
    text = str(err).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "RATE" in text and "LIMIT" in text


# --- Token usage / cost accounting ---------------------------------------
def _new_usage() -> dict:
    return {"input": 0, "output": 0, "total": 0, "requests": 0, "search_requests": 0}


def _add_event_usage(usage: dict, event) -> None:
    """Accumulate token counts and web-search (Tavily) invocations from an ADK
    event. Partial (streaming) events are skipped to avoid double counting. Output
    tokens include `thoughts` tokens (billed at the output rate on thinking models).
    Note: fmp_stock_news calls are not counted here — they are FMP (Starter) calls,
    not billed web searches."""
    if getattr(event, "partial", False):
        return

    # Count Tavily web-search calls (each web_search_tool invocation = 1 request).
    content = getattr(event, "content", None)
    if content and getattr(content, "parts", None):
        for part in content.parts:
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None) == "web_search_tool":
                usage["search_requests"] += 1

    # Token usage (present only on model response events).
    um = getattr(event, "usage_metadata", None)
    if um is None:
        return
    usage["input"] += um.prompt_token_count or 0
    usage["output"] += (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
    usage["total"] += um.total_token_count or 0
    usage["requests"] += 1


def _merge_usage(acc: dict, other: dict) -> None:
    for k in acc:
        acc[k] += other.get(k, 0)


def _llm_cost(usage: dict) -> float:
    return (usage["input"] / 1e6) * PRICE_INPUT_PER_1M + (usage["output"] / 1e6) * PRICE_OUTPUT_PER_1M


def _search_cost(usage: dict) -> float:
    return (usage["search_requests"] / 1000.0) * TAVILY_PRICE_PER_1K


def _log_usage(label: str, usage: dict) -> float:
    """Log token/search usage and estimated cost. Returns combined USD cost."""
    llm = _llm_cost(usage)
    search = _search_cost(usage)
    total = llm + search
    logger.info(
        f"[{label}] Tokens: {usage['requests']} model calls | "
        f"input={usage['input']:,} output={usage['output']:,} total={usage['total']:,} "
        f"| Tavily: {usage['search_requests']} searches "
        f"| est. cost: LLM=${llm:.4f} Tavily=${search:.4f} TOTAL=${total:.4f}"
    )
    return total


def _finalize_run(run_id: str, usage: dict, run_label: str) -> None:
    """Log the run's usage/cost and persist it (with terminal status) to
    pipeline_runs."""
    total_cost = _log_usage(f"RUN {run_id} TOTAL", usage)
    _check_db(
        db_finalize_pipeline_run(
            run_id, "COMPLETED",
            usage["requests"], usage["input"], usage["output"], usage["total"],
            usage["search_requests"],
            round(_llm_cost(usage), 6), round(_search_cost(usage), 6), round(total_cost, 6),
        ),
        "finalize pipeline run",
    )
    logger.info(f"{run_label} {run_id} COMPLETED. Estimated spend (LLM+Tavily): ${total_cost:.4f}")


async def _run_pipeline_async(run_id: str, ticker: str, company_name: str,
                              sec_data: str, metrics_data: str, screen_context: str):
    """Run the search->analysis graph for one ticker. Returns (state, usage) where
    state holds search_data/final_report (plus the seeded sec_data/metrics_data)
    and usage holds the aggregated token counts for this ticker's model calls."""
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=skeptical_pipeline, session_service=session_service)
    session_id = f"{run_id}:{ticker}"

    # Seed per-ticker context. sec_data/metrics_data come from direct tool calls
    # (100% fidelity); screen_context carries the Magic Formula bull signal. All are
    # read by the analysis agent via {sec_data}/{metrics_data}/{screen_context}.
    await session_service.create_session(
        app_name=APP_NAME,
        user_id="orchestrator",
        session_id=session_id,
        state={
            "ticker": ticker,
            "company_name": company_name or ticker,
            "sec_data": sec_data,
            "metrics_data": metrics_data,
            "screen_context": screen_context,
        },
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=f"Run the skeptical decomposer analysis for {company_name} ({ticker}).")],
    )

    # Drive the graph to completion; sub-agent handoffs happen via session state.
    usage = _new_usage()
    async for event in runner.run_async(
        user_id="orchestrator", session_id=session_id, new_message=message
    ):
        _add_event_usage(usage, event)

    final = await session_service.get_session(
        app_name=APP_NAME, user_id="orchestrator", session_id=session_id
    )
    return (dict(final.state) if final else {}), usage


def run_pipeline(run_id: str, ticker: str, company_name: str, sec_data: str,
                 metrics_data: str, screen_context: str):
    """Synchronous wrapper around the Phase-B graph with 429 exponential backoff.
    Returns (state, usage). Note: tokens from a failed attempt before a 429 are
    not counted (the retry re-runs the whole ticker); this is a minor undercount.

    A 429 mid-sequence re-runs the whole ticker pipeline; with the analysis
    agent on flash plus throttling this should be rare.
    """
    for attempt in range(MAX_AGENT_RETRIES):
        try:
            state, usage = asyncio.run(_run_pipeline_async(run_id, ticker, company_name, sec_data, metrics_data, screen_context))
            time.sleep(INTER_CALL_DELAY_SECONDS)  # gentle throttle between tickers
            return state, usage
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < MAX_AGENT_RETRIES - 1:
                wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    f"[{ticker}] Rate limited (429). Backing off {wait}s "
                    f"(attempt {attempt + 1}/{MAX_AGENT_RETRIES})."
                )
                time.sleep(wait)
                continue
            logger.error(f"[{ticker}] Pipeline run failed: {e}")
            raise


def _check_db(result: str, context: str) -> None:
    """DB MCP tools return a status string instead of raising. Previously these
    return values were ignored, so failed inserts were completely silent. Log
    them so persistence failures are visible."""
    if isinstance(result, str) and result.startswith("Error"):
        logger.error(f"DB persistence failed ({context}): {result}")
    else:
        logger.info(f"DB ok ({context}): {result}")


# --- 3. PHASE B: PER-TICKER ANALYSIS (shared by full + on-demand runs) ---
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


def analyze_ticker(run_id: str, ticker: str, company_name: str, screen_context: str = "") -> dict:
    """Run the Phase-B SequentialAgent graph for one ticker and persist its
    outputs and final report. Returns the ticker's token-usage dict (zero usage
    if the pipeline failed).

    `screen_context` carries the Magic Formula bull signal (rank / ROC / earnings
    yield) so the verdict can weigh it against the bear case. Empty for tickers
    that did not come through the screen (on-demand runs).

    Assumes the parent pipeline_runs row for `run_id` already exists (the
    agent_outputs/final_reports tables have a FK onto it).
    """
    logger.info(f"[{ticker}] Gathering SEC + metrics (direct tool calls)...")
    # Pure data relays: call the deterministic tools directly (100% fidelity, 0 tokens).
    sec_data = fetch_sec_10k_data(ticker)
    metrics_data = fmp_metrics_extractor(ticker)

    logger.info(f"[{ticker}] Running bear -> bull -> analyst graph...")
    try:
        state, usage = run_pipeline(run_id, ticker, company_name, sec_data, metrics_data, screen_context)
    except Exception as e:
        logger.error(f"[{ticker}] Skipping after pipeline failure: {e}")
        return _new_usage()

    _log_usage(ticker, usage)

    bear_data = state.get("bear_data", "") or ""
    bull_data = state.get("bull_data", "") or ""
    report_text = state.get("final_report", "") or ""

    # Persist each step's raw output (spec section 5B). SEC/metrics are raw
    # provenance (reproducible, low search value) -> stored without an embedding.
    # Bear/bull are the analytical content -> embedded for semantic search.
    _check_db(db_store_agent_output(run_id, ticker, "SEC_DATA", sec_data, json.dumps({"ticker": ticker}), embed=False), f"{ticker} SEC output")
    _check_db(db_store_agent_output(run_id, ticker, "QUANT_METRICS", metrics_data, json.dumps({"ticker": ticker}), embed=False), f"{ticker} metrics output")
    _check_db(db_store_agent_output(run_id, ticker, "BEAR_CASE", bear_data, json.dumps({"ticker": ticker})), f"{ticker} bear case")
    _check_db(db_store_agent_output(run_id, ticker, "BULL_CASE", bull_data, json.dumps({"ticker": ticker})), f"{ticker} bull case")

    if not report_text:
        logger.error(f"[{ticker}] No final report produced; skipping report persistence.")
        return usage

    verdict = _extract_verdict(report_text)
    _check_db(db_store_final_report(run_id, ticker, verdict, report_text), f"{ticker} final report")

    report_path = os.path.join("reports", f"{ticker}_Final_Report_{verdict.title()}.md")
    with open(report_path, "w", encoding='utf-8') as f:
        f.write(report_text)

    logger.info(f"[{ticker}] Completed Analysis ({verdict}) and saved to {report_path}")
    return usage


# --- 4. ORCHESTRATOR WORKFLOWS ---
def _format_screen_context(candidate: dict) -> str:
    """Turn a Magic Formula candidate row (or single-ticker computed metrics) into
    the value/quality bull context that the agents weigh against the bear case."""
    rank = candidate.get("Final_Rank")
    if rank is not None:
        header = (
            "This stock was surfaced by a Magic Formula screen (Joel Greenblatt's value "
            "+ quality strategy). A high rank means it is statistically BOTH cheap (high "
            "earnings yield) AND high-quality (high return on capital)."
        )
        rank_line = f"- Magic Formula rank: #{rank} (lower is better)\n"
    else:
        header = (
            "Magic Formula value/quality metrics were computed on demand for this ticker "
            "(it was not ranked against a screen universe). A high ROC + high earnings "
            "yield means it is statistically BOTH cheap AND high-quality."
        )
        rank_line = "- Magic Formula rank: n/a (single-ticker run)\n"
    ctx = (
        header
        + " This is the bullish starting point that must be weighed against the bear case.\n"
        + rank_line
        + f"- Return on Capital (quality signal): {candidate.get('ROC_Pct')}\n"
        + f"- Earnings Yield (cheapness signal): {candidate.get('EY_Pct')}"
    )
    if candidate.get("MagicFormula_Score") is not None:
        ctx += f"\n- Combined Magic Formula score: {candidate.get('MagicFormula_Score')}"
    return ctx


def _run_phase_b(top_candidates: list, source_label: str):
    """Create a pipeline run and analyze each candidate through Phase B.
    Shared by the full-pipeline and CSV modes."""
    run_id = str(uuid.uuid4())
    logger.info(f"Starting Workflow Run: {run_id} (candidates from {source_label})")

    # Parent pipeline_runs row BEFORE any agent_outputs/final_reports inserts —
    # those tables have a foreign key onto pipeline_runs(run_id).
    tickers = [c.get("Symbol") for c in top_candidates if c.get("Symbol")]
    _check_db(db_create_pipeline_run(run_id, tickers), "create pipeline run")

    run_usage = _new_usage()
    for idx, candidate in enumerate(top_candidates):
        ticker = candidate.get("Symbol")
        company_name = candidate.get("CompanyName")
        logger.info(f"--- Processing {idx+1}/{len(top_candidates)}: {ticker} ({company_name}) ---")
        screen_context = _format_screen_context(candidate)
        _merge_usage(run_usage, analyze_ticker(run_id, ticker, company_name, screen_context))

    _finalize_run(run_id, run_usage, "Workflow Run")


def run_orchestrator():
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
    _run_phase_b(top_candidates, "Phase A screener")


def run_from_csv(csv_path: str = None):
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
    _run_phase_b(top_candidates, f"CSV '{csv_path}'")


def run_single_ticker(ticker: str, company_name: str = None):
    """On-demand Phase B: run the skeptical analysis for a single arbitrary
    ticker, bypassing Phase A. Logged as its own one-company pipeline run."""
    ticker = ticker.strip().upper()
    company_name = company_name or ticker
    run_id = str(uuid.uuid4())
    logger.info(f"Starting On-Demand Run: {run_id} for {ticker} ({company_name})")

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
        logger.warning(f"[{ticker}] Could not compute ROC/EY ({candidate.get('error', 'unknown')}); proceeding without the bull signal.")
        screen_context = (
            "This ticker was analyzed on demand and its Magic Formula ROC/Earnings Yield "
            "could not be computed, so no value/quality (bull) signal is available. Base "
            "the bull case on the fundamentals and research below."
        )
    else:
        candidate.setdefault("CompanyName", company_name)
        screen_context = _format_screen_context(candidate)

    usage = analyze_ticker(run_id, ticker, company_name, screen_context)

    _finalize_run(run_id, usage, "On-Demand Run")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Magic Formula Skeptical Decomposer",
        epilog=(
            "Modes:\n"
            "  python main.py                  full pipeline (Phase A screener + Phase B)\n"
            "  python main.py --from-csv       skip Phase A; run Phase B on top N from the rankings CSV\n"
            "  python main.py --from-csv PATH  same, from a specific CSV file\n"
            "  python main.py TICKER [Company] on-demand Phase B for one ticker"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("ticker", nargs="?", help="On-demand Phase B for a single ticker (skips Phase A)")
    parser.add_argument("company", nargs="?", help="Optional company name for the single-ticker run")
    parser.add_argument(
        "--from-csv", nargs="?", const=OUTPUT_FILENAME, metavar="PATH", default=None,
        help=f"Skip Phase A; read candidates from the screener CSV (default: {OUTPUT_FILENAME}) and run Phase B on the top N",
    )
    args = parser.parse_args()

    if args.from_csv:
        run_from_csv(args.from_csv)
    elif args.ticker:
        run_single_ticker(args.ticker, args.company)
    else:
        run_orchestrator()
