import os
import re
import time
import json
import asyncio
import logging
from datetime import datetime
import uuid
import yaml
from dotenv import load_dotenv

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Import custom MCP tool wrappers
from mcp_server import (
    run_magic_formula_screener,
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

# Load Instructions for Analysis Agent
instructions_path = os.path.join(os.path.dirname(__file__), "research-instructions.md")
with open(instructions_path, "r") as f:
    research_instructions = f.read()

# --- 2. AGENT DEFINITIONS (Phase B ADK Sequential Workflow Graph) ---
# The four Phase-B agents are wired into an ADK SequentialAgent so the handoffs
# happen natively through shared session state (spec section 8):
#   sec_agent  -> state['sec_data']
#   metrics_agent -> state['metrics_data']
#   search_agent -> state['search_data']
#   analysis_agent reads all three via {sec_data}/{metrics_data}/{search_data}
# The per-ticker 'ticker' and 'company_name' are seeded into session state, so
# each agent's instruction templates them with {ticker} / {company_name}.
APP_NAME = "skeptical_decomposer"

sec_agent = LlmAgent(
    name="sec_agent",
    model="gemini-flash-latest",
    instruction=(
        "Call the fetch_sec_10k_data tool with ticker '{ticker}' to extract Item 1 "
        "segment data and >10% customer concentration notes. Return the tool's "
        "output verbatim without summarizing or omitting content."
    ),
    tools=[fetch_sec_10k_data],
    output_key="sec_data",
)

metrics_agent = LlmAgent(
    name="metrics_agent",
    model="gemini-flash-latest",
    instruction=(
        "Call the fmp_metrics_extractor tool with ticker '{ticker}' to fetch 3-year "
        "metric trends, P/E history, analyst consensus, and competitor data. Return "
        "the tool's output verbatim."
    ),
    tools=[fmp_metrics_extractor],
    output_key="metrics_data",
)

search_agent = LlmAgent(
    name="search_agent",
    model="gemini-flash-latest",
    instruction=(
        "Research recent developments and the bear case for {company_name} ({ticker}) "
        "using BOTH of your tools, then combine their outputs:\n"
        "1. Call fmp_stock_news with ticker '{ticker}' for hard, factual, "
        "company-specific news and catalysts (earnings, guidance, downgrades, M&A, "
        "lawsuits).\n"
        "2. Call web_search_tool with a query about {company_name}'s bear case, "
        "valuation risks, and competitive/regulatory threats for the qualitative "
        "analyst/short-seller perspective.\n"
        "Return a combined summary of the factual news AND the bear-case risks, "
        "keeping source URLs."
    ),
    tools=[fmp_stock_news, web_search_tool],
    output_key="search_data",
)

# research_instructions contains no braces, so it is safe to embed in a
# state-templated instruction. The braces below are ADK state placeholders.
analysis_instruction = (
    research_instructions
    + "\n\nAdditionally, append a final section titled '## Final Verdict' that "
    "explicitly outputs a 'Buy', 'Sell', or 'Hold' suggestion with a one-paragraph "
    "justification summarizing the bear case vs. the valuation reality.\n\n"
    "Analyze {company_name} (Ticker: {ticker}) using ONLY the collected data below. "
    "Do not invent facts; explain complex terms simply.\n\n"
    "<SEC_DATA>\n{sec_data}\n</SEC_DATA>\n\n"
    "<METRICS_DATA>\n{metrics_data}\n</METRICS_DATA>\n\n"
    "<SEARCH_DATA>\n{search_data}\n</SEARCH_DATA>\n"
)

analysis_agent = LlmAgent(
    name="analysis_agent",
    model="gemini-flash-latest",  # flash (not pro) to de-risk 429 rate limits
    instruction=analysis_instruction,
    tools=[],  # The Synthesizer doesn't fetch data, just reasons.
    output_key="final_report",
)

# The Phase-B workflow graph: sequential handoffs across the four sub-agents.
skeptical_pipeline = SequentialAgent(
    name="skeptical_pipeline",
    sub_agents=[sec_agent, metrics_agent, search_agent, analysis_agent],
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


async def _run_pipeline_async(run_id: str, ticker: str, company_name: str):
    """Run the SequentialAgent graph for one ticker. Returns (state, usage) where
    state holds sec_data/metrics_data/search_data/final_report and usage holds the
    aggregated token counts for this ticker's model calls."""
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=skeptical_pipeline, session_service=session_service)
    session_id = f"{run_id}:{ticker}"

    # Seed per-ticker context so each sub-agent instruction can template it.
    await session_service.create_session(
        app_name=APP_NAME,
        user_id="orchestrator",
        session_id=session_id,
        state={"ticker": ticker, "company_name": company_name or ticker},
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


def run_pipeline(run_id: str, ticker: str, company_name: str):
    """Synchronous wrapper around the Phase-B graph with 429 exponential backoff.
    Returns (state, usage). Note: tokens from a failed attempt before a 429 are
    not counted (the retry re-runs the whole ticker); this is a minor undercount.

    A 429 mid-sequence re-runs the whole ticker pipeline; with the analysis
    agent on flash plus throttling this should be rare.
    """
    for attempt in range(MAX_AGENT_RETRIES):
        try:
            state, usage = asyncio.run(_run_pipeline_async(run_id, ticker, company_name))
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
    """Extract BUY/SELL/HOLD from the report's '## Final Verdict' section.

    Anchors on the explicit 'Verdict: X' declaration rather than a naive
    substring search — the justification prose often mentions the word 'buy'
    when weighing the bull case, which would misclassify a Hold.
    """
    section = report_text.split("## Final Verdict")[-1]
    m = re.search(r"verdict[^A-Za-z]{0,12}(buy|sell|hold)", section, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    lowered = section.lower()
    positions = {w: lowered.find(w) for w in ("buy", "sell", "hold") if lowered.find(w) != -1}
    return min(positions, key=positions.get).upper() if positions else "HOLD"


def analyze_ticker(run_id: str, ticker: str, company_name: str) -> dict:
    """Run the Phase-B SequentialAgent graph for one ticker and persist its
    outputs and final report. Returns the ticker's token-usage dict (zero usage
    if the pipeline failed).

    Assumes the parent pipeline_runs row for `run_id` already exists (the
    agent_outputs/final_reports tables have a FK onto it).
    """
    logger.info(f"[{ticker}] Running skeptical decomposer pipeline...")
    try:
        state, usage = run_pipeline(run_id, ticker, company_name)
    except Exception as e:
        logger.error(f"[{ticker}] Skipping after pipeline failure: {e}")
        return _new_usage()

    _log_usage(ticker, usage)

    sec_data = state.get("sec_data", "") or ""
    metrics_data = state.get("metrics_data", "") or ""
    search_data = state.get("search_data", "") or ""
    report_text = state.get("final_report", "") or ""

    # Persist each sub-agent's raw output (spec section 5B)
    _check_db(db_store_agent_output(run_id, ticker, "SEC_DATA", sec_data, json.dumps({"ticker": ticker})), f"{ticker} SEC output")
    _check_db(db_store_agent_output(run_id, ticker, "QUANT_METRICS", metrics_data, json.dumps({"ticker": ticker})), f"{ticker} metrics output")
    _check_db(db_store_agent_output(run_id, ticker, "SEARCH_BEAR", search_data, json.dumps({"ticker": ticker})), f"{ticker} search output")

    if not report_text:
        logger.error(f"[{ticker}] No final report produced; skipping report persistence.")
        return usage

    verdict = _extract_verdict(report_text)
    _check_db(db_store_final_report(run_id, ticker, verdict, report_text), f"{ticker} final report")

    report_path = os.path.join("reports", f"{ticker}_Skeptical_Analysis.md")
    with open(report_path, "w", encoding='utf-8') as f:
        f.write(report_text)

    logger.info(f"[{ticker}] Completed Analysis ({verdict}) and saved to {report_path}")
    return usage


# --- 4. ORCHESTRATOR WORKFLOWS ---
def run_orchestrator():
    """Full pipeline: Phase A screener -> Phase B analysis over the Top N."""
    run_id = str(uuid.uuid4())
    logger.info(f"Starting Workflow Run: {run_id}")

    # Phase A: Screener
    # We invoke the screener tool directly (deterministic JSON) rather than via
    # the LLM screener_agent — the agent only wraps this same tool, and going
    # direct avoids running the full FMP universe scan (and an LLM call) twice.
    logger.info("Executing Phase A: Screener...")
    try:
        top_candidates = json.loads(run_magic_formula_screener())
    except Exception as e:
        logger.error(f"Failed to get screener results: {e}")
        return

    top_candidates = top_candidates[:top_n]
    logger.info(f"Phase A Complete. Retrieved {len(top_candidates)} candidates.")

    # Create the parent pipeline_runs row BEFORE any agent_outputs/final_reports
    # inserts — those tables have a foreign key onto pipeline_runs(run_id).
    tickers = [c.get("Symbol") for c in top_candidates if c.get("Symbol")]
    _check_db(db_create_pipeline_run(run_id, tickers), "create pipeline run")

    # Phase B: Skeptical Decomposer Analysis (ADK SequentialAgent graph per ticker)
    run_usage = _new_usage()
    for idx, candidate in enumerate(top_candidates):
        ticker = candidate.get("Symbol")
        company_name = candidate.get("CompanyName")
        logger.info(f"--- Processing {idx+1}/{len(top_candidates)}: {ticker} ({company_name}) ---")
        _merge_usage(run_usage, analyze_ticker(run_id, ticker, company_name))

    _finalize_run(run_id, run_usage, "Workflow Run")


def run_single_ticker(ticker: str, company_name: str = None):
    """On-demand Phase B: run the skeptical analysis for a single arbitrary
    ticker, bypassing Phase A. Logged as its own one-company pipeline run."""
    ticker = ticker.strip().upper()
    company_name = company_name or ticker
    run_id = str(uuid.uuid4())
    logger.info(f"Starting On-Demand Run: {run_id} for {ticker} ({company_name})")

    # Parent pipeline_runs row for just this company (FK target for outputs).
    _check_db(db_create_pipeline_run(run_id, [ticker]), "create pipeline run")

    usage = analyze_ticker(run_id, ticker, company_name)

    _finalize_run(run_id, usage, "On-Demand Run")


if __name__ == "__main__":
    import sys

    # Usage:
    #   python main.py                    -> full pipeline (Phase A screener + Phase B)
    #   python main.py TICKER [Company]   -> on-demand Phase B for one ticker
    if len(sys.argv) > 1:
        arg_ticker = sys.argv[1]
        arg_company = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        run_single_ticker(arg_ticker, arg_company)
    else:
        run_orchestrator()
