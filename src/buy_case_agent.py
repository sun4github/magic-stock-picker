"""The buy-case advisor, the buy-condition checker, and the plumbing between them.

A verdict of **Watch** is the pipeline's most common answer and its least actionable
one: the company cleared a value-and-quality screen, the analyst weighed both cases,
and the conclusion was "not at this price, not on this evidence, not today". The
reader is left holding a name and no statement of what they are waiting for.

This module writes that statement — the **buy case**: a derived price range and three
to five observable events which, when they occur, turn that Watch into a Buy. It is
the mirror of Phase C's sale advisory, which assumes the stock is owned and names the
events that would break the thesis; this one assumes it is NOT owned and names the
events that would make it worth owning. The two are deliberately symmetric down to
the shape of their output, because they are consumed the same way: `--sell-check`
tests a stored `SALE_CASE` against current data, and `--buy-check` tests a stored
`BUY_CASE` the same way.

## Why 'Watch' only

A `Buy` needs no buy case: the entry condition is "now". An `Avoid` needs no buy case
either — writing entry conditions for a company the analysis argued against would
manufacture a path back into a name the pipeline just rejected, and a reader scanning
for the encouraging document would find one attached to every ticker. `Watch` is the
only verdict whose whole content is a deferral, so it is the only one with a question
left open. `is_watch` is the single place that rule is expressed.

## Why it is not the fifth agent in the reasoning graph

The sale advisor runs inside `_run_pipeline_async` because it applies to every
verdict. This one is conditional on a verdict that only exists once `_extract_verdict`
has read the analyst's finished prose — a decision made in Python, after the graph
has finished. Adding a conditional edge to the graph to carry it would mean the graph
knowing about verdict parsing; running it here, as a post-step, keeps that knowledge
in one place. It costs one extra ADK session for the tickers that need it and nothing
at all for the ones that do not.

## Why the price arrives as data rather than as a tool call

`fmp_price_snapshot` is in the agent's toolbelt, but the current price is ALSO fetched
deterministically and seeded into the prompt as PRICE_DATA. Every price threshold in
the document is measured against it, and `--buy-check` compares against it again
later, so it is the one input the report cannot be allowed to be vague or stale about.
A tool call the model might skip, or make once and then paraphrase from memory three
sections later, is not a good enough foundation for the number the whole document
turns on. Same reasoning as `verified_figures` in Phase B.
"""
import os
import re
import json

from google.adk.agents import LlmAgent

import main
from mcp_server import (
    db_store_agent_output,
    fmp_price_snapshot,
    fmp_forward_estimates,
    fmp_earnings_calendar,
    fmp_revenue_segments,
    fmp_pending_ma_filings,
    fmp_company_profile,
    fmp_stock_news,
    web_search_tool,
)
from critic_agent import run_agent

logger = main.logger

# The agent output type stored in `agent_outputs`, and the section heading the buy
# case is written under. Both are referenced from several modules; naming them once
# means a change to either cannot half-land.
BUY_CASE_TYPE = "BUY_CASE"
BUY_CASE_HEADING = "## Buy Case"

_here = os.path.dirname(__file__)
# Both files use '[' / ']' placeholders and contain no '{' or '}', so they are safe to
# embed in a state-templated ADK instruction. Adding a brace to either would be read
# by ADK as a session-state key and would fail the run at template time — there is a
# test covering this in test_buy_case.py.
with open(os.path.join(_here, "buy-case-instructions.md"), "r", encoding="utf-8") as f:
    buy_case_instructions = f.read()
with open(os.path.join(_here, "buy-check-instructions.md"), "r", encoding="utf-8") as f:
    buy_check_instructions = f.read()


def is_watch(verdict: str) -> bool:
    """Does this verdict get a buy case? The single expression of the Watch-only rule.

    Tolerant of the shapes a verdict arrives in — `_extract_verdict` returns 'WATCH',
    the database stores whatever was passed to `db_store_ticker_run`, and a report
    banner may carry 'Watch'. Anything that is not recognisably a watch is treated as
    not one, because the failure direction matters: writing no buy case for a Watch
    is a gap the standalone command repairs, while writing one for an Avoid puts
    entry conditions on a company the analysis argued against.
    """
    return (verdict or "").strip().upper() == "WATCH"


# --- The buy-case advisor -------------------------------------------------------
# Tools, and why each is here:
#   fmp_forward_estimates   consensus revenue/EPS by fiscal year, the implied forward
#                           P/E and — the part that stops it being a number without a
#                           provenance — the analyst counts and estimate spread behind
#                           it. This is the only feed in the system that looks forward.
#   fmp_price_snapshot      the level a price range is measured from (also seeded).
#   fmp_earnings_calendar   next report date for ANY ticker, which is what makes
#                           "watch what META guides to in October" a checkable event
#                           rather than a sentiment.
#   fmp_revenue_segments    what the company actually sells, so an ecosystem claim is
#                           tested against the revenue line before it is asserted.
#   fmp_pending_ma_filings  a company under an agreed bid trades on the offer, which
#                           makes a valuation-derived price trigger meaningless.
#   fmp_company_profile     verify what another company does before calling it a
#                           customer or a supplier (the lesson of §2.C).
#   fmp_stock_news          dated factual developments.
#   web_search_tool         reported deals, capital-spending plans, analyst commentary.
buy_case_agent = LlmAgent(
    name="buy_case_agent",
    model=main.AGENT_MODEL,
    instruction=(
        buy_case_instructions
        + "\n\n## How to run this analysis\n"
        "You are the BUY-CASE ADVISOR for {company_name} ({ticker}). The neutral "
        "analyst's FINAL_REPORT below reached a verdict of WATCH. ASSUME the investor "
        "does NOT own the stock and is asking what they are waiting for. Work through "
        "the seven required sections in order, using your tools: "
        "`fmp_forward_estimates` for consensus estimates, the implied forward "
        "price-to-earnings and the analyst price targets; `fmp_earnings_calendar` for "
        "this company's next report date AND for the next report date of every "
        "company you name in Section 4; `fmp_revenue_segments` before asserting what "
        "drives the revenue line; `fmp_pending_ma_filings` before writing any price "
        "trigger; `fmp_company_profile` to check what a company actually does before "
        "naming it as a customer, supplier or rival; and `fmp_stock_news` plus "
        "`web_search_tool` for deals, spending plans and analyst commentary. Do not "
        "invent facts; cite sources with dates.\n\n"
        "## Deals must be looked up, not remembered\n"
        "Section 3 asks for transactions and catalysts, and that is the section most "
        "easily filled in from recollection — which is how a half-remembered "
        "acquisition, or one that was announced and then abandoned, ends up in a "
        "document someone acts on months later. So:\n"
        "- Make at least ONE `web_search_tool` call about this company's recent deals, "
        "contracts and capital-spending announcements, and at least one "
        "`fmp_stock_news` call, before writing Section 3.\n"
        "- Every transaction, contract, investment or partnership you name must come "
        "from a tool result in this session, and must be cited with the publisher and "
        "the date from that result.\n"
        "- If you believe something happened but no tool result confirms it, either "
        "leave it out or write it as **UNVERIFIED (from background knowledge, not "
        "confirmed by a source in this session)**. Never label an unconfirmed item "
        "CONFIRMED — that word means a source in front of you says so.\n\n"
        + main.VERIFIED_FIGURES_MANDATE
        + "## The price you are writing against\n"
        "PRICE_DATA below was fetched for this document and is the ONLY price you may "
        "quote as current. Every threshold in Section 5 must be stated against it, "
        "with the gap in percentage terms, so a reader months later can tell how far "
        "the stock actually was from your range when you wrote it. A price mentioned "
        "in a news article or in an analyst note is that article's price, not this "
        "one — say which is which.\n\n"
        "## Forward figures are estimates, and must never be laundered into facts\n"
        "The consensus figures from `fmp_forward_estimates` are what analysts EXPECT, "
        "not what the company has DONE. Three rules follow, and they are the ones "
        "most likely to be broken:\n"
        "- Every forward multiple must be quoted with its basis in the same sentence: "
        "the price, the fiscal-year end, the consensus EPS, and how many analysts "
        "contributed. A forward P/E with no analyst count is a number wearing a "
        "disguise.\n"
        "- Cumulative growth across several fiscal years must never be reported as "
        "though it were one year's. State which it is, and annualise it.\n"
        "- A SPECULATIVE catalyst — a rumoured deal, a press report of talks, an "
        "analyst's model, a management target for a distant year — may never be the "
        "sole content of a buy trigger. It may appear in a trigger only in the form "
        "of its confirmation ('the acquisition closes'), never its rumour.\n\n"
        "## Test every trigger before you write it\n"
        "A trigger already satisfied by the figures in front of you is not a trigger, "
        "and one so far from them that it could never fire is a way of never buying. "
        "Check each threshold against PRICE_DATA, VERIFIED_FIGURES and QUARTERLY_DATA "
        "and print the current actual value beside it. QUARTERLY_DATA holds eight "
        "quarters with their period-end dates: compare a quarter against the SAME "
        "quarter a year earlier, never the one before it, and reject any trigger that "
        "would have fired inside that window while the business was performing "
        "normally.\n\n"
        "<PRICE_DATA>\n{price_data}\n</PRICE_DATA>\n\n"
        "<VERIFIED_FIGURES>\n{verified_figures}\n</VERIFIED_FIGURES>\n\n"
        "<QUARTERLY_DATA>\n{quarterly_data}\n</QUARTERLY_DATA>\n\n"
        "<FINAL_REPORT>\n{final_report}\n</FINAL_REPORT>\n"
    ),
    tools=[fmp_forward_estimates, fmp_price_snapshot, fmp_earnings_calendar,
           fmp_revenue_segments, fmp_pending_ma_filings, fmp_company_profile,
           fmp_stock_news, web_search_tool],
    output_key="buy_data",
    include_contents=main.PIPELINE_INCLUDE_CONTENTS,
)


# --- The buy-condition checker (the `--buy-check` counterpart to `--sell-check`) ---
buy_check_agent = LlmAgent(
    name="buy_check_agent",
    model=main.AGENT_MODEL,
    instruction=(
        buy_check_instructions
        + "\n\n## How to run this check\n"
        "You are the BUY-CONDITION EVALUATOR for {company_name} ({ticker}). The "
        "BUY_CONDITIONS below are a buy case written at an earlier date: a derived "
        "price range and a numbered set of observable events that would turn this "
        "Watch into a Buy. Decide, against CURRENT data, whether each one is now met. "
        "PRICE_DATA was fetched for this check and is the authority on today's price "
        "— not any price quoted inside BUY_CONDITIONS, which is the baseline it was "
        "written at. Use `fmp_forward_estimates` to see whether consensus estimates "
        "and price targets have moved since, `fmp_earnings_calendar` for whether this "
        "company (or one the buy case named as a leading indicator) has reported "
        "since, `fmp_pending_ma_filings` for a bid that would make a valuation-based "
        "price trigger meaningless, and `fmp_stock_news` plus `web_search_tool` for "
        "everything else. Keep each trigger's original number. Do not invent facts: "
        "where you cannot establish a condition, mark it UNCLEAR — never resolve a "
        "gap in the evidence in favour of buying.\n\n"
        "CURRENT_METRICS is ANNUAL. Buy conditions are frequently written in "
        "quarterly terms, which annual data cannot answer — use QUARTERLY_DATA, which "
        "holds the last 8 quarters with year-over-year comparisons, and quote the "
        "specific quarter and its period end as your evidence.\n\n"
        "<PRICE_DATA>\n{price_data}\n</PRICE_DATA>\n\n"
        "<BUY_CONDITIONS>\n{buy_conditions}\n</BUY_CONDITIONS>\n\n"
        "<QUARTERLY_DATA>\n{quarterly_data}\n</QUARTERLY_DATA>\n\n"
        "<CURRENT_METRICS>\n{metrics_data}\n</CURRENT_METRICS>\n"
    ),
    tools=[fmp_forward_estimates, fmp_price_snapshot, fmp_earnings_calendar,
           fmp_pending_ma_filings, fmp_stock_news, web_search_tool],
    output_key="buy_check",
    include_contents=main.PIPELINE_INCLUDE_CONTENTS,
)


# --- Deterministic inputs -------------------------------------------------------
def price_data_block(ticker: str, data: dict = None) -> str:
    """The current price, rendered for the prompt. Deterministic, free, and dated.

    `data` is an already-fetched snapshot (`main._price_snapshot`). The pipeline
    passes the one it fetched for the report, so the price the buy triggers are
    measured against is byte-identical to the price printed at the top of the report
    they accompany — two quotes taken a minute apart would be a small discrepancy in
    a document whose whole purpose is a price threshold. An empty dict means
    "fetched, and there was none"; omitting the argument entirely fetches one.

    Returned as text rather than the tool's raw JSON for the same reason
    `_format_verified_figures` exists: this block is quoted back by the model into
    prose a human reads, and a labelled line is copied accurately far more often than
    a field name from a JSON blob. The failure case is spelled out rather than
    silently omitted — a buy case with no price is still worth writing (its event
    triggers stand), but the reader has to know the price trigger could not be
    anchored.
    """
    if data is None:
        try:
            data = json.loads(fmp_price_snapshot(ticker))
        except Exception as e:
            logger.warning(f"[{ticker}] Price snapshot failed ({e}).")
            data = {"error": str(e)}
    if data.get("error") or data.get("price") is None:
        why = data.get("error") or "the quote came back without a price"
        return (f"PRICE DATA UNAVAILABLE for {ticker} ({why}). You "
                f"cannot write a price-based buy trigger anchored to a current price. "
                f"Say so explicitly in Section 5 as the reason, keep Trigger 1 as a "
                f"price trigger expressed against a stated valuation multiple instead, "
                f"and do not quote any price as current.")

    def _n(key, fmt="{:,.2f}"):
        v = data.get(key)
        return fmt.format(v) if isinstance(v, (int, float)) else "n/a"

    return "\n".join([
        f"CURRENT PRICE — {data.get('symbol', ticker)} ({data.get('name') or ''}), "
        f"as of {data.get('as_of')}. All figures USD.",
        "",
        f"- Last price: ${_n('price')}  (previous close ${_n('previousClose')}, "
        f"{_n('changePercentage', '{:+.2f}')}% on the session)",
        f"- Day range: ${_n('dayLow')} - ${_n('dayHigh')}",
        f"- 52-week range: ${_n('yearLow')} - ${_n('yearHigh')}",
        f"- Distance from the 52-week high: {_n('pct_from_52w_high', '{:+.1f}')}%",
        f"- Distance above the 52-week low: {_n('pct_above_52w_low', '{:+.1f}')}%",
        f"- 50-day average: ${_n('priceAvg50')}   200-day average: ${_n('priceAvg200')}",
        f"- Market capitalisation: ${_n('marketCap', '{:,.0f}')}",
        "",
        "This price is live and moves every session. State it, with its date, beside "
        "any threshold you derive from it.",
    ])


# --- The two agent runs ---------------------------------------------------------
def run_buy_case(ticker: str, company_name: str, report_body: str,
                 verified_figures: str, quarterly_data: str,
                 price_data: str = None) -> tuple:
    """Generate a buy case from a WATCH report body. Returns (text, usage).

    Shared by all three callers — the pipeline (`main.analyze_ticker`), the critic
    loop (`refine.py`), and the standalone command (`buy_case.py`) — for the same
    reason `run_sale_advisor` is shared: the three decide *whether* to write one for
    very different reasons, but *how* must stay identical. Two copies would drift the
    moment `buy_case_agent` gains a templated key, and the symptom would be one entry
    point quietly producing a worse document than the other.

    `report_body` must be the analyst's own prose (see `refine.strip_generated_sections`)
    — not the assembled report with the deterministic Magic Formula section and
    reconciliation warnings wrapped around it.

    `price_data` is fetched here when not supplied. Callers pass it when they have
    already fetched it (the standalone command logs the price before deciding to
    spend anything).
    """
    return run_agent(
        buy_case_agent,
        {
            "ticker": ticker,
            "company_name": company_name,
            "verified_figures": verified_figures,
            "quarterly_data": quarterly_data,
            "price_data": price_data if price_data is not None else price_data_block(ticker),
            "final_report": report_body,
        },
        f"Write the buy case for {company_name} ({ticker}).",
    )


def run_buy_check(ticker: str, company_name: str, buy_conditions: str,
                  metrics_data: str, quarterly_data: str,
                  price_data: str = None) -> tuple:
    """Evaluate stored buy conditions against current data. Returns (text, usage)."""
    return run_agent(
        buy_check_agent,
        {
            "ticker": ticker,
            "company_name": company_name,
            "buy_conditions": buy_conditions,
            "metrics_data": metrics_data,
            "quarterly_data": quarterly_data,
            "price_data": price_data if price_data is not None else price_data_block(ticker),
        },
        f"Check whether the buy conditions for {company_name} ({ticker}) are now met.",
    )


def write_buy_case(run_id: str, ticker: str, company_name: str, report_body: str,
                   verified_figures: str, quarterly_data: str, candidate: dict = None,
                   usage: dict = None, origin_note: str = "", metadata: dict = None,
                   price_data: str = None) -> str:
    """Generate a buy case, check it, store it on `run_id`, and write the .md file.

    The single path from "this report is a Watch" to a stored `BUY_CASE`, used by all
    three callers. They differ only in which run the artefact belongs to and in the
    provenance note it carries, and those are the two parameters; everything between
    — the cost accounting, the reconciliation gate, the run banner, the database row,
    the report file — is identical by construction rather than by three authors
    remembering to keep it so.

    Returns the stored text, or '' when nothing was written (the agent produced no
    output). `usage` is merged into when supplied, so the caller's run totals include
    this work; when it is None the caller is handling accounting itself.
    """
    logger.info(f"[{ticker}] Verdict is Watch — writing the buy case "
                f"(entry price range and buy triggers)...")
    try:
        text, buy_usage = run_buy_case(ticker, company_name, report_body,
                                       verified_figures, quarterly_data, price_data)
    except Exception as e:
        logger.error(f"[{ticker}] Buy-case advisor failed: {e}")
        return ""

    cost = main._log_usage(f"{ticker} buy case", buy_usage)
    if usage is not None:
        main._merge_usage(usage, buy_usage)
    if not text.strip():
        logger.warning(f"[{ticker}] Buy-case advisor produced no output; nothing "
                       f"stored. The attempt still cost ${cost:.4f}.")
        return ""

    # The same gate every other generated document passes. It matters more here than
    # elsewhere: a buy trigger is a threshold the reader is invited to act on months
    # later without re-deriving it, so one anchored to a figure the filings contradict
    # is a wrong number with a long half-life. Findings are appended to the document
    # as well as logged — the log line is seen once, by whoever ran the pipeline; the
    # document is read later, by whoever is deciding.
    findings = main._reconcile_agent_figures(text, candidate, "Buy case")
    for f in findings:
        logger.warning(
            main.reconciliation_warning(ticker, f)
        )
    if not findings:
        logger.info(f"[{ticker}] Reconciliation gate passed on the buy case.")

    stored = main._with_run_header(
        origin_note + text + main._format_reconciliation_section(findings), run_id, ticker
    )
    meta = {"ticker": ticker, "triggers": count_triggers(text)}
    meta.update(metadata or {})
    main._check_db(
        db_store_agent_output(run_id, ticker, BUY_CASE_TYPE, stored, json.dumps(meta)),
        f"{ticker} buy case",
    )
    out_path = os.path.join("reports", f"{ticker}_Buy_Case.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(stored)
    logger.info(
        f"[{ticker}] Buy case written ({meta['triggers']} trigger(s), ${cost:.4f}) and "
        f"saved to {out_path}. `--buy-check {ticker} --run {run_id}` will use it."
    )
    return stored


_RECOMMENDATION_RE = re.compile(r"recommendation[^A-Za-z]{0,12}(buy|wait)", re.IGNORECASE)


def extract_buy_recommendation(text: str) -> str:
    """Pull BUY/WAIT from a buy-check report's 'Recommendation:' line.

    Anchored on the explicit declaration rather than a substring search, exactly as
    `main._extract_recommendation` is for sell-checks: the justification paragraph
    routinely contains the word 'buy' while explaining why NOT to, and a naive search
    would read that as the recommendation.
    """
    m = _RECOMMENDATION_RE.search(text or "")
    return m.group(1).upper() if m else "UNKNOWN"


def count_triggers(text: str) -> int:
    """How many numbered triggers a buy case defines. Used only for logging.

    Deliberately a loose count of distinct 'Trigger N' labels rather than a parse: the
    document is prose for a human, the machine-readable part is re-read by the
    buy-check agent, and a strict parser here would be a second, silently divergent
    definition of the format. The first version anchored on the '**Trigger N' bolding
    the instructions ask for and counted zero on the first live run, which wrote them
    as '### Trigger 1 — Price' headings instead — a good demonstration of why this
    number is a log line and not a gate. Counting distinct NUMBERS rather than
    occurrences also makes the count immune to the closing 'Trigger 1 must fire
    alongside...' sentence naming them again.
    """
    return len(set(re.findall(r"Trigger\s+(\d+)", text or "", re.IGNORECASE)))
