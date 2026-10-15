"""The entry gate's reading, and the '## Entry Timing' block printed beside the verdict.

The verdict is the analysts' call on the BUSINESS; this is a mechanical call on the
MOMENT, from `momentum.py`. The section says both, side by side, and says plainly
that the second does not change the first. That separation is the whole design —
see `momentum.py` for why the verdict is never overwritten.

Like the price block, the reading is taken ONCE per ticker from the same quote the
price table prints, so the averages in the two sections cannot disagree.
"""
import logging

import momentum
import settings
from mcp_server import fmp_price_history

logger = logging.getLogger("InvestmentAgentPipeline")

ENTRY_HEADING = "## Entry Timing"

_STATE_LABEL = {
    "CLEAR": "Clear",
    "EXTENDED": "Extended",
    "FALLING_KNIFE": "Falling knife",
    "UNKNOWN": "Could not be read",
}
_CALL_LABEL = {
    "BUY_NOW": "Buy now",
    "WAIT_PULLBACK": "Wait for a pullback",
    "WAIT_TREND": "Wait for the trend to turn",
    "CHECK_CHART": "Check the chart first",
}


def _entry_reading(ticker: str, price: dict) -> dict:
    """The chart reading for one ticker, or {} when the gate is disabled. Never raises.

    `settings.MOMENTUM_ENABLED` is read live from its module, not copied at import,
    so a test or a caller that flips it is honoured.
    """
    if not settings.MOMENTUM_ENABLED:
        return {}
    cfg = settings.MOMENTUM
    try:
        closes = fmp_price_history(ticker, momentum.sessions_needed(cfg))
    except Exception as e:
        logger.warning(f"[{ticker}] Price history failed ({e}); the entry gate "
                       f"cannot read the 200-day trend.")
        closes = []
    return momentum.momentum_state(price, closes, cfg)


def _signed(v, suffix="%"):
    return "n/a" if v is None else f"{v:+.1f}{suffix}"


def _format_entry_section(verdict: str, m: dict) -> str:
    """The '## Entry Timing' block, or "" when there is no reading (gate disabled)."""
    if not m:
        return ""
    verdict = (verdict or "").strip().upper() or "UNPARSED"
    state = m.get("state") or "UNKNOWN"
    call = momentum.entry_call(verdict, state)
    t = m.get("thresholds") or momentum.DEFAULTS
    slope = m.get("dma200_slope_pct")
    trend = ("n/a" if slope is None
             else f"{'rising' if slope >= 0 else 'falling'} ({slope:+.1f}%)")
    cross = {"GOLDEN": "50 above 200", "DEATH": "50 below 200"}.get(m.get("cross"), "n/a")
    rng = m.get("range_position")
    why = "; ".join(m.get("reasons") or [])
    as_of = (m.get("as_of") or "n/a").replace("T", " ")

    if call == "BUY_NOW":
        verdict_note = ("Nothing in the chart argues for waiting: the price is not "
                        "stretched above its recent trend, and it is not sliding below "
                        "a falling long-term one.")
    elif call == "WAIT_PULLBACK":
        verdict_note = (f"The business case stands; this is about the price, not the "
                        f"company. The stock is {why}. The gate clears at or below "
                        f"**${m['clear_below']:,.2f}**.")
    elif call == "WAIT_TREND":
        verdict_note = (f"The stock is cheap and still falling: it is {why}. Nothing "
                        f"yet says where it stops. The gate clears when the price "
                        f"recovers to **${m['clear_above']:,.2f}** or the 200-day "
                        f"average stops falling.")
    elif call == "CHECK_CHART":
        verdict_note = (f"The chart could not be read ({why or 'no reason recorded'}). "
                        f"Look at the price history before acting on this Buy: a "
                        f"missing reading is not a clear one.")
    elif verdict == "WATCH":
        verdict_note = ("A Watch takes its entry conditions from the buy case, not from "
                        "this gate. The chart reading is recorded so a later review "
                        "can see where the price stood when the verdict was written.")
    else:
        verdict_note = ("This verdict takes no entry call. The chart reading is "
                        "recorded so a later review can see where the price stood "
                        "when the verdict was written.")

    return "\n".join([
        ENTRY_HEADING,
        "",
        "| Verdict | Entry | Chart | vs 50-day avg | vs 200-day avg | "
        f"200-day trend ({m.get('slope_sessions', t['slope_sessions'])} sessions) | "
        "50/200 cross | Place in 52-week range |",
        "| :--- | :--- | :--- | ---: | ---: | :--- | :--- | ---: |",
        f"| {verdict} | **{_CALL_LABEL.get(call, '—')}** | {_STATE_LABEL.get(state, state)} | "
        f"{_signed(m.get('pct_vs_50dma'))} | {_signed(m.get('pct_vs_200dma'))} | "
        f"{trend} | {cross} | {'n/a' if rng is None else f'{rng:.0f}%'} |",
        "",
        verdict_note,
        "",
        f"The verdict is the analysts' judgement of the business, and this section "
        f"does not change it. The entry call is a separate, mechanical reading of the "
        f"price chart as of {as_of}, by fixed rules: wait for a pullback when the "
        f"price is more than {t['extended_pct']:.0f}% above its 50-day average or in "
        f"the top {100 - t['max_range_pct']:.0f}% of its 52-week range; wait for the "
        f"trend to turn when it is more than {t['knife_pct']:.0f}% below a 200-day "
        f"average that is still falling. Both are recorded separately so a later "
        f"review can tell a wrong call on the company from a wrong call on the entry.",
        "",
    ])
