"""The five agents, and the shared prompt fragments they are built from.

Four of them run in order over one shared session as PIPELINE_AGENTS; the fifth,
`sell_check_agent`, is standalone and on-demand. What each is told is in
prompts/*.md — this module is the wiring: which prompt, which tools, which session
key it writes, and how much of the transcript it is sent.

Nothing here runs an agent. `runtime.py` builds the Runner; `main` drives them.

**Editing anything in this module or in prompts/ changes `main._prompt_version()`**,
which is hashed into every stored report's reuse fingerprint. That is deliberate —
a tuned prompt must not keep serving reports written under the old one — but it
means a careless edit re-analyzes 30 tickers on the next --from-csv run.

Extracted verbatim from main.py, comments included. The measured figures in them
(token counts per agent, the caching experiment that was reverted) are the record
of why this wiring is the way it is.
"""
from google.adk.agents import LlmAgent

from mcp_server import fmp_company_profile, fmp_stock_news, web_search_tool
from settings import AGENT_MODEL


# Agent instructions live in prompts/, one .md per agent, and are composed by
# prompt_loader. They were Python string literals here until the analyst's ran to
# ~185 lines assembled from four sources 200 lines apart, at which point "what is
# the judge actually told?" had become a code-reading exercise rather than a
# prose-reading one.
#
# The loader validates every {placeholder} against the session-state keys the
# pipeline actually seeds, which replaces the comment that used to sit here
# claiming the files contain no '{'. That claim was both stricter than ADK
# requires and unenforceable; see prompt_loader's module docstring.
from prompt_loader import load_mandate, load_prompt

# --- 2. AGENT DEFINITIONS (Phase B) ---
# Two kinds of steps:
#   1. Pure data relays (SEC 10-K, FMP metrics) — deterministic tool calls invoked
#      DIRECTLY in analyze_ticker (100% fidelity, 0 tokens), seeded into session
#      state as sec_data / metrics_data (shared by both advocates below).
#   2. Reasoning — FOUR roles run in order over one shared session, handing off
#      via session state (PIPELINE_AGENTS; see _run_pipeline_async for why this
#      is a list rather than a SequentialAgent):
#         bear_agent  (prompts/bear.md) -> state['bear_data']
#         bull_agent  (prompts/bull.md) -> state['bull_data']
#                     (runs after bear so it can refute it)
#         analyst_agent (neutral judge) reads {bear_data}/{bull_data} -> final_report
# ticker, company_name, sec_data, metrics_data, screen_context are seeded before
# the graph runs; each agent templates the keys it references.
APP_NAME = "skeptical_decomposer"

# Shared prompt fragment: the same recency mandate is given to bear, bull, and
# analyst so no role can build a case on annual data alone. Added after a review
# found every agent arguing from FY2023-FY2025 trends while the two most recent
# quarters were contracting -- the annual feed simply could not show it.
RECENCY_MANDATE = load_mandate("recency")

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
REFERENCE_DATA_BLOCKS = load_mandate("reference-data-blocks")

# Given only to the two research advocates. The judge works from their arguments
# plus the verified figures, and the sale advisor from the finished report — neither
# needs the raw filing extract or the annual ratio dump, and sending it to them
# would add ~10k tokens per turn for nothing.
RESEARCH_SOURCE_BLOCKS = load_mandate("research-source-blocks")

VERIFIED_FIGURES_MANDATE = load_mandate("verified-figures")


# The machine-readable tail of the analyst's report, used only to populate the
# verdict ledger (see sql-schema.sql tables 6-9). It is PARSED AND STRIPPED by
# `_split_verdict_basis` before the report is stored, so no reader ever sees it:
# the report is written for someone with no finance background, and a JSON dump
# at the end of it would be noise to every human who opens it.
#
# Asked for as a fenced block rather than as prose because it is the one part of
# the output that a program consumes. Prose would need a parser that guesses,
# and a guessing parser writing to a table meant to be a factual record of what
# we claimed is the wrong trade.
#
# The factor list is a CONTROLLED VOCABULARY, and that is the whole point: free
# text keys would record "customer concentration", "client concentration risk"
# and "top-10 customer exposure" as three unrelated factors, and the base rates
# the ledger exists to accumulate would never form. The list is injected from
# specs/config.yaml at import so the vocabulary has exactly one home.
VERDICT_BASIS_MANDATE = load_mandate("verdict-basis")


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
    instruction=load_prompt("bear.md"),
    tools=[fmp_stock_news, web_search_tool, fmp_company_profile],
    output_key="bear_data",
    include_contents=PIPELINE_INCLUDE_CONTENTS,
)

# --- Advocate 2: BULL (runs after bear; §4 refutes the bear case) ---
bull_agent = LlmAgent(
    name="bull_agent",
    model=AGENT_MODEL,
    instruction=load_prompt("bull.md"),
    tools=[fmp_stock_news, web_search_tool, fmp_company_profile],
    output_key="bull_data",
    include_contents=PIPELINE_INCLUDE_CONTENTS,
)

# --- Judge: NEUTRAL analyst (no skeptical prompt) weighs bull vs bear ---
analyst_agent = LlmAgent(
    name="analyst_agent",
    model=AGENT_MODEL,
    instruction=load_prompt("analyst.md"),
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
    instruction=load_prompt("sale-advisor.md"),
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
    instruction=load_prompt("sell-check.md"),
    tools=[fmp_stock_news, web_search_tool],
    output_key="sell_check",
)
