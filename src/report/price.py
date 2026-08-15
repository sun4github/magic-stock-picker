"""The live quote, and the '## Price' block that opens every stored report.

Fetching sits beside formatting here on purpose: the price is fetched ONCE per
ticker and passed to everything that quotes it, so the price on the report and the
price the buy triggers are measured against cannot be two numbers taken seconds
apart. Splitting the two apart would invite a second call.

Extracted verbatim from main.py, where this lived among the pipeline orchestration
it has nothing to do with. The bodies are unchanged — every one of these functions
is judged by the exact bytes it emits (stored in the database, injected into agent
prompts, hashed into the reuse fingerprint), so the move was verified by diffing
`tools/snapshot_report_surface.py` output before and after, not by inspection.

Names keep their leading underscore. They are private to the report layer by
intent, but `refine`, `buy_case`, `buy_case_agent`, `sale_advisory` and four test
modules call them as `main._name`, and renaming them here would have meant editing
~40 call sites in the same change that moved the code — losing the byte-identical
guarantee that makes the move checkable. `main` re-exports them under their old
names; the renaming, if it happens, is its own reviewable step.
"""
import json
import logging

from mcp_server import fmp_price_snapshot
from magic_formula_starter_screener import format_money

# The pipeline's logger singleton, configured by main. Fetched by name rather than
# imported from main, which would be a cycle: main imports this package.
logger = logging.getLogger("InvestmentAgentPipeline")


# --- The share price ------------------------------------------------------------
# Every report used to talk about valuation in market capitalisation and enterprise
# value and never once said what a SHARE costs. Those are the right units for the
# Magic Formula's arithmetic and the wrong ones for a reader: "$164.26B market cap,
# 65x trailing earnings" does not answer "what would I pay for this, and is that a
# lot?". The price was in the screener's CSV all along (`Live_Price`) and simply
# never reached the report.
#
# It is fetched here rather than read from the candidate, because the two candidate
# shapes disagree: the screener CSV carries `Live_Price`, and
# `compute_ticker_magic_metrics` carries no price at all. Sourcing it from the
# candidate would therefore print "Not available" on exactly the on-demand
# single-ticker runs a reader looks at most closely — the §2.I trap again. One quote
# call is deterministic, costs no tokens, and is subscription-metered.
#
# Fetched ONCE per ticker and passed to everything that quotes it — the report
# section below, the VERIFIED_FIGURES block the agents read, and the buy case — so
# the price on the report and the price the buy triggers are measured against cannot
# be two different numbers taken seconds apart.
def _price_snapshot(ticker: str) -> dict:
    """Current quote for a ticker, or {} when unavailable. Never raises."""
    try:
        data = json.loads(fmp_price_snapshot(ticker))
    except Exception as e:
        logger.warning(f"[{ticker}] Price snapshot failed ({e}); reports for this "
                       f"ticker will say the price was unavailable.")
        return {}
    if data.get("error") or data.get("price") is None:
        logger.warning(f"[{ticker}] No current price available "
                       f"({data.get('error') or 'quote carried no price'}).")
        return {}
    return data


def _format_price_section(price: dict, ticker: str) -> str:
    """The '## Price' block that opens every stored report.

    One row, read left to right: what a share costs, when that was true, and the
    three comparisons that tell you whether it is a lot — where it sits in its own
    year, how far it is off the high, and where the trend has been. Market cap is
    repeated here on purpose: it is the bridge between this line and the ratios
    below, which are all computed from it.

    **The as-of stamp is not decoration.** A stored report is a point-in-time
    document and this is the one figure in it that was already out of date by the
    time anyone read it. Stamped and captioned, it becomes strictly more useful than
    no price at all — reading an old `Watch` and knowing what the stock cost when
    that verdict was written is most of the context for deciding whether it still
    applies.
    """
    if not price:
        return (
            "## Price\n\n"
            f"A current share price could not be retrieved for {ticker} when this "
            f"report was written. The valuation figures below are computed from the "
            f"filings and the market capitalisation, and are unaffected — but this "
            f"report cannot tell you what one share costs today.\n"
        )

    def _n(key, fmt="{:,.2f}", dollar=True):
        v = price.get(key)
        if not isinstance(v, (int, float)):
            return "n/a"
        return ("$" if dollar else "") + fmt.format(v)

    off_high = price.get("pct_from_52w_high")
    off_high_str = f"{off_high:+.1f}%" if isinstance(off_high, (int, float)) else "n/a"
    # ISO with a 'T' in it is a machine's timestamp. The prompt block keeps it; the
    # reader-facing table does not.
    as_of = (price.get("as_of") or "n/a").replace("T", " ")
    return "\n".join([
        "## Price",
        "",
        "| Share price | As of | 52-week range | Off its 52-week high | 50-day avg | "
        "200-day avg | Market value |",
        "| ---: | :--- | :--- | ---: | ---: | ---: | ---: |",
        f"| **{_n('price')}** | {as_of} | "
        f"{_n('yearLow')} – {_n('yearHigh')} | {off_high_str} | "
        f"{_n('priceAvg50')} | {_n('priceAvg200')} | "
        f"{format_money(price.get('marketCap'))} |",
        "",
        "That price is what one share cost **at the moment this report was written**, "
        "and it moves every trading session. It is shown so the rest of the report has "
        "a stated starting point: every valuation figure below — the earnings yield, "
        "the price-to-earnings ratio, the PEG — is derived from the market value in "
        "the last column, which is this price multiplied by the number of shares. If "
        "you are reading this days later, check the current price before treating any "
        "of them as today's.",
        "",
    ])
