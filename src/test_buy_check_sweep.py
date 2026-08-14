"""Tests for the weekly buy-check sweep's free pre-filter.

Runs offline — no model calls, no database, no network:

    python test_buy_check_sweep.py

Why these exist. The pre-filter decides who costs money and who does not, and
both of its failure directions are silent:

  * escalate too eagerly and the sweep becomes a weekly bill plus a weekly
    notification that always says the same thing, which is a notification the
    reader stops opening;
  * escalate too rarely and a trigger fires unseen, which is the entire failure
    the job was built to prevent.

The second is far worse than the first, so every ambiguous case here is asserted
to escalate — an unreadable trigger costs $0.08, a missed one costs the trade.
"""
import importlib.util
import os
from datetime import date

_spec = importlib.util.spec_from_file_location(
    "sweep", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "cron", "run_buy_check_sweep.py"))
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


CFG = {"near_trigger_pct": 5.0, "escalate_on_new_earnings": True,
       "max_agent_checks_per_sweep": 8}

# Both shapes below are taken from buy cases actually sitting in the database.
# The heading depth and the dash character differ between them, which is why the
# parser anchors on "Trigger 1" plus the word Price rather than on either.
FN_SHAPE = """### Trigger 1 - Price
- **Condition:** Stock price drops to $430.75 or lower.
- **Basis:** Applies a 25.0x multiple to the consensus FY2027 EPS estimate.
- **Currently:** $574.00 as of August 12, 2026 (requiring a 25.0% price decline).
"""

VNT_SHAPE = """*   **Trigger 1 - Price.** *(mandatory, and always first)*
    *   **Condition:** Share price drops below **$28.00 per share**.
    *   **Currently:** $33.87 as of August 4, 2026.
"""


def _parse_cases():
    """(name, buy_case_text, expected trigger price)"""
    return [
        ("heading shape with 'drops to'", FN_SHAPE, 430.75),
        ("bulleted shape with 'drops below'", VNT_SHAPE, 28.00),
        ("thousands separator",
         "### Trigger 1 - Price\n- **Condition:** falls to $1,234.50 or below.", 1234.50),
        ("spaced dollar sign",
         "### Trigger 1 - Price\n- **Condition:** below $ 42.10.", 42.10),
        # Each of these returns None, and None means "ask the model" — never
        # "skip this ticker". Asserted in the decision cases below.
        ("no trigger section at all", "Some prose about the company.", None),
        ("trigger present but no dollar figure",
         "### Trigger 1 - Price\n- **Condition:** below the 52-week low.", None),
        ("empty buy case", "", None),
    ]


def _decide_cases():
    """(name, price, trigger, prior_state, latest_earnings, should_escalate)"""
    written = date(2026, 7, 1)
    swept = date(2026, 8, 1)
    return [
        # --- price conditions -------------------------------------------------
        ("price below the trigger", 25.00, 28.00, None, None, True),
        ("price exactly at the trigger", 28.00, 28.00, None, None, True),
        ("price 3% above (inside the 5% band)", 28.84, 28.00, None, None, True),
        ("price 5% above (band is inclusive)", 29.40, 28.00, None, None, True),
        ("price 6% above (outside the band)", 29.68, 28.00, None, None, False),
        ("price far above", 566.49, 430.75, None, None, False),

        # --- the unreadable-trigger rule -------------------------------------
        # The expensive-but-safe direction. A buy case whose formatting drifted
        # must not silently drop off the watchlist.
        ("unparseable trigger escalates", 100.0, None, None, None, True),

        # --- earnings conditions ---------------------------------------------
        # Price is nowhere near the trigger in all of these; only the earnings
        # date decides. This is the case a price-only filter misses entirely.
        ("reported after the last sweep", 566.49, 430.75,
         {"last_swept_on": swept, "last_earnings_seen": date(2026, 5, 1)},
         date(2026, 8, 6), True),
        ("reported before the last sweep", 566.49, 430.75,
         {"last_swept_on": swept, "last_earnings_seen": date(2026, 8, 6)},
         date(2026, 8, 6), False),
        # Never swept before: baseline against the buy case's own date, so a
        # report filed after the case was written still counts as untested —
        # without this, a NULL last_earnings_seen would make every historical
        # report look new and escalate the entire watchlist on first run.
        ("never swept, reported after the case was written", 566.49, 430.75,
         None, date(2026, 7, 15), True),
        ("never swept, reported before the case was written", 566.49, 430.75,
         None, date(2026, 6, 15), False),
        ("no earnings data at all", 566.49, 430.75, None, None, False),
    ]


def run():
    failures = []

    print("-- parsing the price trigger out of a stored buy case --")
    for name, text, expected in _parse_cases():
        got = sweep.parse_trigger_price(text)
        ok = got == expected
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {got}")

    print()
    print("-- the escalate / skip decision --")
    for name, price, trigger, prior, earnings, expected in _decide_cases():
        case = {"ticker": "TEST", "written_on": date(2026, 7, 1)}
        escalate, reason, _ = sweep._decide(case, price, trigger, prior, CFG, earnings)
        ok = escalate == expected
        failures += [] if ok else [name]
        verb = "ESCALATE" if escalate else "skip"
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {verb}")

    print()
    print("-- the unreadable-trigger streak --")
    # The streak is what turns a permanent silent cost into one prompt to fix it.
    # A buy case whose price trigger reads "below 8.0x forward earnings" is
    # legitimate and unparseable, so it escalates to the model EVERY week and
    # never becomes free to check. The document cannot repair itself.
    #
    streak_cases = [
        ("first unreadable sweep", None, 0, 1),
        ("second in a row", None, 1, 2),
        ("third in a row reaches the default threshold", None, 2, 3),
        # The reset matters more than the increment: without it a name that was
        # briefly unreadable would carry the count for ever and eventually warn
        # about a buy case that has been fine for months.
        ("a parseable trigger resets it", 28.00, 5, 0),
        ("stays reset while readable", 28.00, 0, 0),
    ]
    for name, trigger, prior_streak, expected in streak_cases:
        got = sweep.next_unreadable_streak(trigger, {"unreadable_streak": prior_streak})
        ok = got == expected
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {prior_streak} -> {got}")

    print()
    print("-- ordering and configuration --")
    checks = []

    # An unreadable trigger must still escalate while the streak is counting.
    # If the counter ever became a reason to SKIP, the sweep would stop checking
    # exactly the names it is complaining about.
    esc, _, _ = sweep._decide({"ticker": "A", "written_on": date(2026, 7, 1)},
                              100.0, None, {"unreadable_streak": 9}, CFG, None)
    checks.append(("a long streak still escalates, never skips", esc))

    # Urgency orders escalations so the per-sweep ceiling drops the least
    # urgent rather than an arbitrary tail.
    _, _, deep = sweep._decide({"ticker": "A", "written_on": date(2026, 7, 1)},
                               20.0, 28.0, None, CFG, None)
    _, _, shallow = sweep._decide({"ticker": "B", "written_on": date(2026, 7, 1)},
                                  27.5, 28.0, None, CFG, None)
    checks.append(("further below the trigger sorts first", deep > shallow))

    # Turning the earnings rule off must leave a price-only filter behind.
    off = dict(CFG, escalate_on_new_earnings=False)
    esc, _, _ = sweep._decide({"ticker": "A", "written_on": date(2026, 7, 1)},
                              566.49, 430.75, None, off, date(2026, 8, 6))
    checks.append(("earnings rule can be disabled", not esc))

    # A wider band must escalate a ticker the default band skips, or the knob
    # in config.yaml does nothing.
    wide = dict(CFG, near_trigger_pct=20.0)
    esc, _, _ = sweep._decide({"ticker": "A", "written_on": date(2026, 7, 1)},
                              29.68, 28.00, None, wide, None)
    checks.append(("near_trigger_pct widens the band", esc))

    for name, ok in checks:
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {ok}")

    total = len(_parse_cases()) + len(_decide_cases()) + len(streak_cases) + len(checks)
    print()
    print(f"{total - len(failures)}/{total} passed.")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
