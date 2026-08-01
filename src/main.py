import os
import re
import time
import json
import asyncio
import hashlib
import logging
from datetime import datetime
import uuid
import yaml
import pandas as pd
from dotenv import load_dotenv

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
    fmp_stock_news,
    web_search_tool,
    db_create_pipeline_run,
    db_update_pipeline_status,
    db_finalize_pipeline_run,
    db_store_agent_output,
    db_store_final_report,
    db_store_ticker_run,
    db_get_sale_case,
    db_find_reusable_report,
    db_copy_ticker_outputs,
    db_spend_since,
    drain_embedding_usage,
)
# The screener's CSV output path (default source for --from-csv mode) and the
# shared dollar formatter, so amounts read identically in reports and logs.
from magic_formula_starter_screener import OUTPUT_FILENAME, format_money, MAX_PEG

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

# PINNED model version, deliberately not the `gemini-flash-latest` alias.
#
# That alias is one Google moves at each new Flash release. It moved from Gemini
# 2.5 Flash to gemini-3.6-flash without any change here, and per-run cost rose
# ~3.5x silently (input $0.30 -> $1.50/1M, output $2.50 -> $7.50/1M). Pinning makes
# a model change a decision taken here, reviewed against specs/config.yaml pricing,
# rather than one made for us. Read from config so there is a single place to
# change it; the runtime still prices whatever `event.model_version` reports, so a
# mismatch between this pin and the billed model is caught by the cost accounting.
AGENT_MODEL = config.get("agents", {}).get("model", "gemini-3.6-flash")

# LLM pricing (USD per 1M tokens), keyed by the RESOLVED model version the API
# reports — see the commentary in specs/config.yaml. Pricing the alias instead of
# the resolved model is what caused per-run spend to be understated ~2.4x once
# `gemini-flash-latest` moved from Gemini 2.5 Flash to gemini-3.6-flash.
_pricing = config.get("llm_pricing", {})
MODEL_PRICES = _pricing.get("models", {}) or {}
_embed_cfg = _pricing.get("embedding", {}) or {}
EMBED_MODEL = _embed_cfg.get("model", "text-embedding-004")
EMBED_PRICE_INPUT_PER_1M = float(_embed_cfg.get("input_usd_per_1m", 0.0))

# Models seen at runtime with no configured price, and unconfirmed prices already
# warned about. Both are reported once per process rather than per call.
_UNPRICED_MODELS = set()
_UNCONFIRMED_WARNED = set()

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

# sale-advisor-instructions.md drives the Phase C SALE ADVISOR agent. It uses '['/']'
# placeholders (not '{}'), so it is safe to embed in a state-templated instruction.
sale_advisor_path = os.path.join(os.path.dirname(__file__), "sale-advisor-instructions.md")
with open(sale_advisor_path, "r") as f:
    sale_advisor_instructions = f.read()

# --- 2. AGENT DEFINITIONS (Phase B) ---
# Two kinds of steps:
#   1. Pure data relays (SEC 10-K, FMP metrics) — deterministic tool calls invoked
#      DIRECTLY in analyze_ticker (100% fidelity, 0 tokens), seeded into session
#      state as sec_data / metrics_data (shared by both advocates below).
#   2. Reasoning — FOUR roles run in order over one shared session, handing off
#      via session state (PIPELINE_AGENTS; see _run_pipeline_async for why this
#      is a list rather than a SequentialAgent):
#         bear_agent  (research-instructions.md) -> state['bear_data']
#         bull_agent  (bullish-research-instructions.md) -> state['bull_data']
#                     (runs after bear so it can refute it)
#         analyst_agent (neutral judge) reads {bear_data}/{bull_data} -> final_report
# ticker, company_name, sec_data, metrics_data, screen_context are seeded before
# the graph runs; each agent templates the keys it references.
APP_NAME = "skeptical_decomposer"

# Shared prompt fragment: the same recency mandate is given to bear, bull, and
# analyst so no role can build a case on annual data alone. Added after a review
# found every agent arguing from FY2023-FY2025 trends while the two most recent
# quarters were contracting -- the annual feed simply could not show it.
RECENCY_MANDATE = (
    "## Recency requirement (mandatory)\n"
    "QUARTERLY_DATA below holds two tables: the last 8 quarters of figures (USD "
    "millions, newest first), and the year-over-year percentage change of each "
    "recent quarter against the SAME quarter one year earlier. METRICS_DATA is "
    "ANNUAL and cannot show recent deterioration.\n"
    "- You MUST examine the most recent quarter before writing any conclusion.\n"
    "- You MUST state plainly whether the most recent quarter CONFIRMS or "
    "CONTRADICTS the multi-year annual trend, and quote the specific year-over-year "
    "percentage changes in revenue, operating income, and operating cash flow.\n"
    "- A deteriorating recent quarter is present-tense evidence. Do not discount it "
    "as noise, and do not let a healthier annual trend paper over it.\n"
    "- If quarterly data is unavailable, say so explicitly and treat it as a gap in "
    "the evidence -- never as confirmation that recent quarters were fine.\n\n"
)

# Shared prompt fragment: figures the pipeline computed deterministically from the
# filings. Agents may not contradict these. Added after a review found a bear case
# asserting $36.9B of debt for a company carrying $29.3B -- the wrong figure then
# propagated into the bull refutation, the final verdict, and a sell trigger that
# was calibrated against a number that never existed.
# Reference data blocks, templated into each agent's instruction.
#
# An attempt was made to move these out of the instruction and into the first
# conversation message, so that ADK could build an explicit Gemini cache (a cache
# cannot be created from a system instruction alone — the API returns "contents are
# required", which is why ADK's every attempt failed silently). Measured on FISV,
# that restructure made things WORSE: cached share fell 32% -> 6% and cost rose
# $0.32 -> $0.43 per run.
#
# The reason is that the long, stable system instruction was precisely what Gemini's
# IMPLICIT caching had been matching on. Moving the bulk into `contents` — which
# grows and shifts as each agent appends to the session — destroyed the stable
# prefix and bought nothing, because no explicit cache was created either.
#
# So the data stays here, in the instruction, where implicit caching demonstrably
# works. See specs/agent_architecture.md §5B.
# Given to every reasoning agent.
REFERENCE_DATA_BLOCKS = (
    "<MAGIC_FORMULA_CONTEXT>\n{screen_context}\n</MAGIC_FORMULA_CONTEXT>\n\n"
    "<VERIFIED_FIGURES>\n{verified_figures}\n</VERIFIED_FIGURES>\n\n"
    "<QUARTERLY_DATA>\n{quarterly_data}\n</QUARTERLY_DATA>\n\n"
)

# Given only to the two research advocates. The judge works from their arguments
# plus the verified figures, and the sale advisor from the finished report — neither
# needs the raw filing extract or the annual ratio dump, and sending it to them
# would add ~10k tokens per turn for nothing.
RESEARCH_SOURCE_BLOCKS = (
    "<METRICS_DATA>\n{metrics_data}\n</METRICS_DATA>\n\n"
    "<SEC_DATA>\n{sec_data}\n</SEC_DATA>\n"
)

VERIFIED_FIGURES_MANDATE = (
    "## Verified figures (authoritative)\n"
    "VERIFIED_FIGURES below were computed directly from the company's filings by "
    "this pipeline, not by a language model. They override any conflicting number "
    "you find in news, web search, or your own recollection.\n"
    "- You MUST NOT state a total debt, cash, market capitalisation, enterprise "
    "value, shares-outstanding, price-to-earnings, earnings-growth or PEG figure "
    "that contradicts VERIFIED_FIGURES.\n"
    "- If a source you cite disagrees with a verified figure, use the verified "
    "figure and note the discrepancy rather than silently preferring the source.\n"
    "- Do not confuse market capitalisation with total debt: they are different "
    "line items and a plausible-looking coincidence between them is a sign you have "
    "mixed them up.\n\n"
)


# The four roles share ONE session so that `output_key` handoffs work, which means
# the session's event log accumulates every earlier role's turns. Under ADK's default
# `include_contents='default'` each agent is then sent the WHOLE prior transcript,
# rewritten as user-role context ("For context: [bear_agent] said: ...", and one entry
# per tool call carrying the full Tavily/FMP payload). That is pure waste here: no
# agent reads the transcript. Every input they use is templated from session state --
# {bear_data}, {bull_data}, {final_report}, {verified_figures}, {quarterly_data} --
# so the replay only duplicated, in raw form, what the instruction already carried in
# distilled form, and it landed hardest on the LAST agents (the sale advisor was
# receiving bear + bull + analyst in full).
#
# 'none' sends the current turn only: the user message plus this agent's own tool
# calls and responses, which is exactly what a tool loop needs to function.
#
# Measured on FISV, same verdict (WATCH) both times: 183,202 -> 129,617 input tokens,
# LLM spend $0.3881 -> $0.2869 (-26%), run total $0.4201 -> $0.3349. The saving scales
# with position in the pipeline, which is the signature of history replay:
#   bear    37,303 -> 56,289  (unaffected -- it runs first, so there is no history to
#                              replay; the rise is one extra tool round trip, and the
#                              per-call prompt is flat at 18.6k -> 18.8k)
#   bull    66,687 -> 50,986  (-24%)
#   analyst 29,599 -> 11,355  (-62%)
#   sale    49,613 -> 10,987  (-78%)
# Caveat: live web search makes runs non-identical (2 vs 3 Tavily calls here), so
# treat the per-agent figures as indicative and the direction as established.
#
# Note this does NOT silence the "Event from an unknown agent" warnings from
# runners.py -- those come from `_find_agent_to_run` scanning the shared session's
# event authors, which happens regardless of what ends up in `contents`. The warning
# is benign: each per-agent Runner has no sub-agents, so the scan finds nothing it
# recognises and correctly falls through to its own root agent.
PIPELINE_INCLUDE_CONTENTS = "none"


# --- Advocate 1: BEAR (skeptical research) ---
bear_agent = LlmAgent(
    name="bear_agent",
    model=AGENT_MODEL,
    instruction=(
        research_instructions
        + "\n\n## How to run this research\n"
        "You are building the BEAR case for {company_name} ({ticker}). Use your tools: "
        "call fmp_stock_news for recent factual news, web_search_tool for bear-case "
        "risks, short-seller theses, and competitive/regulatory threats, and "
        "fmp_company_profile to CHECK WHAT A COMPANY ACTUALLY SELLS before naming it "
        "as a competitor — a shared sector label is not competition. Answer the "
        "questions above skeptically, using the gathered research plus the financial "
        "data below. Do not invent facts; cite sources.\n\n"
        + RECENCY_MANDATE
        + "Open your '## 3. Key Numbers' section with a MOST RECENT QUARTER table "
        "drawn from QUARTERLY_DATA (revenue, operating income, net income, operating "
        "cash flow, capital expenditure, total debt -- each against the same quarter a "
        "year earlier) BEFORE presenting the three-year annual summary.\n\n"
        + VERIFIED_FIGURES_MANDATE
        + REFERENCE_DATA_BLOCKS
        + RESEARCH_SOURCE_BLOCKS
    ),
    tools=[fmp_stock_news, web_search_tool, fmp_company_profile],
    output_key="bear_data",
    include_contents=PIPELINE_INCLUDE_CONTENTS,
)

# --- Advocate 2: BULL (runs after bear; §4 refutes the bear case) ---
bull_agent = LlmAgent(
    name="bull_agent",
    model=AGENT_MODEL,
    instruction=(
        bullish_instructions
        + "\n\n## How to run this research\n"
        "You are building the BULL case for {company_name} ({ticker}). The required "
        "inputs — Earnings Yield, ROC, and the PEG growth test — are in "
        "MAGIC_FORMULA_CONTEXT below, and the "
        "Bear Case Research Output is in BEAR_CASE below. Use your tools: call "
        "fmp_stock_news for positive catalysts, and web_search_tool for bull-thesis "
        "evidence (moat/pricing power, growth catalysts, historical resilience). Answer "
        "every bullish question above; for Section 4, directly refute the specific "
        "points raised in BEAR_CASE. Do not invent facts; cite sources.\n\n"
        + RECENCY_MANDATE
        + "A refutation that ignores a deteriorating recent quarter is not a "
        "refutation. Where the bear case cites recent-quarter weakness, either "
        "explain concretely why it is temporary (naming the specific mechanism and "
        "when it reverses) or concede it.\n\n"
        + VERIFIED_FIGURES_MANDATE
        + "If the BEAR_CASE states a figure that contradicts VERIFIED_FIGURES, correct "
        "it explicitly in your Section 4 rather than arguing against the wrong number.\n\n"
        "## Distinguish confirmed from speculative\n"
        "When your case rests on a catalyst, label it: CONFIRMED (already reported, "
        "contracted, or filed) or SPECULATIVE (rumoured transaction, press report of "
        "talks, management target for a future year, analyst projection). State each "
        "catalyst's label inline. A rumoured deal is optionality, not a plan.\n\n"
        + REFERENCE_DATA_BLOCKS
        + RESEARCH_SOURCE_BLOCKS
        + "\n<BEAR_CASE>\n{bear_data}\n</BEAR_CASE>\n"
    ),
    tools=[fmp_stock_news, web_search_tool, fmp_company_profile],
    output_key="bull_data",
    include_contents=PIPELINE_INCLUDE_CONTENTS,
)

# --- Judge: NEUTRAL analyst (no skeptical prompt) weighs bull vs bear ---
analyst_agent = LlmAgent(
    name="analyst_agent",
    model=AGENT_MODEL,
    instruction=(
        "You are a NEUTRAL, balanced investment analyst — neither skeptical-by-default "
        "nor promotional. You are given a fully-argued BEAR case and a fully-argued BULL "
        "case for {company_name} ({ticker}), plus the Magic Formula value/quality "
        "context. Weigh them fairly and write a Markdown report with EXACTLY these "
        "sections:\n"
        "## Recent Quarter Check\n"
        "## Bull Case (summary)\n"
        "## Bear Case (summary)\n"
        "## Final Verdict\n"
        "## What Would Make This Wrong\n\n"
        "## Writing style\n"
        "Your reader is a college-educated adult with NO accounting or finance "
        "background — not a professional investor. Write every section in plain, "
        "everyday English: explain what is happening and why it matters to an "
        "ordinary investor's money, not just what the numbers say. When a "
        "technical/financial term genuinely earns its place (e.g. 'gross margin', "
        "'free cash flow', 'return on capital'), you may use it, but immediately "
        "follow it with a short plain-English explanation in brackets, e.g. 'gross "
        "margin (the share of each sales dollar left after making the product)'. "
        "Keep the bracketed term for readers who want it, but never leave a reader "
        "guessing what it means. Avoid jargon, acronyms, and hedge-speak where a "
        "plain sentence would do.\n\n"
        "## Recent Quarter Check (write this section FIRST)\n"
        "Before weighing either case, read QUARTERLY_DATA and report what the most "
        "recent quarter actually did versus the same quarter a year earlier: revenue, "
        "operating income, net income, operating cash flow, capital expenditure. Quote "
        "the year-over-year percentages. Then state in one sentence whether the recent "
        "quarter CONFIRMS or CONTRADICTS the longer-term annual picture the two cases "
        "argue over. If it contradicts, that finding carries into the verdict — you may "
        "not reach the Final Verdict without addressing it.\n\n"
        + VERIFIED_FIGURES_MANDATE
        + "Where BEAR_CASE and BULL_CASE disagree on a figure covered by "
        "VERIFIED_FIGURES, the verified figure wins and you should say so plainly.\n\n"
        "## What every candidate already has (so it cannot be your argument)\n"
        "EVERY company you are given has already cleared a screen requiring a high "
        "return on capital, a high earnings yield, demonstrated multi-year growth in "
        "earnings per share off a genuinely profitable base, and a PEG below the "
        "ceiling. Strong returns, a low multiple relative to growth, and a healthy "
        "recent quarter are therefore TRUE OF EVERY CANDIDATE. They are the entry "
        "ticket, not the case.\n"
        "- Citing them is not weighing anything. A verdict whose reasoning could be "
        "copied onto any other screened company has not done the work.\n"
        "- Your job is to find what distinguishes THIS company from the others that "
        "also passed: where it sits WITHIN the screen's ranges (a PEG of 1.45 against "
        "a 1.5 ceiling is a very different fact from 0.25), what the bear case has "
        "found that a quantitative screen cannot see, and whether anything has changed "
        "since the filings the screen was computed from.\n"
        "- If the screen's own figures are the strongest thing you can say about a "
        "company, that is a 'Watch', not a 'Buy'.\n\n"
        "## How to weigh evidence\n"
        "Apply the same standard to both cases:\n"
        "- Realised results outrank projections. Something that has already happened "
        "(a reported decline, a completed refinancing, a signed contract) carries more "
        "weight than something expected to happen. **This is a rule about the quality "
        "of evidence, NOT a licence to dismiss the future.** A risk that has not yet "
        "reached the income statement is not thereby refuted — regulation, patent "
        "expiry, technological substitution and funding cycles all show up in reported "
        "results late, by which point the price has usually moved. If your reasoning "
        "reduces to 'the risk is real but is not yet impairing results', you have "
        "deferred the question rather than answered it: say what would have to be true "
        "for the risk NOT to arrive, and whether you actually believe it.\n"
        "- Label the bull case's catalysts CONFIRMED or SPECULATIVE. A rumoured "
        "transaction, a press report of talks, a management target for a future year, "
        "and an analyst price target are all SPECULATIVE.\n"
        "- A 'Buy' may not rest on a SPECULATIVE catalyst. If removing every "
        "speculative catalyst collapses the bull case, the verdict is at best 'Watch'. "
        "Speculative catalysts are upside on top of a case that already stands up, "
        "never the foundation of one.\n"
        "- 'Priced in' is a claim, not an argument. If you conclude a risk is already "
        "reflected in the valuation, say what specifically tells you that, or drop the "
        "claim.\n"
        "- Return on Capital from the Magic Formula screen excludes goodwill and "
        "acquired intangibles. For a company built by acquisition this makes the "
        "headline figure very large while saying nothing about the return earned on the "
        "purchase price. Where VERIFIED_FIGURES reports a goodwill-inclusive ROIC "
        "alongside it, cite BOTH, and do not describe the business as exceptionally "
        "capital-efficient on the strength of the screen figure alone.\n"
        "- This screen is Greenblatt's two ratios PLUS Peter Lynch's PEG test, and the "
        "ranking uses all three. State the company's PEG ratio and earnings-per-share "
        "growth rate (both in VERIFIED_FIGURES) in the body of the Final Verdict — "
        "never as its opening sentence (see the structural requirement below) and "
        "never as the reason for the verdict. What "
        "matters is WHERE IN THE RANGE it sits: at or below 1.0, the price paid for "
        "each dollar of profit is no higher than the rate that profit has been "
        "growing; approaching the screen's ceiling, the company only just qualified, "
        "which is a materially weaker starting point and must be said plainly rather "
        "than dressed up in the same sentence you would use for a PEG of 0.3. If the "
        "PEG could not be computed, say so and say why rather than passing over it.\n"
        "- The PEG is backward-looking: it measures growth already delivered over the "
        "last few fiscal years and assumes nothing about the future. Where the recent "
        "quarter contradicts that multi-year growth, the QUARTER is the present-tense "
        "evidence and the PEG is the stale one — say so plainly rather than letting a "
        "low PEG vouch for a business that is currently shrinking.\n\n"
        "## How to open the Final Verdict (structural requirement)\n"
        "The FIRST SENTENCE of '## Final Verdict' must name **the single fact that "
        "decided it** — the specific thing about THIS company that tipped the balance "
        "one way or the other. Write that sentence before you write anything else in "
        "the section.\n"
        "- It must NOT be the screen's own figures: rank, return on capital, earnings "
        "yield, PEG, or growth rate. Those are identical in kind for every candidate "
        "you will ever be given, so opening with them frames the verdict as a "
        "restatement of the screen rather than a judgement about the company.\n"
        "- The figures still belong in the section — as SUPPORT for that opening "
        "claim, in the body, not as the claim itself.\n"
        "- Test it: if your first sentence could be moved to a different screened "
        "company without alteration, it is the wrong first sentence.\n\n"
        "## Verdict stance\n"
        "Assume your reader is a middle-class, mid-career professional investing "
        "their own hard-earned savings — not a millionaire, billionaire, or "
        "institution that can shrug off a loss. Someone mid-career has years to ride "
        "out volatility and can reasonably accept moderate, well-reasoned risk, so do "
        "not treat every uncertainty as disqualifying. Equally, do not let an "
        "attractive story or a good screen score pull an unproven case up to 'Buy'. "
        "**The two failure directions are symmetric: a wrongly cautious 'Watch' costs "
        "the reader an opportunity, a wrongly confident 'Buy' costs them money.** "
        "Weigh them as equals.\n"
        "All three verdicts are live options and each should be reached regularly. If "
        "you find yourself returning 'Buy' for every company you see, you have stopped "
        "discriminating — these candidates all passed the same screen, and the screen "
        "has already been applied. In the Final "
        "Verdict, explicitly weigh the bull case against the bear case, then state "
        "exactly one verdict for an investor who does NOT currently own the stock:\n"
        "- 'Verdict: Buy'   = passes the screen on its own merits; a reasonable "
        "candidate to include in a DIVERSIFIED Magic Formula basket.\n"
        "- 'Verdict: Watch' = not compelling enough to buy now; watchlist.\n"
        "- 'Verdict: Avoid' = actively unattractive; do not buy.\n"
        # Listed flush-left on purpose. When these three lines were indented two
        # spaces for readability, the model copied the indentation into the report and
        # the verdict line rendered as an indented block instead of a paragraph.
        "Write the verdict line in exactly one of these three forms, including the "
        "qualifier and with no leading spaces. These are alternatives of equal "
        "standing — the first is not a default:\n"
        "**Verdict: Buy** — screen pass; candidate for a diversified basket, not a "
        "single-stock recommendation.\n"
        "**Verdict: Watch** — passes the screen but the case is not compelling "
        "enough to buy today; worth monitoring.\n"
        "**Verdict: Avoid** — passes the screen, but the evidence argues against "
        "buying it.\n"
        "Follow the verdict with a one-paragraph justification, in the plain-language "
        "style above, of how the two cases net out — naming the specific thing that "
        "tipped it, not a restatement of the screen's figures. Do not choose 'Watch' "
        "merely to avoid committing, and do not choose 'Buy' merely because the "
        "company reached this stage. Do not invent facts beyond the two cases "
        "provided.\n\n"
        "## Basket framing (mandatory, applies to every verdict)\n"
        "The Magic Formula is a BASKET strategy. Greenblatt's own method buys 20–30 "
        "screened companies and holds them about a year; it works because the winners "
        "outweigh the individual names that turn out to be value traps. A verdict here "
        "is a judgement about ONE name and cannot be de-risked by diversification the "
        "reader has not actually done. In the Final Verdict you MUST include one plain "
        "sentence telling the reader that this is a single screened candidate rather "
        "than a standalone recommendation, and that the strategy's historical results "
        "depend on holding many such names, not one.\n\n"
        "## What Would Make This Wrong (mandatory final section)\n"
        "Regardless of the verdict, close with the single most likely way it turns out "
        "to be wrong. Name ONE specific, observable development — not 'macro "
        "conditions' or 'execution risk' — and say what a reader would see in a future "
        "earnings report if it were happening. For a 'Buy', this is the thing most "
        "likely to break the case; for 'Watch' or 'Avoid', the thing most likely to "
        "prove you too cautious. Do not use the word 'Verdict' in this section.\n"
        "**When you point the reader at a future quarter, check the seasonality first.** "
        "QUARTERLY_DATA gives you eight quarters with their period-end dates, so you can "
        "see how the company's year is actually shaped. Name the quarter AND its period "
        "end, and if the business is seasonal, say whether that one quarter captures the "
        "whole season or only part of it — if only part, name the quarters that together "
        "do. A tax preparer whose fiscal Q3 ends 31 March has booked most of the filing "
        "season but not its final fortnight, and an investor told to judge the year on "
        "Q3 alone has been pointed at an incomplete picture. The same applies to any "
        "retailer, or to any company whose quarters are visibly uneven in "
        "QUARTERLY_DATA.\n\n"
        + REFERENCE_DATA_BLOCKS
        + "<BEAR_CASE>\n{bear_data}\n</BEAR_CASE>\n\n"
        "<BULL_CASE>\n{bull_data}\n</BULL_CASE>\n"
    ),
    tools=[],
    output_key="final_report",
    include_contents=PIPELINE_INCLUDE_CONTENTS,
)

# --- Phase C: SALE ADVISOR (runs after the analyst; reads {final_report}) ---
# Assumes the stock is ALREADY OWNED (ignores the verdict) and advises on the
# specific, measurable business events that would break the original thesis and
# justify selling. Uses fmp_stock_news + Tavily to ground the analysis in real
# recent developments. Its output is persisted as SALE_CASE.
sale_advisor_agent = LlmAgent(
    name="sale_advisor_agent",
    model=AGENT_MODEL,
    instruction=(
        sale_advisor_instructions
        + "\n\n## How to run this analysis\n"
        "You are the SALE ADVISOR for {company_name} ({ticker}). The neutral analyst's "
        "FINAL_REPORT below contains the bull thesis, bear thesis, and a verdict. "
        "IGNORE the verdict and ASSUME the investor already owns the stock. Use your "
        "tools: call fmp_stock_news for recent factual developments and web_search_tool "
        "for adverse business events and thesis-breaking signals. Then write a Markdown "
        "report titled '## Sale Advisory' that names the THREE specific business events "
        "(NOT price movements) that would signal the original investment case is broken "
        "and justify selling. Where possible attach concrete, measurable thresholds "
        "(e.g. gross margin dropping below X%, two consecutive quarters of negative user "
        "growth, loss of a key distribution contract). Do not invent facts; cite "
        "sources.\n\n"
        + VERIFIED_FIGURES_MANDATE
        + "Your sell triggers are only as good as the baseline they are measured from. "
        "Every numeric threshold you set MUST be anchored to a figure in "
        "VERIFIED_FIGURES or QUARTERLY_DATA, and you MUST state the current actual "
        "value next to each threshold so the investor can see how much headroom is "
        "left (e.g. 'total debt above $32B — currently $29.3B'). A threshold that the "
        "current figures already satisfy, or that sits so far from them it could never "
        "realistically fire, is worse than useless: check each one against the actual "
        "value before you write it.\n\n"
        "## Seasonality — check it before writing any quarter-based trigger\n"
        "QUARTERLY_DATA gives you eight quarters with their period-end dates. Read the "
        "shape of the year before you write a threshold that counts quarters.\n"
        "- Many businesses are structurally uneven. A tax preparer earns almost "
        "everything in the quarter ending 31 March and LOSES money in the two quarters "
        "before it, every single year. A trigger reading 'two consecutive quarters of "
        "negative net income' would fire annually on such a company, in its best years "
        "as well as its worst, and would tell the investor to sell a healthy business.\n"
        "- So compare like with like: a quarter against the SAME quarter one year "
        "earlier, never against the quarter immediately before it, unless you have "
        "checked that the company's quarters are genuinely comparable.\n"
        "- **Test every trigger against the eight quarters you were given.** If it "
        "would have fired at some point in that window while the business was "
        "performing normally, it is broken — rewrite it. Say explicitly which quarter "
        "you are measuring and what its period end is, so the investor knows when to "
        "look.\n\n"
        + "<VERIFIED_FIGURES>\n{verified_figures}\n</VERIFIED_FIGURES>\n\n"
        "<QUARTERLY_DATA>\n{quarterly_data}\n</QUARTERLY_DATA>\n\n"
        "<FINAL_REPORT>\n{final_report}\n</FINAL_REPORT>\n"
    ),
    tools=[fmp_stock_news, web_search_tool],
    output_key="sale_data",
    include_contents=PIPELINE_INCLUDE_CONTENTS,
)

# The reasoning graph: bear -> bull -> neutral analyst (Phase B) -> sale advisor
# (Phase C). SEC and metrics are gathered by direct tool calls in analyze_ticker,
# not by agents. The sale advisor runs last so it can read the analyst's
# {final_report} (bull + bear thesis + verdict) from session state.
# Run in order, each against the SAME session so `output_key` handoffs work exactly
# as they did inside SequentialAgent. Kept as a plain list rather than a
# SequentialAgent so `_run_pipeline_async` can retry ONE failed role on a 429
# instead of re-running (and re-paying for) every role before it.
PIPELINE_AGENTS = [bear_agent, bull_agent, analyst_agent, sale_advisor_agent]

# --- Phase C follow-up: SELL-CONDITION CHECK (standalone, on-demand) ---
# A separate single-stock flow. It loads the ticker's most recent SALE_CASE (the
# measurable sell triggers the sale advisor defined) and tests, against CURRENT
# data, whether any/all of them are now met — advising Sell vs. Hold for someone
# who already owns the stock. It is NOT part of the analysis graph; it is run on
# demand for one ticker via `run_sell_check`. Reads {sale_conditions} (from the DB)
# and {metrics_data} (a fresh direct FMP call), plus live news/web research.
sell_check_agent = LlmAgent(
    name="sell_check_agent",
    model=AGENT_MODEL,
    instruction=(
        "You are a SELL-CONDITION EVALUATOR for {company_name} ({ticker}). A previous "
        "analysis produced a set of SALE CONDITIONS — specific, measurable business "
        "events that would break the investment thesis and justify selling. Your job is "
        "to determine, using CURRENT data, whether each condition is now MET.\n\n"
        "Use your tools: call fmp_stock_news for the latest developments (earnings, "
        "guidance changes, impairments, contract losses, downgrades) and web_search_tool "
        "for anything not in the news feed. The latest FMP fundamentals are in "
        "CURRENT_METRICS below. Compare the current figures/events against each "
        "condition's threshold.\n\n"
        "CURRENT_METRICS is ANNUAL. Sale conditions are frequently written in "
        "quarterly terms ('two consecutive quarters of...'), which annual data cannot "
        "answer — use QUARTERLY_DATA below, which holds the last 8 quarters with "
        "year-over-year comparisons, to evaluate any condition expressed in quarters. "
        "Quote the specific quarter and figure as your evidence.\n\n"
        "Write a Markdown report titled '## Sell-Condition Check'. For EACH sale "
        "condition, output:\n"
        "- **Condition** (restated briefly)\n"
        "- **Status:** MET / NOT MET / UNCLEAR\n"
        "- **Evidence:** the current figure or event vs. the threshold, with sources\n\n"
        "Then write a '## Sell Recommendation' section that states:\n"
        "- How many conditions are MET (e.g. '1 of 3 conditions met').\n"
        "- Exactly one recommendation line: 'Recommendation: SELL' if ANY condition is "
        "clearly MET (the original thesis is broken), otherwise 'Recommendation: HOLD'.\n"
        "- A one-paragraph justification of how the met/unmet conditions net out. Do not "
        "invent facts; if data is unavailable say UNCLEAR and do not treat it as met. "
        "Cite sources.\n\n"
        "<SALE_CONDITIONS>\n{sale_conditions}\n</SALE_CONDITIONS>\n\n"
        "<QUARTERLY_DATA>\n{quarterly_data}\n</QUARTERLY_DATA>\n\n"
        "<CURRENT_METRICS>\n{metrics_data}\n</CURRENT_METRICS>\n"
    ),
    tools=[fmp_stock_news, web_search_tool],
    output_key="sell_check",
)

# --- 2b. AGENT EXECUTION HELPER ---
# Gemini API returns HTTP 429 (RESOURCE_EXHAUSTED) when the per-minute request
# or token quota is exceeded — especially for gemini-2.5-pro, whose free/standard
# tier limits are low and which the analysis agent hits once per ticker with a
# large prompt. We retry with exponential backoff and throttle between calls.
MAX_AGENT_RETRIES = 5
BASE_BACKOFF_SECONDS = 20
INTER_CALL_DELAY_SECONDS = 2
INTER_AGENT_DELAY_SECONDS = 1

# --- Context caching ----------------------------------------------------------
# Gemini bills a repeated prompt prefix at $0.15/1M instead of $1.50/1M. Two ways to
# get it: IMPLICIT (automatic prefix matching) and EXPLICIT (a pinned CachedContent).
#
# Explicit caching is DISABLED by default, on evidence. An explicit cache cannot be
# built from a system instruction alone — the API rejects it with "contents are
# required" — and all of our data lives in the agent instruction, so ADK's every
# attempt failed silently on every turn.
#
# The obvious fix, moving the data block into the first conversation message so it
# lands in `contents`, was implemented and measured on FISV. It was WORSE: cached
# share fell 32% -> 6% and cost rose $0.32 -> $0.43 per run. The long, stable
# system instruction turned out to be exactly what implicit caching had been
# matching on; relocating it into `contents` (which grows as each agent appends to
# the session) destroyed that stable prefix and bought no explicit cache either.
#
# So: leave the data in the instruction, let implicit caching do its job (~32% of
# prompt tokens), and keep this switch here for when ADK or the API supports
# caching a system instruction directly. See specs/agent_architecture.md §5B.
#
# ttl_seconds: cache storage bills per token-hour, so this covers one ticker only.
# min_tokens: below this, storage costs more than it saves.
# cache_intervals: invocations reusing one cache before it is refreshed.
CONTEXT_CACHE = ContextCacheConfig(
    ttl_seconds=int(config.get("context_cache", {}).get("ttl_seconds", 900)),
    min_tokens=int(config.get("context_cache", {}).get("min_tokens", 4096)),
    cache_intervals=int(config.get("context_cache", {}).get("cache_intervals", 10)),
)
CONTEXT_CACHE_ENABLED = bool(config.get("context_cache", {}).get("enabled", True))


def _build_runner(agent, session_service) -> Runner:
    """Runner for one agent, with context caching attached via App.

    Caching is configured on the App rather than the Runner, so each agent gets an
    App wrapper. Falls back to a plain Runner if caching is disabled in config, so
    the feature can be switched off without touching code."""
    if not CONTEXT_CACHE_ENABLED:
        return Runner(app_name=APP_NAME, agent=agent, session_service=session_service)
    app = App(name=APP_NAME, root_agent=agent, context_cache_config=CONTEXT_CACHE)
    return Runner(app=app, session_service=session_service)


def _is_rate_limit_error(err: Exception) -> bool:
    text = str(err).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "RATE" in text and "LIMIT" in text


# --- Token usage / cost accounting ---------------------------------------
# --- Budget guard -------------------------------------------------------------
_budget = config.get("budget", {}) or {}
BUDGET_ENABLED = bool(_budget.get("enabled", True))
BUDGET_PER_RUN_USD = float(_budget.get("per_run_usd", 15.0))
BUDGET_PER_DAY_USD = float(_budget.get("per_day_usd", 25.0))
BUDGET_DAY_WINDOW_HOURS = int(_budget.get("day_window_hours", 24))
BUDGET_ON_EXCEED = str(_budget.get("on_exceed", "halt")).lower()


class BudgetExceeded(Exception):
    """Raised to stop a run cleanly when a spend ceiling is hit. Work already
    completed is persisted; only the remaining tickers are skipped."""


def _prior_day_spend() -> float:
    """Spend already recorded in the rolling window, from the DB. Excludes the
    current run, whose row is only finalized at the end."""
    try:
        return float(json.loads(db_spend_since(BUDGET_DAY_WINDOW_HOURS)).get("total_usd", 0.0))
    except Exception as e:
        # A guard that fails open is worse than no guard being claimed, so say so.
        logger.warning(f"Could not read recent spend for the budget guard: {e}. "
                       f"Proceeding with the per-run ceiling only.")
        return 0.0


def _check_budget(run_usage: dict, prior_day_usd: float, next_label: str) -> None:
    """Enforce the spend ceilings before starting another ticker.

    Checked between tickers rather than mid-ticker: stopping half way through a
    ticker would leave a partial analysis and still have paid for it."""
    if not BUDGET_ENABLED:
        return
    run_cost = _total_cost(run_usage)
    day_cost = prior_day_usd + run_cost

    breach = None
    if run_cost >= BUDGET_PER_RUN_USD:
        breach = (f"per-run ceiling reached: ${run_cost:.2f} of ${BUDGET_PER_RUN_USD:.2f}")
    elif day_cost >= BUDGET_PER_DAY_USD:
        breach = (f"{BUDGET_DAY_WINDOW_HOURS}h ceiling reached: ${day_cost:.2f} of "
                  f"${BUDGET_PER_DAY_USD:.2f} (${prior_day_usd:.2f} before this run)")
    if not breach:
        return

    if BUDGET_ON_EXCEED == "halt":
        raise BudgetExceeded(f"{breach}. Stopping before {next_label}. "
                             f"Raise budget.per_run_usd / budget.per_day_usd in "
                             f"specs/config.yaml to continue.")
    logger.warning(f"BUDGET: {breach}. Continuing because budget.on_exceed is "
                   f"'{BUDGET_ON_EXCEED}' — set it to 'halt' to stop instead.")


# --- Duplicate-run skip --------------------------------------------------------
_reuse = config.get("reuse", {}) or {}
REUSE_ENABLED = bool(_reuse.get("enabled", True))
REUSE_MAX_AGE_HOURS = int(_reuse.get("max_age_hours", 24))


def _prompt_version() -> str:
    """Short hash of every agent instruction plus the shared prompt fragments.

    Any edit to a prompt changes this, which invalidates reuse — otherwise tuning
    the analyst's verdict stance would silently keep serving reports written under
    the old rules."""
    blob = "".join(a.instruction for a in PIPELINE_AGENTS)
    blob += RECENCY_MANDATE + VERIFIED_FIGURES_MANDATE + REFERENCE_DATA_BLOCKS
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _analysis_key(ticker: str, candidate: dict, skip_sale_advisor: bool = False) -> str:
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

    Returns "" when the balance sheet date is unknown, which disables reuse for
    that ticker — better to pay again than to serve a report whose provenance we
    cannot establish."""
    bs_date = (candidate or {}).get("BalanceSheetDate")
    if not bs_date:
        return ""
    variant = "|no-phase-c" if skip_sale_advisor else ""
    raw = f"{ticker}|{bs_date}|{_prompt_version()}{variant}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _new_usage() -> dict:
    """Usage accumulator. `by_model` splits tokens by the resolved model version so
    each is priced at its own rate; the flat input/output/total counters are kept
    for the existing DB columns and log lines. `embed_input` counts embedding
    tokens, which are billed separately and were previously not counted at all."""
    return {
        "input": 0, "output": 0, "total": 0, "requests": 0, "search_requests": 0,
        "cached_input": 0, "embed_input": 0, "embed_requests": 0,
        "by_model": {},
    }


def _model_bucket(usage: dict, model: str) -> dict:
    return usage["by_model"].setdefault(
        model or "unknown",
        {"input": 0, "cached_input": 0, "output": 0, "total": 0, "requests": 0},
    )


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
    tokens_in = um.prompt_token_count or 0
    # Thinking tokens bill at the output rate. gemini-3.6-flash spends the large
    # majority of its output budget here -- a probe showed 388 of 478 output
    # tokens were thoughts -- so dropping them would badly understate cost.
    tokens_out = (um.candidates_token_count or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
    tokens_total = um.total_token_count or 0
    # Repeated prompt prefixes bill as a separate, much cheaper SKU. prompt_token_count
    # INCLUDES these, so full-rate input is (prompt - cached). Ignoring the split
    # overstates cost -- on a real day's billing, cached was 21% of prompt tokens at
    # one tenth the rate.
    tokens_cached = getattr(um, "cached_content_token_count", 0) or 0

    usage["input"] += tokens_in
    usage["cached_input"] += tokens_cached
    usage["output"] += tokens_out
    usage["total"] += tokens_total
    usage["requests"] += 1

    # The alias configured on the agent is not what gets billed; the resolved
    # version is. Bucket by it so each model is priced at its own rate.
    bucket = _model_bucket(usage, getattr(event, "model_version", None))
    bucket["input"] += tokens_in
    bucket["cached_input"] += tokens_cached
    bucket["output"] += tokens_out
    bucket["total"] += tokens_total
    bucket["requests"] += 1


# Embedding responses carry no usage metadata, so tokens are approximated from the
# payload length. 4 chars/token is the usual rule of thumb for English prose; this
# line is a small fraction of total spend, so the approximation is not material —
# but counting it approximately beats the previous behaviour of not counting it.
_CHARS_PER_TOKEN = 4


def _collect_embedding_usage(usage: dict) -> None:
    """Fold embedding calls made since the last collection into `usage`."""
    drained = drain_embedding_usage()
    usage["embed_input"] += drained["chars"] // _CHARS_PER_TOKEN
    usage["embed_requests"] += drained["requests"]


def _merge_usage(acc: dict, other: dict) -> None:
    """Fold `other`'s usage into `acc`.

    `cached_input` MUST be merged along with the rest: `_model_cost` reads it from
    the per-model bucket to bill the cheaper cached-prefix rate, so dropping it here
    silently re-priced every cached token at the full rate once per-ticker usage was
    rolled up into the run total. The per-ticker log lines were right and the RUN
    TOTAL (and the pipeline_runs row, and the budget guard that reads it back) was
    too high by the whole cached discount."""
    for k in acc:
        if k == "by_model":
            for model, b in other.get("by_model", {}).items():
                target = _model_bucket(acc, model)
                for field in ("input", "cached_input", "output", "total", "requests"):
                    target[field] += b.get(field, 0)
        else:
            acc[k] += other.get(k, 0)


def _model_price(model: str):
    """(input_per_1m, cached_input_per_1m, output_per_1m) for a resolved model
    version, or None when the model has no configured price. Returning None rather
    than a default is deliberate: silently falling back to another model's rate is
    exactly how the original 2.4x understatement went unnoticed for so long."""
    entry = MODEL_PRICES.get(model)
    if not entry:
        if model not in _UNPRICED_MODELS:
            _UNPRICED_MODELS.add(model)
            logger.error(
                f"No price configured for model '{model}'. Its tokens are EXCLUDED "
                f"from the cost estimate, so reported spend is too low. Add it under "
                f"llm_pricing.models in specs/config.yaml."
            )
        return None
    if not entry.get("confirmed", False) and model not in _UNCONFIRMED_WARNED:
        _UNCONFIRMED_WARNED.add(model)
        logger.warning(
            f"Price for '{model}' is marked unconfirmed in specs/config.yaml "
            f"(${entry.get('input_usd_per_1m')}/1M in, ${entry.get('output_usd_per_1m')}/1M out). "
            f"Verify against the Cloud Billing SKU lines and set confirmed: true."
        )
    return (
        float(entry.get("input_usd_per_1m", 0.0)),
        float(entry.get("cached_input_usd_per_1m", 0.0)),
        float(entry.get("output_usd_per_1m", 0.0)),
    )


def _model_cost(bucket: dict, price) -> float:
    """Cost for one model's tokens. `prompt_token_count` includes cached tokens, so
    the full rate applies only to (input - cached); the remainder bills at the
    cheaper cached-input rate."""
    p_in, p_cached, p_out = price
    cached = min(bucket.get("cached_input", 0), bucket["input"])
    full = bucket["input"] - cached
    return (full / 1e6) * p_in + (cached / 1e6) * p_cached + (bucket["output"] / 1e6) * p_out


def _llm_cost(usage: dict) -> float:
    """Sum per-model cost. Models without a configured price contribute nothing and
    are logged as errors by _model_price -- the estimate is then a known lower
    bound rather than a wrong number presented as fact."""
    total = 0.0
    for model, b in usage.get("by_model", {}).items():
        price = _model_price(model)
        if price is None:
            continue
        total += _model_cost(b, price)
    return total + _embedding_cost(usage)


def _embedding_cost(usage: dict) -> float:
    """Embedding spend. Four vectors are generated per ticker (bear, bull, sale,
    final report); before this was added they were billed but never counted."""
    return (usage.get("embed_input", 0) / 1e6) * EMBED_PRICE_INPUT_PER_1M


def _search_cost(usage: dict) -> float:
    return (usage["search_requests"] / 1000.0) * TAVILY_PRICE_PER_1K


def _total_cost(usage: dict) -> float:
    """Combined LLM + embedding + web-search spend for a usage accumulator."""
    return _llm_cost(usage) + _search_cost(usage)


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
    # Per-model breakdown. Printed because the agents are configured with a moving
    # alias -- seeing the resolved version in the log is what makes a repointed
    # alias (and the stale price that follows) noticeable instead of silent.
    for model, b in sorted(usage.get("by_model", {}).items()):
        price = _model_price(model)
        cost = ("$%.4f" % _model_cost(b, price)) if price else "UNPRICED"
        cached = b.get("cached_input", 0)
        cached_note = f" (of which {cached:,} cached)" if cached else ""
        logger.info(
            f"[{label}]   {model}: {b['requests']} calls "
            f"in={b['input']:,}{cached_note} out={b['output']:,} -> {cost}"
        )
    if usage.get("embed_input"):
        logger.info(
            f"[{label}]   {EMBED_MODEL}: {usage['embed_requests']} embeddings "
            f"in={usage['embed_input']:,} -> ${_embedding_cost(usage):.4f}"
        )
    return total


def _finalize_run(run_id: str, usage: dict, run_label: str, status: str = "COMPLETED") -> None:
    """Log the run's usage/cost and persist it (with terminal status) to
    pipeline_runs. `status` is BUDGET_EXCEEDED when a spend ceiling stopped the run
    early, so a short run is distinguishable from a completed one in the history."""
    total_cost = _log_usage(f"RUN {run_id} TOTAL", usage)
    _check_db(
        db_finalize_pipeline_run(
            run_id, status,
            usage["requests"], usage["input"], usage["output"], usage["total"],
            usage["search_requests"],
            round(_llm_cost(usage), 6), round(_search_cost(usage), 6), round(total_cost, 6),
        ),
        "finalize pipeline run",
    )
    logger.info(f"{run_label} {run_id} {status}. Estimated spend (LLM+Tavily): ${total_cost:.4f}")


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


def _check_db(result: str, context: str) -> None:
    """DB MCP tools return a status string instead of raising. Previously these
    return values were ignored, so failed inserts were completely silent. Log
    them so persistence failures are visible."""
    if isinstance(result, str) and result.startswith("Error"):
        logger.error(f"DB persistence failed ({context}): {result}")
    else:
        logger.info(f"DB ok ({context}): {result}")


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


def analyze_ticker(run_id: str, ticker: str, company_name: str, screen_context: str = "",
                   candidate: dict = None, force: bool = False,
                   skip_sale_advisor: bool = False) -> dict:
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
    analysis_key = _analysis_key(ticker, candidate, skip_sale_advisor)
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
                                    _present((candidate or {}).get("Final_Rank"))),
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
    verified_figures = _format_verified_figures(candidate)

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
                f"[{ticker}] RECONCILIATION: {f['source']} states {f['field']} of "
                f"{format_money(f['stated'])}, but the filings show "
                f"{format_money(f['verified'])} (off by {f['deviation_pct']}%). "
                f"Context: \"{f['context']}\""
            )
        logger.warning(
            f"[{ticker}] {len(recon_findings)} figure(s) in this report contradict the "
            f"filings; a warning table has been added to the report."
        )
    else:
        logger.info(f"[{ticker}] Reconciliation gate passed (no contradicted figures).")

    # Extract the verdict from the original text (banner/metrics section prepended
    # below don't affect the '## Final Verdict' split), then persist the report
    # with the deterministic Magic Formula Metrics section + run header prepended.
    verdict = _extract_verdict(report_text)
    report_with_metrics = (
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
                            _present((candidate or {}).get("Final_Rank"))),
        f"{ticker} ticker_run",
    )

    report_path = os.path.join("reports", f"{ticker}_Final_Report_{verdict.title()}.md")
    with open(report_path, "w", encoding='utf-8') as f:
        f.write(report_stored)

    # Fold in the embedding calls made by the db_store_* tools above.
    _collect_embedding_usage(usage)

    logger.info(f"[{ticker}] Completed Analysis ({verdict}) and saved to {report_path}")
    return usage


# --- 4. ORCHESTRATOR WORKFLOWS ---
def _format_screen_context(candidate: dict) -> str:
    """Turn a Magic Formula candidate row (or single-ticker computed metrics) into
    the value/quality/growth bull context that the agents weigh against the bear case."""
    rank = candidate.get("Final_Rank")
    if rank is not None:
        header = (
            "This stock was surfaced by a Magic Formula screen (Joel Greenblatt's value "
            "+ quality strategy) extended with Peter Lynch's PEG growth test. A high "
            "rank means it is statistically cheap (high earnings yield), high-quality "
            "(high return on capital), AND growing at a rate its price does not yet "
            "reflect (low PEG)."
        )
        rank_line = f"- Screen rank: #{rank} (lower is better)\n"
    else:
        header = (
            "Magic Formula value/quality metrics and the PEG growth test were computed "
            "on demand for this ticker (it was not ranked against a screen universe). A "
            "high ROC + high earnings yield + a low PEG means it is statistically cheap, "
            "high-quality, and growing."
        )
        rank_line = "- Screen rank: n/a (single-ticker run)\n"
    ctx = (
        header
        + " This is the bullish starting point that must be weighed against the bear case.\n"
        + rank_line
        + f"- Return on Capital (quality signal): {candidate.get('ROC_Pct')}\n"
        + f"- Earnings Yield (cheapness signal): {candidate.get('EY_Pct')}"
    )
    # Lynch's growth signal. Stated as the third leg of the ranking rather than as a
    # footnote, because it is the one that answers the objection the other two invite:
    # a high earnings yield on shrinking earnings is a value trap, not a bargain.
    growth_pct = _present(candidate.get("EPSGrowth_Pct"))
    peg_ratio = _present(candidate.get("PEG_Ratio"))
    if growth_pct or peg_ratio:
        ctx += (
            f"\n- Earnings-per-share growth, compound annual over the last "
            f"{_growth_window(candidate)} fiscal years "
            f"(growth signal): {growth_pct or 'Not available'}"
            f"\n- PEG ratio (P/E divided by that growth rate in percentage points; "
            f"below 1.0 is Lynch's fair-value line, and the screen requires "
            f"{MAX_PEG:g} or better): "
            f"{peg_ratio if peg_ratio is not None else 'Not available'}"
        )
    if candidate.get("MagicFormula_Score") is not None:
        ctx += f"\n- Combined Magic Formula score (ROC + earnings yield ranks): {candidate.get('MagicFormula_Score')}"
    if _present(candidate.get("Composite_Score")) is not None:
        ctx += (f"\n- Composite screen score (those two ranks plus the 1/PEG rank): "
                f"{candidate.get('Composite_Score')}")
    return ctx


def _verified_figures(candidate: dict) -> dict:
    """The subset of screener output that is deterministic ground truth: figures
    read straight off the filings rather than written by a model. Used twice --
    injected into every agent prompt as VERIFIED_FIGURES, and used as the baseline
    the reconciliation gate checks agent prose against."""
    candidate = candidate or {}
    out = {}
    for key in (
        "TotalDebt", "Cash", "TotalEquity", "TotalAssets", "GoodwillAndIntangibles",
        "InvestedCapital", "EnterpriseValue", "LiveMarketCap", "EBIT", "CapitalEmployed",
    ):
        val = candidate.get(key)
        if isinstance(val, (int, float)) and not pd.isna(val):
            out[key] = float(val)
    return out


def _format_verified_figures(candidate: dict) -> str:
    """Human-and-model readable VERIFIED_FIGURES block for the agent prompts.

    Every number here came from the same balance sheet / income statement the
    Magic Formula ratios were computed from, so an agent contradicting this block
    is contradicting the pipeline's own basis for screening the company."""
    candidate = candidate or {}
    figs = _verified_figures(candidate)
    if not figs:
        return (
            "No independently verified figures are available for this ticker (the "
            "Magic Formula metrics could not be computed). Treat every financial "
            "figure you cite as unverified and attribute it to its source."
        )

    def _m(key):
        return format_money(figs[key]) if key in figs else "Not available"

    lines = [
        "Computed directly from the company's most recent filings by this pipeline. "
        "These override any conflicting figure from news, search, or recollection.",
        "",
        f"- Total debt: {_m('TotalDebt')}",
        f"- Cash and short-term investments: {_m('Cash')}",
        f"- Market capitalisation: {_m('LiveMarketCap')}",
        f"- Enterprise value: {_m('EnterpriseValue')}",
        f"- Total shareholders' equity: {_m('TotalEquity')}",
        f"- Total assets: {_m('TotalAssets')}",
        f"- Goodwill and acquired intangibles: {_m('GoodwillAndIntangibles')}",
        f"- Operating profit (EBIT, {candidate.get('EBIT_Basis') or 'basis not stated'}): {_m('EBIT')}",
        f"- Balance sheet as of: {candidate.get('BalanceSheetDate') or 'Not available'}",
    ]

    # The two return figures side by side. Reported together because quoting the
    # Greenblatt ROC alone for an acquisition-built company reliably produces the
    # claim that it is an elite-efficiency business, which the goodwill-inclusive
    # figure usually contradicts by an order of magnitude.
    roc_pct = _present(candidate.get("ROC_Pct"))
    roic_pct = _present(candidate.get("ROIC_InclGoodwill_Pct"))
    intang_pct = _present(candidate.get("IntangiblesShareOfAssets_Pct"))
    roa_pct_vf = _present(candidate.get("ROA_Pct"))
    lines += [
        "",
        "Two different return-on-capital measures — cite BOTH, never the first alone:",
        f"- Magic Formula ROC (EXCLUDES goodwill/intangibles): {roc_pct or 'Not available'}",
        f"- ROIC including goodwill (return on capital actually spent, "
        f"EBIT / (equity + debt - cash)): {roic_pct or 'Not available'}",
        f"- Goodwill and intangibles as a share of total assets: {intang_pct or 'Not available'}",
    ]
    # When the companion cannot be computed, the mandate above would otherwise leave
    # the agent with the headline ROC and nothing to weigh it against — on precisely
    # the companies (negative equity from heavy buybacks) whose headline is most
    # distorted. Substitute ROA explicitly, labelled as what it is.
    if not roic_pct:
        reason = _present(candidate.get("ROIC_Unavailable_Reason"))
        equity_raw = _present(candidate.get("TotalEquity"))
        # Same inference as the reader-facing section: the explicit reason is absent
        # on CSVs written before that column existed, but negative equity is visible.
        if reason is None and isinstance(equity_raw, (int, float)) and equity_raw < 0:
            reason = "negative_invested_capital"
        if reason == "negative_invested_capital":
            lines.append(
                "- NOTE: ROIC including goodwill CANNOT be computed for this company. It "
                "has bought back more stock than its retained earnings cover, so "
                "shareholders' equity is negative and invested capital (equity + debt - "
                "cash) is not positive — there is no denominator to divide by. This is "
                "NOT a data gap and must not be reported as one."
            )
        elif reason:
            lines.append(
                "- NOTE: ROIC including goodwill could not be computed for this company "
                "from the available balance-sheet data."
            )
        if roa_pct_vf:
            lines.append(
                f"- USE INSTEAD as the conservative counterweight — Return on Assets "
                f"(EBIT / TOTAL assets, which INCLUDES goodwill, intangibles and cash): "
                f"{roa_pct_vf}. Cite this alongside the Magic Formula ROC. Do not call it "
                f"ROIC — it is a different, blunter measure — but it serves the same "
                f"purpose here: the Magic Formula ROC excludes the purchase price of past "
                f"acquisitions, and this figure does not."
            )
    if intang_pct:
        lines.append(
            "  A high share here means the company was largely assembled by "
            "acquisition, so the Magic Formula ROC is a screening artifact of "
            "excluding the purchase price — not evidence of operating efficiency."
        )

    # --- Growth and the PEG test ---------------------------------------------
    # The screen's third leg, and the answer to the objection the first two invite:
    # a high earnings yield on shrinking earnings is a value trap, not a bargain.
    # Included here (rather than only in the reader-facing section) so an agent
    # arguing "the market has not priced in growth" has to argue against the actual
    # measured rate, and so a bear case citing a growth figure can be checked.
    pe_ratio = _present(candidate.get("PE_Ratio"))
    growth_pct = _present(candidate.get("EPSGrowth_Pct"))
    peg_ratio = _present(candidate.get("PEG_Ratio"))
    growth_years = _growth_window(candidate)
    eps_now = _present(candidate.get("EPS_Current"))
    eps_base = _present(candidate.get("EPS_Base"))
    lines += [
        "",
        "Growth and valuation (Peter Lynch's PEG test — the screen's third ranking "
        "factor alongside the two returns above):",
        f"- Price-to-earnings ratio (market capitalisation / trailing twelve-month net "
        f"income): {pe_ratio if pe_ratio is not None else 'Not available'}",
        f"- Earnings-per-share growth rate (compound annual over {growth_years} fiscal "
        f"years): {growth_pct or 'Not available'}",
        f"- PEG ratio (the P/E divided by that growth rate in PERCENTAGE POINTS, so a "
        f"P/E of 20 on 20% growth is 1.0): "
        f"{peg_ratio if peg_ratio is not None else 'Not available'}",
    ]
    if candidate.get("EPSGrowth_Capped") and _present(candidate.get("EPSGrowth_ForPEG")) is not None:
        lines.append(
            f"- NOTE: the PEG above was computed against a growth rate capped at "
            f"{round(float(candidate['EPSGrowth_ForPEG']) * 100, 2)}% a year. Measured "
            f"persistence, not opinion: companies growing faster than 50% a year went "
            f"on to a MEDIAN of -20% over the next three years, and the 95th "
            f"percentile of realised 3-year growth is 63%. The two figures above "
            f"therefore do not divide into each other — this is deliberate, not an "
            f"error, and the cap only ever makes the PEG larger."
        )
    if eps_now is not None and eps_base is not None:
        # Name the actual measure. `fetch_eps_growth` prefers diluted but falls back to
        # basic when a filer reports only that, and labelling basic figures as diluted
        # would be exactly the kind of small false claim VERIFIED_FIGURES exists to
        # stop the agents from making.
        window = _present(candidate.get("EPS_Window"))
        span = _present(candidate.get("EPS_Window_Years")) or 3
        label = (f"TOTAL {_present(candidate.get('EPS_Basis')) or 'reported'} earnings "
                 f"per share over {span} fiscal years"
                 if window == "sums"
                 else f"{_present(candidate.get('EPS_Basis')) or 'reported'} earnings "
                      f"per share")
        lines.append(
            f"- The growth rate was computed from {label}: {eps_base} for the window "
            f"ending {_present(candidate.get('EPS_Base_Date')) or 'not stated'}, "
            f"against {eps_now} for the window ending "
            f"{_present(candidate.get('EPS_Current_Date')) or 'not stated'}."
        )
        if window == "sums":
            lines.append(
                "  Measured as multi-year TOTALS rather than two endpoint years, so a "
                "loss year inside the window is counted at its real negative value "
                "and a one-off spike is diluted rather than annualised. Do not "
                "recompute this rate from two single-year EPS figures and report a "
                "different answer."
            )
    base_margin = _present(candidate.get("BaseNetMargin_Pct"))
    if base_margin:
        lines.append(
            f"- Net profit margin over the BASE window: {base_margin}. This is the "
            f"check that the starting point was a real trading period rather than a "
            f"breakeven one — a near-zero base turns any recovery into a three-digit "
            f"growth rate that says nothing about the business."
        )
    if peg_ratio is None:
        # Say what is missing and why, rather than leaving an agent to fill the gap
        # with a PEG it recalled or invented — the failure mode this whole block exists
        # to prevent. The cause is a fact about the company far more often than it is
        # a data outage, and a shrinking-earnings cause is itself bear evidence.
        lines.append(
            f"- NOTE: a PEG ratio could NOT be computed for this company, because "
            f"{_no_peg_reason(candidate)}. Do not substitute a PEG from any other "
            f"source, and do not treat its absence as evidence either way about "
            f"growth — say plainly that the screen's growth test could not be applied "
            f"here, and why."
        )
    return "\n".join(lines)


# Heading of the reader-facing warning table. Defined once: the gate strips
# everything from this point on before scanning, so it never re-reads its own
# output when an already-stored report is checked again.
_RECONCILIATION_HEADING = "## Data Reconciliation Warnings"

# Figures an agent must not contradict, mapped to the phrases that introduce them
# in prose. Used by the reconciliation gate below.
_RECONCILED_FIELDS = {
    "TotalDebt": (r"total debt|debt (?:load|stack|burden|pile)|gross debt", "total debt"),
    "LiveMarketCap": (r"market cap(?:italisation|italization)?", "market capitalisation"),
    "EnterpriseValue": (r"enterprise value", "enterprise value"),
}

# Written amounts: $36.9 billion / $36.9B / $36,908 million / $36908M.
_MONEY_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|bn|b|million|mm|m|trillion|t)\b",
    re.IGNORECASE,
)
_MULTIPLIER = {
    "t": 1e12, "trillion": 1e12,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "m": 1e6, "mm": 1e6, "million": 1e6,
}

# How far an agent-written figure may drift from the verified one before it is
# flagged. Loose enough to tolerate rounding and a slightly different period end,
# tight enough to catch a genuinely different number.
#
# Per-field, because these are not the same kind of number. Total debt is an
# accounting figure read off a filing and should barely move between the sources
# an agent might cite. Market capitalisation is a LIVE price that legitimately
# drifts between when a cited article was written and when the pipeline ran, and
# enterprise value inherits that drift. Holding a market price to a 10% accounting
# tolerance just manufactures warnings about ordinary price movement.
RECONCILE_TOLERANCE = 0.10
_FIELD_TOLERANCE = {
    "TotalDebt": 0.10,
    "EnterpriseValue": 0.20,
    "LiveMarketCap": 0.25,
}

# The gate adjudicates claims about the CURRENT level only. Two kinds of figure
# are legitimately different from today's balance sheet and must not be flagged,
# or the warnings that matter get lost in noise:
#
#   1. A change rather than a level -- "debt rose BY $11 billion". Detected by
#      delta language in the ~30 characters immediately before the number.
#   2. A figure pinned to another period -- "debt was $24.4B in 2023", "reaches
#      $32B by FY2027". Detected by a year or quarter marker close to the number.
#   3. A THRESHOLD rather than a level -- "Re-leveraging above $6.00B Total Debt".
#      The sale advisory is written almost entirely in this form: every sell
#      trigger names a level the company has NOT reached, sitting deliberately
#      clear of the current figure. Flagging those reports a correct advisory as
#      contradicting the filings, which is how this class was found.
#
# Known limitation: a figure written only inside a markdown table whose unit lives
# in the column header ("| Total Debt ($M) | 36,908 |") carries no unit next to the
# number and is not checked. Matching bare table numbers would flag every legitimate
# multi-year history table, so prose assertions are the deliberate scope here.
_HEDGE = r"(?:over|under|about|nearly|roughly|approximately|more\s+than|less\s+than)"
# Change language: the amount is a movement, not a level.
_DELTA_PREFIX_RE = re.compile(
    rf"\b(?:by|added|increase[ds]?|rose|grew|surged?|jumped|climbed|reduced?|repaid|"
    rf"paid\s+down|cut|declined?|fell|dropped)\s*{_HEDGE}?\s*$",
    re.IGNORECASE,
)
# Threshold language: the amount is a level the company has NOT reached. Scanned
# only in the text PRECEDING the number -- "above $6.00B", "rises above $9.50B".
# Scanning forward as well would let a trigger in the NEXT sentence suppress a
# wrong baseline in this one: "Current total debt of $8.92B. Sell if debt rises
# above $9.50B." must still flag the $8.92B.
_THRESHOLD_RE = re.compile(
    r"\b(?:above|below|under|over|beyond|exceed(?:s|ing|ed)?|surpass(?:es|ing|ed)?|"
    r"breach(?:es|ing|ed)?|rises?\s+(?:above|to)|climbs?\s+(?:above|to)|"
    r"falls?\s+(?:below|under)|drops?\s+below|at\s+least|greater\s+than|"
    r"more\s+than|less\s+than|no\s+more\s+than|re-?lever\w*|threshold)\b",
    re.IGNORECASE,
)
_THRESHOLD_PROXIMITY = 45  # chars either side scanned for threshold language
_OTHER_PERIOD_RE = re.compile(r"\b(?:FY\s?)?20\d{2}\b|\bQ[1-4]\b", re.IGNORECASE)
_DELTA_LOOKBEHIND = 30    # chars before the number scanned for delta language
_PERIOD_PROXIMITY = 40    # chars either side scanned for a year/quarter marker
_CONCEPT_LOOKBEHIND = 25  # chars before the concept word scanned for the amount

# Associating a label with the amount it describes.
#
# The naive rule -- "nearest dollar figure to the label" -- is wrong, and wrong in
# a way that fires constantly on correct reports. Real examples that defeated it:
#
#   "TTM EBIT of $88.35M on a verified Enterprise Value of $8.91B"
#   "Cash & Short-Term Investments of $453.58M vs. Total Debt of $7.61M"
#
# In both, an unrelated amount sits just BEFORE the label and the correct one just
# after, so "nearest" picks the wrong one and reports a 99% discrepancy against a
# perfectly accurate sentence. English almost always writes "<label> of $X", so:
#
#   1. An amount immediately BEFORE the label wins first -- no intervening words,
#      as in "$36.9 billion debt burden" or "($8.91B Enterprise Value)". Direct
#      adjacency is the strongest signal there is.
#   2. Otherwise take the first amount AFTER the label, but only when it is joined
#      to it by a short connector ("of", "was", ": ", a table pipe). A comma or a
#      long gap means a new clause has started and the amount belongs to something
#      else -- "...$7.61M total debt, giving it roughly $446 million in net cash"
#      must not report $446M as the debt.
#   3. Never associate across a line break, and cross at most one table-cell
#      boundary, so a label can reach its own cell but not a peer's column or a
#      figure on the row above.
# An amount belongs to a label only when the text BETWEEN them is pure connective
# tissue. Measuring that gap in characters proved far too blunt -- "$29.31 billion
# in total debt** against **$829 million in cash" has a short gap on both sides,
# and the character rule attributed the cash figure to the debt label. So the gap
# must match a whitelist instead: whitespace, markdown emphasis, table pipes, a
# parenthetical gloss, and a small set of linking verbs.
#
# Threshold words (above, below, exceeds, ...) are deliberately EXCLUDED. "total
# debt above $32B" is a sell-trigger condition, not a claim about today's level,
# and the sale advisory is written almost entirely in that form.
_NOISE = r"[\s\*_:|~\-–—]"
_PAREN = r"\([^)]{0,120}\)"
_FORWARD_CONNECTOR_RE = re.compile(
    rf"^(?:{_NOISE}|{_PAREN})*"
    rf"(?:(?:of|is|are|was|were|at|totals?|totall?ing|totall?ed|reached?|remains?|"
    rf"stands?|sits?|carries|carrying|holds?|comes?)\b(?:{_NOISE}|{_PAREN})*)*"
    rf"(?:(?:approximately|roughly|about|nearly|around|circa|some)\b(?:{_NOISE}|{_PAREN})*)?"
    rf"$",
    re.IGNORECASE,
)
# Preceding form: "$36.9 billion debt burden", "$29.31 billion in total debt".
_BACKWARD_CONNECTOR_RE = re.compile(rf"^(?:{_NOISE})*(?:(?:in|of)\b(?:{_NOISE})*)?$", re.IGNORECASE)

_FORWARD_SCAN = 140        # chars after the label searched for its amount
_BACKWARD_GAP_MAX = 12     # backstop on the preceding-amount gap
_CONNECTOR_GAP_MAX = 60    # backstop on the following-amount gap

# A line carrying three or more amounts is a comparison table -- either a
# multi-year history ("| Total Debt | $1.31B | $1.27B | $1.37B |") or a peer
# column set ("| Market Cap | $9.36B | $6.75B | $6.78B |"). The gate cannot tell
# which column is the current period or which company owns it, and a sweep of the
# stored corpus showed the column order is not even consistent between reports
# (some run oldest-first, some newest-first). Attributing any one cell to "the
# current figure" is therefore a guess, so these rows are skipped outright.
_COMPARISON_ROW_MIN_AMOUNTS = 3

# Peer discussion: "Akebia Therapeutics, Inc. (AKBA) ... ($327M market cap)" or
# "**CRVS** ($1.10B market cap) commands a far higher multiple". The amount belongs
# to the named peer, not the subject. Detected by a ticker earlier on the same line
# -- parenthesised or bold-emphasised -- that is not the subject's own symbol.
_PEER_TICKER_RE = re.compile(r"\(([A-Z]{2,5})\)|\*\*([A-Z]{2,5})\*\*")


def _line_bounds(text: str, pos: int) -> tuple:
    """Start and end offsets of the line containing `pos`."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return start, (len(text) if end == -1 else end)


def _amount_for_label(text: str, m: re.Match):
    """Locate the amount a label refers to. Returns (abs_start, abs_end, match) for
    the associated amount, or None when no amount can be attributed to this label.

    `m` is the label match. See the commentary above for the association rules."""
    # --- Backward: the amount butts directly against the label ---
    back_start = max(0, m.start() - _CONCEPT_LOOKBEHIND)
    back = text[back_start: m.start()]
    boundary = max(back.rfind("\n"), back.rfind("|"))
    if boundary != -1:
        back_start += boundary + 1
        back = text[back_start: m.start()]
    preceding = list(_MONEY_RE.finditer(back))
    if preceding:
        last = preceding[-1]
        gap = back[last.end():]
        if len(gap) <= _BACKWARD_GAP_MAX and _BACKWARD_CONNECTOR_RE.match(gap):
            return back_start + last.start(), back_start + last.end(), last

    # --- Forward: the dominant construction, "<label> of $X" ---
    region = text[m.end(): m.end() + _FORWARD_SCAN]
    line_end = region.find("\n")
    if line_end != -1:
        region = region[:line_end]
    # Allow reaching into the next table cell (label and value are usually adjacent
    # cells) but stop at the one after it, so peer columns are never picked up.
    cells = [c.start() for c in re.finditer(r"\|", region)]
    if len(cells) >= 2:
        region = region[:cells[1]]
    money = _MONEY_RE.search(region)
    if money:
        connector = region[:money.start()]
        if len(connector) <= _CONNECTOR_GAP_MAX and _FORWARD_CONNECTOR_RE.match(connector):
            return m.end() + money.start(), m.end() + money.end(), money
    return None


def _reconcile_agent_figures(text: str, candidate: dict, source: str) -> list:
    """Scan agent-written prose for dollar figures presented as total debt, market
    cap, or enterprise value, and flag any that deviate from the pipeline's own
    verified figures by more than RECONCILE_TOLERANCE.

    Deliberately narrow: it only checks the three figures the pipeline computes
    deterministically, and only when the sentence names one of them. It is a
    tripwire for the specific failure of a fabricated headline number propagating
    through the report chain, not a general-purpose fact checker -- so a clean
    result means 'no contradiction of our own basis', never 'everything is true'.
    """
    findings = []
    if not text:
        return findings
    verified = _verified_figures(candidate)
    if not verified:
        return findings

    # Never scan a previous run's warning table. In the live pipeline the gate sees
    # the analyst's raw output, which has no such section -- but re-checking an
    # already-stored report would otherwise read the table's own "stated vs
    # verified" columns as fresh claims and re-flag every one of them.
    text = text.split(_RECONCILIATION_HEADING)[0]
    subject = (candidate or {}).get("Symbol") or ""

    for field, (pattern, label) in _RECONCILED_FIELDS.items():
        truth = verified.get(field)
        if not truth:
            continue
        for m in re.finditer(pattern, text, re.IGNORECASE):
            line_start, line_end = _line_bounds(text, m.start())
            line = text[line_start:line_end]

            # Comparison table row -- cannot attribute a column. See above.
            if len(_MONEY_RE.findall(line)) >= _COMPARISON_ROW_MIN_AMOUNTS:
                continue

            # A peer named earlier on this line owns the figure, not the subject.
            peers = [a or b for a, b in _PEER_TICKER_RE.findall(text[line_start: m.start()])]
            if peers and peers[-1] != subject:
                continue

            located = _amount_for_label(text, m)
            if not located:
                continue
            money_start, money_end, money = located
            try:
                amount = float(money.group(1).replace(",", "")) * _MULTIPLIER[money.group(2).lower()]
            except (ValueError, KeyError):
                continue

            # Skip figures that are not claims about the current level (see above).
            if _DELTA_PREFIX_RE.search(text[max(0, money_start - _DELTA_LOOKBEHIND): money_start]):
                continue
            near = text[max(0, money_start - _PERIOD_PROXIMITY): money_end + _PERIOD_PROXIMITY]
            if _OTHER_PERIOD_RE.search(near):
                continue
            if _THRESHOLD_RE.search(text[max(0, money_start - _THRESHOLD_PROXIMITY): money_start]):
                continue

            deviation = abs(amount - truth) / truth
            if deviation > _FIELD_TOLERANCE.get(field, RECONCILE_TOLERANCE):
                findings.append({
                    "source": source,
                    "field": label,
                    "stated": amount,
                    "verified": truth,
                    "deviation_pct": round(deviation * 100, 1),
                    "context": " ".join(text[m.start(): money_end + 40].split()),
                })

    # One agent can repeat the same wrong number many times; report each distinct
    # (field, stated amount) pair once so the log stays readable.
    unique, seen = [], set()
    for f in findings:
        key = (f["field"], round(f["stated"]))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _format_reconciliation_section(findings: list) -> str:
    """Reader-facing note appended to the final report when the gate fired.

    Surfaced in the report rather than only logged: a reader has no other way to
    know that a figure in the bull or bear case above contradicts the filings."""
    if not findings:
        return ""
    lines = [
        "",
        _RECONCILIATION_HEADING,
        "",
        "Some figures written by the analysis agents above disagree with the figures "
        "this pipeline read directly from the company's filings. The verified figure "
        "is the one to trust. Treat any argument resting on a flagged number as "
        "unreliable.",
        "",
        "| Section | Figure | Stated in report | Verified from filings | Off by |",
        "| :--- | :--- | ---: | ---: | ---: |",
    ]
    for f in findings:
        lines.append(
            f"| {f['source']} | {f['field']} | {format_money(f['stated'])} | "
            f"{format_money(f['verified'])} | {f['deviation_pct']}% |"
        )
    return "\n".join(lines) + "\n"


def _present(value):
    """Return `value` if it is a real, displayable figure, else None.

    Candidate dicts reach this module two ways: straight from the screener (missing
    fields are None) and via pandas in --from-csv mode (missing fields are NaN, which
    is TRUTHY and formats as the string 'nan'). Everything rendered into a report has
    to survive both paths, so normalise here rather than at each call site."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "none"):
        return None
    return value


# Ratio fields the CSV stores raw, mapped to the percent-string key the report and
# VERIFIED_FIGURES blocks actually read, with the decimal places each is shown at.
_RATIO_TO_PCT_FIELD = {
    "ROIC_InclGoodwill": ("ROIC_InclGoodwill_Pct", 2),
    "IntangiblesShareOfAssets": ("IntangiblesShareOfAssets_Pct", 1),
    "ROA": ("ROA_Pct", 2),
    "ROA_NetIncome": ("ROA_NetIncome_Pct", 2),
    # Lynch's EPS growth rate. PEG itself is NOT here: it is a plain multiple, not a
    # percentage, and both paths already emit it as `PEG_Ratio`.
    "EPSGrowth": ("EPSGrowth_Pct", 2),
    "BaseNetMargin": ("BaseNetMargin_Pct", 2),
}


def _normalize_candidate(candidate: dict) -> dict:
    """Fill in the `*_Pct` display fields from their raw ratio columns.

    Candidates reach the report two ways and they do NOT agree on key names. The
    single-ticker path (`compute_ticker_magic_metrics`) emits pre-formatted percent
    strings like `ROIC_InclGoodwill_Pct`; the screener CSV stores the same figures as
    raw ratios under `ROIC_InclGoodwill`. Both report formatters only ever read the
    `_Pct` form.

    The consequence was silent and total: in the full-pipeline and `--from-csv` modes
    — every batch run — `ROIC_InclGoodwill_Pct` was simply absent, so the
    goodwill-inclusive companion was skipped in both the reader-facing '## Magic
    Formula Metrics' section AND the agent-facing VERIFIED_FIGURES block. The §2.F
    guard that exists specifically to stop a four-digit ROC being read as operating
    efficiency was therefore doing nothing on batch runs, which is where it matters.

    Normalising here rather than renaming the CSV columns keeps old archived CSVs
    readable — a `--from-csv` run against a previously archived file still works.
    """
    if not candidate:
        return candidate or {}
    out = dict(candidate)
    for raw_key, (pct_key, places) in _RATIO_TO_PCT_FIELD.items():
        if _present(out.get(pct_key)) is not None:
            continue  # already supplied in display form
        raw = _present(out.get(raw_key))
        if isinstance(raw, (int, float)):
            out[pct_key] = f"{round(float(raw) * 100, places)}%"
    return out


def _eps_window_phrase(candidate: dict) -> str:
    """Plain-English description of the two figures the growth rate came from.

    The wording has to follow the measurement. Under the "sums" method the figures
    are MULTI-YEAR TOTALS, and describing them as "earnings per share in the year
    ended X" would be a plain misstatement of what was divided — the exact class of
    small false claim the verified-figures block exists to prevent."""
    candidate = candidate or {}
    base = _present(candidate.get("EPS_Base"))
    cur = _present(candidate.get("EPS_Current"))
    base_date = _present(candidate.get("EPS_Base_Date")) or "not available"
    cur_date = _present(candidate.get("EPS_Current_Date")) or "not available"
    base_s = base if base is not None else "not available"
    cur_s = cur if cur is not None else "not available"
    if _present(candidate.get("EPS_Window")) == "sums":
        span = _present(candidate.get("EPS_Window_Years")) or 3
        return (f"total earnings per share of {base_s} across the {span} years up to "
                f"{base_date}, compared with {cur_s} across the {span} years up to "
                f"{cur_date}. Adding up whole periods rather than picking two single "
                f"years means a bad year in the middle is counted, not skipped over")
    return (f"earnings per share of {base_s} in the year ended {base_date}, compared "
            f"with {cur_s} in the year ended {cur_date}")


def _growth_window(candidate: dict):
    """The growth window's length, formatted for prose ('3', not '3.0').

    A whole number of fiscal years is the normal case and must read as one; a company
    that changed its fiscal year end genuinely has a fractional window and keeps it."""
    years = _present((candidate or {}).get("EPSGrowth_Years"))
    if not isinstance(years, (int, float)):
        return "3"
    return str(int(years)) if float(years).is_integer() else f"{float(years):.2f}"


def _no_peg_reason(candidate: dict) -> str:
    """Plain-English cause when no PEG could be computed, or "" if one was.

    Three distinct causes, and conflating them misinforms in opposite directions. The
    growth rate may be MISSING (its own set of reasons, carried on the candidate), or
    it may be perfectly well measured and NEGATIVE — a fact about the business, not a
    gap — or the company may have no P/E because it is loss-making on a trailing
    twelve-month basis even though its annual EPS grew."""
    candidate = candidate or {}
    if _present(candidate.get("PEG_Ratio")) is not None:
        return ""
    growth = _present(candidate.get("EPSGrowth"))
    if isinstance(growth, (int, float)) and growth <= 0:
        return ("its profit per share has been shrinking rather than growing, and this "
                "ratio only means anything for a company whose profits are rising — "
                "dividing by a negative growth rate would produce a negative figure "
                "that sorts as the cheapest thing on the list, which is the exact "
                "trap the ratio exists to expose")
    if isinstance(growth, (int, float)) and _present(candidate.get("PE_Ratio")) is None:
        return ("the company has no price-to-earnings ratio to divide: it is "
                "loss-making over the last twelve months, even though its annual "
                "earnings per share grew over the longer window")
    return {
        "non_positive_base_eps":
            "the company was losing money in the earlier year, so there is no positive "
            "starting profit to measure growth from. That is a fact about its history, "
            "not a gap in the data",
        "non_positive_current_eps":
            "the company lost money in its most recent full year, so there is no growth "
            "rate to work out",
        "insufficient_history":
            "the company has not published enough years of annual results to cover the "
            "whole growth window — usually a recent listing",
        "stale_eps_history":
            "its most recent annual report is too old for a growth rate drawn from it to "
            "describe the company as it is today",
        "no_eps_reported":
            "the data provider did not report an earnings-per-share figure for this company",
        "fetch_failed":
            "the annual accounts could not be retrieved from the data provider",
    }.get(
        _present(candidate.get("EPSGrowth_Unavailable_Reason")),
        "a growth rate could not be worked out from the available annual accounts",
    )


def _format_magic_formula_section(candidate: dict) -> str:
    """Deterministic '## Magic Formula Metrics' section, injected into every
    final report regardless of what the LLM chooses to restate. Built directly
    from the screener/on-demand candidate data (not the LLM), so Earnings
    Yield, ROC, their basis (TTM vs Annual fallback), and the underlying
    formula/components are always present and accurate. Every field is always
    present -- falling back to 'Not available' rather than being omitted -- and
    when the ratios could not be computed the section states the plain-English
    reason (e.g. the company is loss-making) instead of leaving the reader
    guessing whether it was a data glitch or a fact about the business."""
    candidate = candidate or {}
    rank = candidate.get("Final_Rank")
    rank_str = f"#{rank}" if rank not in (None, "") else "Not available (single-ticker run)"

    ebit_raw = candidate.get("EBIT")
    ebit = format_money(ebit_raw)
    capital_employed = format_money(candidate.get("CapitalEmployed"))
    enterprise_value = format_money(candidate.get("EnterpriseValue"))
    market_cap = format_money(candidate.get("LiveMarketCap"))
    basis = candidate.get("EBIT_Basis") or "Not available"

    ey_pct = candidate.get("EY_Pct")
    roc_pct = candidate.get("ROC_Pct")

    lines = [
        "## Magic Formula Metrics",
        "",
        "These are the numbers this screen ranks companies on. The first two are Joel "
        "Greenblatt's Magic Formula: one asks whether the business is cheap, the other "
        "whether it is any good. The third is Peter Lynch's PEG ratio, which asks "
        "whether it is growing — because the first two compare profit to price at a "
        "single moment, and cannot tell a steady business apart from one whose profits "
        "are on the way down. The workings are shown so you can check them yourself.",
        "",
        f"- **Earnings Yield (is it cheap?):** {ey_pct or 'Not available'}",
        f"  - How it is worked out: operating profit ({ebit}) divided by enterprise "
        f"value ({enterprise_value}).",
        "  - In plain English: what the company earns from its actual business, compared "
        "with what it would really cost to buy the whole company outright. A higher "
        "percentage means you pay less for each dollar of profit.",
        f"- **Return on Capital (is it a good business?):** {roc_pct or 'Not available'}",
        f"  - How it is worked out: operating profit ({ebit}) divided by capital employed "
        f"({capital_employed}).",
        "  - In plain English: how much profit the business squeezes out of the money "
        "tied up in running it. A higher percentage means the company turns its "
        "resources into profit more efficiently than rivals.",
        "  - Important limit: this figure deliberately leaves out what the company PAID "
        "to buy other businesses. That is the right choice for ranking companies against "
        "each other, but it can make a company built by takeovers look extraordinarily "
        "efficient. The companion figure below shows the return on the money actually "
        "spent.",
        "",
    ]
    # Companion ROIC. Shown whenever it could be computed, because the gap between
    # the two figures is the single most misread number in these reports: a very
    # large Magic Formula ROC next to a modest goodwill-inclusive ROIC means the
    # company bought its earnings rather than generating them from a small asset base.
    roic_pct = _present(candidate.get("ROIC_InclGoodwill_Pct"))
    intangibles_pct = _present(candidate.get("IntangiblesShareOfAssets_Pct"))
    invested_capital = format_money(candidate.get("InvestedCapital"))
    if roic_pct:
        lines += [
            f"- **Return on capital actually spent (including takeover costs):** {roic_pct}",
            f"  - How it is worked out: operating profit ({ebit}) divided by all the money "
            f"invested in the company ({invested_capital} — shareholders' money plus "
            f"borrowings, less spare cash).",
            "  - In plain English: what the business earns on every dollar ever put into "
            "it, including the price paid for acquisitions. This is the more conservative "
            "of the two, and for a company assembled by takeover it is the more honest "
            "guide to how good the business really is.",
        ]
        if intangibles_pct:
            lines.append(
                f"  - Goodwill and acquired intangibles are {intangibles_pct} of this "
                f"company's total assets. The higher that share, the wider the gap "
                f"between the two figures above, and the more weight the second deserves."
            )
        lines.append("")
    else:
        # The companion could not be computed. Say so, say WHY, and put a conservative
        # figure in its place — otherwise the reader is left with a headline return in
        # the hundreds or thousands of percent and nothing to weigh it against, on
        # exactly the companies where that headline is least trustworthy.
        reason = _present(candidate.get("ROIC_Unavailable_Reason"))
        roa_for_fallback = _present(candidate.get("ROA_Pct"))
        equity_raw = _present(candidate.get("TotalEquity"))
        total_equity_fmt = format_money(equity_raw)

        # The paragraph above ends by promising "the companion figure below". That
        # promise must be answered EVERY time, even when we cannot say why the figure
        # is missing — otherwise the section reads as though a number were simply
        # dropped, and the substitute below becomes an "instead" with no antecedent.
        lines.append(
            "- **Return on capital actually spent (including takeover costs):** "
            "cannot be calculated for this company."
        )
        # Infer the cause when the explicit reason is absent — it is missing for CSVs
        # written before that column existed, and negative equity is visible from the
        # figures we already carry. Better to explain from evidence than stay silent.
        if reason is None and isinstance(equity_raw, (int, float)) and equity_raw < 0:
            reason = "negative_invested_capital"

        if reason == "negative_invested_capital":
            lines += [
                f"  - Why: the company has returned more money to shareholders — through "
                f"share buybacks — than it has accumulated in retained profits, so its "
                f"balance-sheet equity is negative ({total_equity_fmt}). Once you add its "
                f"debts and subtract its cash, the total capital invested in it comes out "
                f"at or below zero, and you cannot work out a return on a negative amount.",
                "  - This is a fact about how the company is financed, not a gap in the "
                "data. It is common in mature businesses that buy back stock heavily.",
            ]
        else:
            lines.append(
                "  - Why: the money invested in this company (shareholders' funds plus "
                "borrowings, less spare cash) does not come out as a positive figure on "
                "the latest balance sheet, so there is nothing to divide the profit by. "
                "This is a feature of how the company is financed, not a missing number."
            )
        if roa_for_fallback:
            lines += [
                f"  - **Use this instead as the conservative check: return on assets of "
                f"{roa_for_fallback}** — operating profit measured against *everything* "
                f"the company owns, including spare cash and the goodwill from past "
                f"takeovers. It is a blunter measure than the one above and is not the "
                f"same thing, but it serves the same purpose: the headline return on "
                f"capital leaves those items out, and this figure does not. Where the two "
                f"are far apart, trust this one.",
            ]
            if intangibles_pct:
                lines.append(
                    f"  - Goodwill and acquired intangibles are {intangibles_pct} of this "
                    f"company's total assets."
                )
        lines.append("")
    # --- Lynch's growth test -------------------------------------------------
    # Rendered as a first-class ranking figure, next to ROC and earnings yield,
    # because it now IS one: survivors are ordered on 1/PEG alongside the other two.
    # Written out in full (rate, ratio, and the two EPS figures behind them) because
    # the PEG is the one ratio here whose UNITS are routinely got wrong — a reader who
    # cannot see that the denominator is "24.8", not "0.248", has no way to check it.
    growth_pct = _present(candidate.get("EPSGrowth_Pct"))
    peg_ratio = _present(candidate.get("PEG_Ratio"))
    growth_years = _growth_window(candidate)
    eps_now = _present(candidate.get("EPS_Current"))
    eps_base = _present(candidate.get("EPS_Base"))
    pe_for_peg = _present(candidate.get("PE_Ratio"))
    lines += [
        f"- **Profit growth (is it getting bigger?):** {growth_pct or 'Not available'} "
        f"a year",
        f"  - How it is worked out: {_eps_window_phrase(candidate)} — the "
        f"steady yearly rate that would take you from the first figure to the second "
        f"over {growth_years} years."
        # Only said when it applies. A standing "a minus sign means..." legend beside
        # a healthy positive rate is noise; beside a negative one it is the point.
        + (" The rate is negative, which means profit per share FELL over the period."
           if isinstance(_present(candidate.get("EPSGrowth")), (int, float))
           and candidate["EPSGrowth"] < 0 else ""),
        "  - In plain English: how fast the profit belonging to each share you own has "
        "actually grown. Per share matters: a company can grow its total profit while "
        "issuing so many new shares that your slice does not grow at all.",
        f"- **PEG ratio (is the growth already in the price?):** "
        f"{peg_ratio if peg_ratio is not None else 'Not available'}",
        f"  - How it is worked out: the price-to-earnings ratio "
        f"({pe_for_peg if pe_for_peg is not None else 'not available'}) divided by the "
        f"growth rate above written as a plain number — so 20% growth becomes 20, not "
        f"0.20.",
    ]
    # When the sustainability cap bit, the two figures above no longer divide into
    # each other and a reader checking the arithmetic would think one of them wrong.
    # Say what was actually divided by, and why, rather than quietly publishing a sum
    # that does not add up.
    if candidate.get("EPSGrowth_Capped") and _present(candidate.get("EPSGrowth_ForPEG")) is not None:
        capped_at = round(float(candidate["EPSGrowth_ForPEG"]) * 100, 2)
        lines.append(
            f"  - Note: the growth rate used for this calculation was held down to "
            f"{capped_at}% a year, rather than the higher figure above. Very fast "
            f"growth almost never continues: of the companies in our sample that had "
            f"been growing faster than 50% a year, nearly three quarters went on to "
            f"SHRINKING profits over the following three years, and only about one "
            f"company in twenty ever sustains more than 63%. Paying for growth above "
            f"that rate means paying for something that usually does not arrive. "
            f"Holding the figure down makes this ratio less flattering, never more."
        )
    lines += [
        "  - In plain English: Peter Lynch's test of whether you are paying a fair "
        "price for growth. At 1.0 the price you pay for each dollar of profit is "
        "exactly matched by how fast that profit is rising; below 1.0 you are getting "
        f"the growth cheaply; above it you are paying up front for growth that has not "
        f"happened yet. This screen only lets a company through at {MAX_PEG:g} or lower.",
        "  - Its limit: it takes the last few years' growth as a guide to the future, "
        "which is exactly what fails when a business turns. Treat it as one reading "
        "among several, not a forecast.",
    ]
    if peg_ratio is None:
        lines.append(f"  - Why this is missing: {_no_peg_reason(candidate)}.")
    lines.append("")

    # --- Greenblatt's step-by-step gate figures ---
    # These are NOT the ranking metrics, and the section says so, because "Return on
    # Assets" and "Return on Capital" are the single easiest pair to conflate in these
    # reports: same operating profit on top, very different denominators underneath.
    # `_present` matters here specifically: in --from-csv mode these arrive via
    # pandas, so an absent value is NaN, and NaN is truthy — a bare `if roa_pct`
    # would happily print "Return on assets: nan".
    roa_pct = _present(candidate.get("ROA_Pct"))
    roa_ni_pct = _present(candidate.get("ROA_NetIncome_Pct"))
    pe_ratio = _present(candidate.get("PE_Ratio"))
    if roa_pct or pe_ratio:
        lines += [
            "### The screening hurdles this company had to clear",
            "",
            "Greenblatt's step-by-step instructions for building the screen by hand use "
            "two rougher measures as entry conditions, because they are what an ordinary "
            "stock screener will give you. They decide who gets INTO the list; the two "
            "ratios above decide the order.",
            "",
        ]
        if roa_pct:
            lines.append(f"- **Return on assets:** {roa_pct} (operating profit divided by "
                         f"every asset the company owns).")
            lines.append(
                "  - Not the same thing as return on capital above. Both start from the "
                "same operating profit, but return on capital divides by only the money "
                "actually tied up in trading — equipment and working funds — while return "
                "on assets divides by EVERYTHING on the books, including spare cash and "
                "the goodwill left over from past takeovers. So return on assets is always "
                "the lower and blunter of the two, and it is the one Greenblatt's manual "
                "screen filters on purely because a free screener will show it to you. "
                "The hurdle is deliberately high — 25% — to make up for how rough it is."
            )
            if roa_ni_pct:
                lines.append(
                    f"  - On the stricter after-tax measure (bottom-line profit divided by "
                    f"total assets) the same company scores {roa_ni_pct}."
                )
        if pe_ratio:
            lines.append(
                f"- **Price-to-earnings ratio:** {pe_ratio} (the company's market value "
                f"divided by its bottom-line profit)."
            )
            lines.append(
                "  - The screen throws out anything below 5. That sounds backwards — a low "
                "price-to-earnings ratio is supposed to mean cheap — but a ratio that low "
                "almost always means a one-off event flattered the profit figure, such as "
                "selling a division or winning a lawsuit. That profit will not repeat, so "
                "the bargain is an illusion."
            )
        # The growth hurdle is listed with the others because it is applied the same
        # way — as an entry condition — even though the same figure ALSO feeds the
        # ranking above. Saying so here keeps a reader from thinking the PEG shown
        # earlier was decorative.
        if peg_ratio is not None or growth_pct:
            lines.append(
                "- **Growth and the PEG ratio:** this screen adds a hurdle of its own, "
                "which is not part of Greenblatt's original method. A company must be "
                f"growing its profit per share at all over the last few years, that "
                f"growth must have started from a genuinely profitable period rather "
                f"than a break-even one, and its PEG ratio must be {MAX_PEG:g} or "
                f"lower. The reason is that the two ratios above "
                "are snapshots — this year's profit set against today's price — so they "
                "cannot tell a company earning steadily apart from one whose profits are "
                "falling. Both can show the same attractive percentage. Worse, a "
                "shrinking company often keeps a tempting-looking figure, because once "
                "the market expects further decline it marks the share price down faster "
                "than the profits have actually fallen. What you are buying is a "
                "shrinking stream of profit at an apparent discount, and the discount "
                "disappears along with the profits. The same PEG figure is "
                "then used again in the ranking, so a cheap, well-run company that is "
                "also growing quickly ranks above one that is merely cheap and well-run."
            )
        lines.append("")

    lines += [
        "### The numbers behind the ratios",
        f"- Operating profit (EBIT — earnings before interest and taxes; profit from the "
        f"core business before loan interest and tax): {ebit}",
        f"- Enterprise value (the company's market price plus its debts, minus its spare "
        f"cash — the true cost of buying it outright): {enterprise_value}",
        f"- Capital employed (the money tied up running the business: equipment and "
        f"buildings, plus day-to-day working funds): {capital_employed}",
        f"- Market value of all shares (market capitalisation): {market_cap}",
        # Debt and cash are printed here as the reader-facing counterpart to the
        # VERIFIED_FIGURES block the agents were given, so a figure quoted in the
        # bull or bear case can be checked against the filings without leaving the page.
        f"- Total debt owed (read directly from the filings): "
        f"{format_money(candidate.get('TotalDebt'))}",
        f"- Cash and short-term investments: {format_money(candidate.get('Cash'))}",
        f"- Balance sheet date these figures come from: "
        f"{candidate.get('BalanceSheetDate') or 'Not available'}",
        f"- Reporting period used (basis): {basis} "
        f"(TTM = the last four quarters combined; Annual = the most recent full-year "
        f"report, used only when quarterly figures are unavailable)",
        f"- Magic Formula rank: {rank_str}",
    ]

    # When the ratios are missing, say why — this is usually a meaningful fact
    # about the company (loss-making, no capital employed), not a data outage.
    message = candidate.get("message")
    if not (ey_pct and roc_pct):
        lines += [
            "",
            "### Why these figures are unavailable",
            message or (
                "The Magic Formula ratios could not be calculated for this company from "
                "the available financial data."
            ),
            "",
            "This does not by itself make the company a bad investment — it means this "
            "particular value-and-quality screen cannot score it, so weigh the bull and "
            "bear cases below on their own merits.",
        ]

    return "\n".join(lines) + "\n"


def _run_phase_b(top_candidates: list, source_label: str, force: bool = False,
                 skip_sale_advisor: bool = False):
    """Create a pipeline run and analyze each candidate through Phase B.
    Shared by the full-pipeline and CSV modes."""
    run_id = str(uuid.uuid4())
    logger.info(f"Starting Workflow Run: {run_id} (candidates from {source_label})")

    # Parent pipeline_runs row BEFORE any agent_outputs/final_reports inserts —
    # those tables have a foreign key onto pipeline_runs(run_id).
    tickers = [c.get("Symbol") for c in top_candidates if c.get("Symbol")]
    _check_db(db_create_pipeline_run(run_id, tickers), "create pipeline run")

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
                                    force, skip_sale_advisor))
        completed += 1

    if halted:
        logger.warning(
            f"Stopped after {completed}/{len(top_candidates)} tickers. Everything "
            f"analyzed so far is saved under run {run_id}."
        )
    _finalize_run(run_id, run_usage, "Workflow Run",
                  status="BUDGET_EXCEEDED" if halted else "COMPLETED")


def run_orchestrator(force: bool = False, skip_sale_advisor: bool = False):
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
    _run_phase_b(top_candidates, "Phase A screener", force, skip_sale_advisor)


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


def run_from_csv(csv_path: str = None, force: bool = False, skip_sale_advisor: bool = False):
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
    _run_phase_b(top_candidates, f"CSV '{csv_path}'", force, skip_sale_advisor)


def run_single_ticker(ticker: str, company_name: str = None, force: bool = False,
                      skip_sale_advisor: bool = False):
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
                           skip_sale_advisor)

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
            "\n"
            "Phase A is subscription-metered (time only); Phase B/C is the billed part.\n"
            "--screen-only and --from-csv are inverses, so the two can run on different\n"
            "cadences: screen often, analyze only when you intend to act on the list.\n"
            "--skip-sale-advisor drops Phase C from any Phase B run (~1/4 of the cost).\n"
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
        "--sell-check", action="store_true",
        help="Test whether TICKER's sale conditions are now met, and advise Sell/Hold (no full analysis)",
    )
    parser.add_argument(
        "--run", metavar="RUN_ID", default=None,
        help="With --sell-check: evaluate against a SPECIFIC run's SALE_CASE (the run you purchased under) instead of the latest",
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

    if args.run and not args.sell_check:
        parser.error("--run is only valid together with --sell-check")

    # --screen-only never reaches Phase B, so any flag that only shapes Phase B is a
    # sign the command was not the one intended. Fail loudly rather than silently
    # ignoring it and leaving the operator to think Phase C ran (or didn't).
    if args.screen_only:
        conflicting = [
            name for name, given in (
                ("--from-csv", args.from_csv), ("--sell-check", args.sell_check),
                ("--skip-sale-advisor", args.skip_sale_advisor), ("--force", args.force),
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
    elif args.from_csv:
        run_from_csv(args.from_csv, args.force, args.skip_sale_advisor)
    elif args.ticker:
        run_single_ticker(args.ticker, args.company, args.force, args.skip_sale_advisor)
    else:
        run_orchestrator(args.force, args.skip_sale_advisor)
