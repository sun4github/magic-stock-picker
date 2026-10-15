"""Tests for the entry gate (momentum.py) and its report section. Runs offline:

    python test_momentum_gate.py

Why these exist. The gate is a SILENT classifier: a sign flip on the slope would
call every rising stock a falling knife, and a missing-data path that defaulted to
CLEAR would tell the reader to buy on no evidence at all. Nothing downstream can
tell a wrong reading from a right one, so the rules are pinned here, one boundary
at a time — and so is the separation that is the point of the design: the gate
never touches the verdict.
"""
import sys

import momentum
import settings
from report import entry as entry_mod
from report import _format_entry_section

FAILS = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def quote(price=100.0, d50=100.0, d200=100.0, lo=50.0, hi=150.0):
    return {"price": price, "priceAvg50": d50, "priceAvg200": d200,
            "yearLow": lo, "yearHigh": hi, "as_of": "2026-10-14T16:00"}


def closes(slope_pct):
    """220 closes whose 200-session average moves by roughly `slope_pct` over the
    last 20 sessions: a straight line, so the average moves with it."""
    n = 220
    # SMA(last 200) - SMA(200 ending 20 back) = 20 * step for a linear series.
    base = 100.0
    step = slope_pct / 100.0 * base / 20.0
    return [base + step * (i - (n - 1)) + 50 for i in range(n)]


print("--- the chart states ---")
m = momentum.momentum_state(quote(), closes(+2))
check("an ordinary chart is CLEAR", m["state"] == "CLEAR")
check("and a Buy on it is BUY_NOW", momentum.entry_call("BUY", m["state"]) == "BUY_NOW")

m = momentum.momentum_state(quote(price=121, d50=100), closes(+2))
check("21% above the 50-day is EXTENDED", m["state"] == "EXTENDED")
check("it says why", "above the 50-day average" in " ".join(m["reasons"]))
m = momentum.momentum_state(quote(price=119, d50=100), closes(+2))
check("19% above the 50-day is not", m["state"] == "CLEAR")

m = momentum.momentum_state(quote(price=141, d50=135, d200=120, lo=50, hi=150), closes(+2))
check("91% of the 52-week range is EXTENDED", m["state"] == "EXTENDED")
check("the clearing price is the LOWER of the two limits",
      m["clear_below"] == round(min(135 * 1.2, 50 + 100 * 0.9), 2))

m = momentum.momentum_state(quote(price=75, d50=80, d200=100), closes(-3))
check("25% below a FALLING 200-day is FALLING_KNIFE", m["state"] == "FALLING_KNIFE")
check("its slope is negative", m["dma200_slope_pct"] < 0)
check("it clears at 80% of the 200-day", m["clear_above"] == 80.0)
check("a Buy on it is WAIT_TREND", momentum.entry_call("BUY", m["state"]) == "WAIT_TREND")

m = momentum.momentum_state(quote(price=75, d50=80, d200=100), closes(+3))
check("the same distance below a RISING 200-day is not a knife", m["state"] == "CLEAR")
check("and carries no knife clearing price", m["clear_above"] is None)

m = momentum.momentum_state(quote(price=79, d50=65, d200=100), closes(-3))
check("a bounce inside a downtrend reads as the knife, not as extended",
      m["state"] == "FALLING_KNIFE")

print("\n--- missing data is never CLEAR ---")
m = momentum.momentum_state({}, closes(+2))
check("no quote is UNKNOWN", m["state"] == "UNKNOWN")
check("and a Buy on it is CHECK_CHART",
      momentum.entry_call("BUY", m["state"]) == "CHECK_CHART")
m = momentum.momentum_state(quote(price=75, d50=80, d200=100), [])
check("deep below the 200-day with no history to read its slope is UNKNOWN",
      m["state"] == "UNKNOWN")
m = momentum.momentum_state(quote(), [])
check("no history is harmless when the knife rule is not in play", m["state"] == "CLEAR")
check("the slope is reported as missing, not as zero", m["dma200_slope_pct"] is None)

print("\n--- the verdict is never changed ---")
for v in ("WATCH", "AVOID", "Watch", "", None):
    check(f"a {v!r} verdict gets no entry call",
          momentum.entry_call(v, "EXTENDED") is None)
fields = momentum.ledger_fields("WATCH", momentum.momentum_state(quote(), closes(1)))
check("the ledger fields carry no verdict key", "verdict" not in fields)
check("a Watch still records its chart state", fields["entry_gate"] == "CLEAR")
check("but no entry call", fields["entry_call"] is None)
check("no reading, no ledger fields", momentum.ledger_fields("BUY", {}) == {})

print("\n--- config ---")
m = momentum.momentum_state(quote(price=115, d50=100), closes(1), {"extended_pct": 10})
check("a configured threshold is honoured", m["state"] == "EXTENDED")
m = momentum.momentum_state(quote(price=115, d50=100), closes(1),
                            {"extended_pct": "ten", "enabled": True})
check("a malformed or foreign key falls back to the default", m["state"] == "CLEAR")
check("sessions needed covers the slope window",
      momentum.sessions_needed({"slope_sessions": 30}) == 230)

print("\n--- the report section ---")
m = momentum.momentum_state(quote(price=121, d50=100), closes(+2))
sec = _format_entry_section("BUY", m)
check("it has its heading", sec.startswith("## Entry Timing"))
check("it shows the verdict unchanged", "| BUY |" in sec)
check("beside the entry call", "Wait for a pullback" in sec)
check("with the clearing price", f"${m['clear_below']:,.2f}" in sec)
check("and says the verdict is not changed", "does not change it" in sec)
check("it ends on a blank line, like the price section", sec.endswith("\n"))
check("no reading, no section", _format_entry_section("BUY", {}) == "")
sec = _format_entry_section("WATCH", m)
check("a Watch shows no entry call", "| WATCH | **—** |" in sec)
check("and points at the buy case", "buy case" in sec)
sec = _format_entry_section("BUY", momentum.momentum_state({}, []))
check("an unreadable chart says so", "Check the chart first" in sec)
check("no literal braces that could reach an ADK template",
      "{" not in _format_entry_section("BUY", m))

print("\n--- the reading honours the enabled flag, read live ---")
saved = settings.MOMENTUM_ENABLED
try:
    settings.MOMENTUM_ENABLED = False
    check("disabled -> no reading, no history call",
          entry_mod._entry_reading("ZZZZ", quote()) == {})
    settings.MOMENTUM_ENABLED = True
    entry_mod.fmp_price_history, real = (lambda t, n: closes(+2)), entry_mod.fmp_price_history
    try:
        check("enabled -> a reading from the handed-over quote",
              entry_mod._entry_reading("ZZZZ", quote())["state"] == "CLEAR")
        entry_mod.fmp_price_history = lambda t, n: (_ for _ in ()).throw(RuntimeError("down"))
        check("a failed history call still yields a reading",
              entry_mod._entry_reading("ZZZZ", quote())["state"] == "CLEAR")
    finally:
        entry_mod.fmp_price_history = real
finally:
    settings.MOMENTUM_ENABLED = saved

print(f"\n{'FAILED: ' + str(len(FAILS)) if FAILS else 'All momentum-gate checks passed.'}")
sys.exit(1 if FAILS else 0)
