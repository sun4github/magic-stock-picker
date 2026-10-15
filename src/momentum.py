"""The entry gate: a mechanical reading of the price chart, kept apart from the verdict.

The verdict is the analysts' judgement of the BUSINESS. It has never looked at the
chart, and it should not: the screen deliberately finds companies that have been
falling (that is why they are cheap), so a verdict that weighed the trend would stop
finding value at all. But "a good business at a good price" and "a good moment to
buy it" are different questions, and until now the second was answered by hand,
outside the system, where nothing recorded it.

This module answers it deterministically — no model, no tokens — and the answer is
reported BESIDE the verdict, never in place of it:

  * `verdict_log.verdict` keeps grading the analyst. Overwriting it with a timing
    call would mix two different kinds of mistake in one column for good, and the
    audit that motivated this ("wrong about the business, or wrong about the
    entry?") could never be run.
  * `entry_gate` / `entry_call` grade the chart reading, against the same
    subsequent prices.

Three chart states, read in this order:

  FALLING_KNIFE — more than `knife_pct` below a 200-day average that is itself
                  falling. Cheap and still going down; nobody knows where it stops.
  EXTENDED      — more than `extended_pct` above the 50-day average, or in the top
                  `max_range_pct` of the 52-week range. A good business bought
                  after a run-up — the CROX/WEX pattern.
  CLEAR         — neither.
  UNKNOWN       — the chart could not be read well enough to rule either out.
                  Unavailable is not clear: a gap in the evidence is never
                  resolved in favour of buying.

No local imports, on purpose: the report layer, the pipeline, refine and the
offline backtest in tools/ all read it, and the buy-check sweep should be able to
without dragging in the agent stack.
"""

# The thresholds, used when config.yaml does not override them. They mirror the
# external gates this replaces, so behaviour carries over unchanged.
DEFAULTS = {
    "extended_pct": 20.0,     # % above the 50-day average
    "max_range_pct": 90.0,    # position in the 52-week range, 0 = low, 100 = high
    "knife_pct": 20.0,        # % below the 200-day average...
    "slope_sessions": 20,     # ...whose change over this many sessions is negative
}

# The history a full reading needs: a 200-session average, measured now and
# `slope_sessions` ago.
def sessions_needed(cfg: dict = None) -> int:
    return 200 + int(_cfg(cfg)["slope_sessions"])


ENTRY_CALLS = {
    # Only a BUY gets an entry call. A Watch already has its own entry conditions in
    # the buy case, and an Avoid should not be given any.
    "CLEAR": "BUY_NOW",
    "EXTENDED": "WAIT_PULLBACK",
    "FALLING_KNIFE": "WAIT_TREND",
    "UNKNOWN": "CHECK_CHART",
}


def _cfg(cfg: dict = None) -> dict:
    out = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if k in DEFAULTS and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    return out


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sma(values: list, n: int, end: int = None):
    """Mean of the `n` values ending at index `end` (exclusive), or None."""
    end = len(values) if end is None else end
    if end < n or n <= 0:
        return None
    window = values[end - n:end]
    return sum(window) / n


def _pct(a, b):
    return None if a is None or not b else round(100.0 * (a / b - 1.0), 2)


def momentum_state(price: dict, closes: list, cfg: dict = None) -> dict:
    """Read the chart. Never raises.

    `price` is the quote dict from `fmp_price_snapshot` (price, priceAvg50,
    priceAvg200, yearHigh, yearLow, as_of). The averages and the range come from
    there, so the figures in the gate are the same ones the report's price table
    prints. `closes` is daily closes, OLDEST FIRST; it is used only for the slope
    of the 200-day average, which a quote cannot give.
    """
    c = _cfg(cfg)
    price = price or {}
    closes = [x for x in (closes or []) if _num(x) and x > 0]
    p = _num(price.get("price"))
    d50, d200 = _num(price.get("priceAvg50")), _num(price.get("priceAvg200"))
    hi, lo = _num(price.get("yearHigh")), _num(price.get("yearLow"))

    k = int(c["slope_sessions"])
    now200, then200 = _sma(closes, 200), _sma(closes, 200, len(closes) - k)
    slope = _pct(now200, then200)

    out = {
        "as_of": price.get("as_of"),
        "price": p,
        "dma50": d50,
        "dma200": d200,
        "pct_vs_50dma": _pct(p, d50),
        "pct_vs_200dma": _pct(p, d200),
        "dma200_slope_pct": slope,
        "cross": (None if d50 is None or d200 is None
                  else "GOLDEN" if d50 > d200 else "DEATH"),
        "range_position": (round(100.0 * (p - lo) / (hi - lo), 1)
                           if p is not None and hi is not None and lo is not None and hi > lo
                           else None),
        "slope_sessions": k,
        "thresholds": c,
        "reasons": [],
        "clear_below": None,   # EXTENDED: the gate clears at or below this price
        "clear_above": None,   # FALLING_KNIFE: ...or at or above this one
    }

    if p is None or d50 is None or d200 is None:
        out["state"] = "UNKNOWN"
        out["reasons"].append("no usable quote (price or moving averages missing)")
        return out

    v50, v200, rng = out["pct_vs_50dma"], out["pct_vs_200dma"], out["range_position"]

    # The falling knife is read first: it is the more serious of the two, and a
    # sharp bounce inside a downtrend can trip the 'extended' rule as well.
    if v200 <= -c["knife_pct"]:
        out["clear_above"] = round(d200 * (1 - c["knife_pct"] / 100.0), 2)
        if slope is None:
            out["state"] = "UNKNOWN"
            out["reasons"].append(
                f"{abs(v200):.1f}% below the 200-day average, and too little price "
                f"history to tell whether that average is still falling")
            return out
        if slope < 0:
            out["state"] = "FALLING_KNIFE"
            out["reasons"].append(
                f"{abs(v200):.1f}% below the 200-day average (limit {c['knife_pct']:.0f}%), "
                f"and that average fell {abs(slope):.1f}% over the last {k} sessions")
            return out
        out["clear_above"] = None

    if v50 >= c["extended_pct"]:
        out["reasons"].append(
            f"{v50:.1f}% above the 50-day average (limit {c['extended_pct']:.0f}%)")
    if rng is not None and rng >= c["max_range_pct"]:
        out["reasons"].append(
            f"at {rng:.0f}% of its 52-week range (limit {c['max_range_pct']:.0f}%)")
    if out["reasons"]:
        out["state"] = "EXTENDED"
        # Both limits apply at once, so the price that clears the gate is the lower.
        limits = [d50 * (1 + c["extended_pct"] / 100.0)]
        if hi is not None and lo is not None and hi > lo:
            limits.append(lo + (hi - lo) * c["max_range_pct"] / 100.0)
        out["clear_below"] = round(min(limits), 2)
        return out

    out["state"] = "CLEAR"
    return out


def entry_call(verdict: str, state: str):
    """The entry recommendation for a verdict, or None when the verdict takes none."""
    if (verdict or "").strip().upper() != "BUY":
        return None
    return ENTRY_CALLS.get(state or "UNKNOWN", "CHECK_CHART")


def ledger_fields(verdict: str, m: dict) -> dict:
    """The keyword arguments `db_log_verdict` stores for the gate. {} without a reading."""
    if not m:
        return {}
    return {
        "entry_gate": m.get("state"),
        "entry_call": entry_call(verdict, m.get("state")),
        "pct_vs_50dma": m.get("pct_vs_50dma"),
        "pct_vs_200dma": m.get("pct_vs_200dma"),
        "dma200_slope_pct": m.get("dma200_slope_pct"),
        "range_position": m.get("range_position"),
    }


def summary_line(verdict: str, m: dict) -> str:
    """One log line: the chart state and, for a Buy, the entry call."""
    if not m:
        return "entry gate not read"
    call = entry_call(verdict, m.get("state"))
    why = "; ".join(m.get("reasons") or [])
    return (f"chart {m.get('state')}" + (f" -> entry {call}" if call else "")
            + (f" ({why})" if why else ""))
