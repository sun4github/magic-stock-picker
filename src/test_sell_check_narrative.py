"""Tests for the sell-check's live-narrative cross-check.

Runs offline — no model calls, no database, no billed work, no network:

    python test_sell_check_narrative.py

What is worth testing here, and why:

The sell-check evaluates a stored SALE_CASE (three measurable business events)
against current data, and its verdict is the ONLY automated authority for exiting
a held lot. Its failure modes split in two:

  - **Miss (the BKNG lesson, 2026).** The sale case names an underlying risk
    ("generative AI travel search disintermediating OTAs"). A narrative event
    (an AI travel-agent launch) hits the news between two earnings prints. The
    numeric triggers are all still clear — the metrics that would move do not
    report until the next print — so a trigger-only check says "0 breached, KEEP"
    and the narrative gap stays invisible until the fear has repriced the stock.
    The prompt must therefore require a live-narrative cross-check section and a
    separate HOLD (risk-live) recommendation grade.
  - **Whiplash (the inverse failure).** The Muse fear peaked in late September
    and was reported easing by mid-October. If a narrative could upgrade the
    recommendation to SELL on its own, the check would sell at fear peaks and
    bless holds at fear troughs — precisely backwards. The prompt must constrain
    the narrative layer to ADVISORY: it can add "(risk-live)" to a HOLD and
    nothing more. Only a MET condition produces SELL.

Everything under test is Python reading prompt Markdown or replaying the
recommendation decision table against sample model outputs. No model is asked
anything; the model-facing artifact is checked for structure the way
test_buy_case.py checks the buy-case prompts.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(HERE, "prompts", "sell-check.md")

# The exact recommendation grammar the prompt defines. Kept here (not imported
# from a module) so a prompt edit that silently changes the grammar fails here
# and gets read before it ships.
_RECS = {
    "SELL": re.compile(r"Recommendation: SELL\b"),
    "HOLD_RISK_LIVE": re.compile(r"Recommendation: HOLD \(risk-live\)"),
    "HOLD": re.compile(r"Recommendation: HOLD\b(?! ?\(risk-live\))"),
}


# --- prompt structure -----------------------------------------------------------
def _load_prompt():
    with open(PROMPT_PATH) as f:
        return f.read()


def test_prompt_exists():
    assert os.path.exists(PROMPT_PATH), f"missing prompt file: {PROMPT_PATH}"


def test_prompt_carries_no_braces_beyond_placeholders():
    """ADK treats {ident} in an instruction as a session-state key. The sell-check's
    allowed placeholders are exactly the ones execution.py seeds into state; any
    other brace is a runtime failure after the check has already been scoped."""
    prompt = _load_prompt()
    allowed = {"company_name", "ticker", "sale_conditions", "metrics_data", "quarterly_data"}
    found = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", prompt))
    extra = found - allowed
    assert not extra, f"unseeded placeholders in sell-check.md: {sorted(extra)}"


def test_prompt_requires_narrative_cross_check_section():
    """The BKNG gap: nothing in the old prompt asked about the live news cycle
    versus the sale case's own named underlying risk. The section must exist and
    be positioned between the condition evaluations and the recommendation."""
    prompt = _load_prompt()
    assert "## Live-Narrative Cross-Check" in prompt, (
        "sell-check.md must require a '## Live-Narrative Cross-Check' section"
    )
    i_cond = prompt.index("For EACH sale condition")
    i_narr = prompt.index("## Live-Narrative Cross-Check")
    i_rec = prompt.index("## Sell Recommendation")
    assert i_cond < i_narr < i_rec, (
        "narrative cross-check must sit between condition evaluations and the recommendation"
    )


def test_prompt_cross_check_targets_underlying_risks_with_recency():
    """The cross-check must work off the conditions' Underlying Risk text (not
    generic news reading), search a bounded recent window, and treat absence of
    evidence as QUIET — never as LIVE."""
    prompt = _load_prompt()
    assert "Underlying Risk" in prompt, "cross-check must anchor on named underlying risks"
    assert "30 days" in prompt, "cross-check must bound the recency window (30 days)"
    assert "empty results mean QUIET, never LIVE" in prompt, (
        "absence of evidence must resolve to QUIET, not LIVE (fail-closed against false alarms)"
    )
    assert "EASING, not LIVE" in prompt, "a faded fear must be called EASING, not LIVE"


def test_prompt_narrative_layer_is_advisory_only():
    """The whiplash guard: the narrative layer may annotate a HOLD and must never
    upgrade it to SELL. The decision table must make SELL conditional solely on a
    clearly MET condition, and HOLD (risk-live) must exist as its own grade."""
    prompt = _load_prompt()
    assert "Recommendation: HOLD (risk-live)" in prompt, (
        "the risk-live HOLD grade must exist verbatim"
    )
    assert "advisory" in prompt.lower(), "the narrative layer must be marked advisory"
    assert "regardless of narrative status" in prompt, (
        "SELL must be driven only by a MET condition, regardless of narrative status"
    )
    assert "can NEVER upgrade" in prompt, (
        "the advisory-only constraint must be stated in the imperative, not implied"
    )
    # Fear-trough guard: an easing narrative must not read as sale-supporting.
    assert "never appear as a reason to sell or trim" in prompt, (
        "EASING/QUIET narratives must be excluded from sale reasoning"
    )


def test_prompt_keeps_price_out_and_wnat_status_vocabulary():
    """The cross-check grades only the named risk's live status (LIVE / EASING /
    QUIET / NOT FOUND); status words must be the defined vocabulary so downstream
    parsing of the section stays possible."""
    prompt = _load_prompt()
    for status in ("LIVE", "EASING", "QUIET", "NOT FOUND"):
        assert status in prompt, f"status vocabulary missing: {status}"


# --- the recommendation decision table, replayed --------------------------------
def _parse_recommendation(report_text):
    """Return the single recommendation the report states, or None.

    Faithful to how the bi-weekly job reads these reports: it scans for the
    exactly-one recommendation line. SELL and plain HOLD cannot coexist without
    the grade at the EARLIEST position being the only grade in its line — two
    distinct grades anywhere in the report mean the model contradicted itself
    and the parser must return None, not silently pick the first match.
    """
    grades = {}
    for key, rx in _RECS.items():
        m = rx.search(report_text)
        if m:
            grades[key] = m
    if not grades:
        return None
    best_key = min(grades, key=lambda k: grades[k].start())
    best_pos = grades[best_key].start()
    # All grades that START on the same line as the best match
    line_text = report_text[report_text.rfind("\n", 0, best_pos) + 1:]
    line_end = report_text.find("\n", best_pos)
    if line_end == -1:
        line_end = len(report_text)
    on_line = {k for k, m in grades.items() if m.start() < line_end}
    if len(on_line) > 1:
        return None
    # A second distinct grade on a DIFFERENT line means a contradictory report
    others = [k for k in grades if k not in on_line]
    if others:
        return None
    return best_key


def _report(rec_line, conditions_met=0, narrative_line=None):
    parts = [
        "## Sell-Condition Check",
        f"{conditions_met} of 3 conditions met.",
    ]
    if narrative_line:
        parts.append(narrative_line)
    parts.append(rec_line)
    parts.append("Justification paragraph.")
    return "\n".join(parts)


def test_decision_table():
    """The four rows of the table the prompt pins: any-MET = SELL (narrative
    irrelevant); none-MET + live = HOLD (risk-live); none-MET + quiet = HOLD;
    none-MET + no evidence = HOLD."""
    cases = [
        # (rec line, expected grade, label)
        ("Recommendation: SELL", "SELL", "condition met; SELL regardless of narrative"),
        ("Recommendation: HOLD (risk-live)", "HOLD_RISK_LIVE", "none met; fear live"),
        ("Recommendation: HOLD", "HOLD", "none met; nothing live"),
        ("Recommendation: HOLD (risk-live) with trailing text", "HOLD_RISK_LIVE", None),  # grammar tolerates trailing prose
        ("Recommendation: HOLD", "HOLD", None),
    ]
    parsed = [
        _parse_recommendation("x " + r) for r, _, _ in
        [(c[0], None, None) for c in cases]
    ]
    assert parsed == ["SELL", "HOLD_RISK_LIVE", "HOLD", "HOLD_RISK_LIVE", "HOLD"], parsed


def test_selling_on_narrative_alone_is_not_representable():
    """There is NO recommendation grade for 'none met + live fear' other than
    HOLD (risk-live). If a future prompt edit introduces one (e.g. a bare
    'Recommendation: TRIM'), this test fails and forces a decision instead of a
    silent scope creep."""
    prompt = _load_prompt()
    banned = re.findall(r"Recommendation: (?!SELL\b|HOLD\b)[A-Za-z-]+", prompt)
    assert not banned, f"non-table recommendation grades in prompt: {banned}"


def test_conflicting_recommendations_surface_as_malformed():
    """A report that states both SELL and a plain HOLD is malformed; the parser
    must return None rather than silently pick the first."""
    text = "## Sell Recommendation\nRecommendation: SELL\nAlso Recommendation: HOLD"
    assert _parse_recommendation(text) is None


def test_risk_live_substring_does_not_shadow_plain_hold():
    """'HOLD (risk-live)' contains 'HOLD' — the plain-HOLD pattern must not match
    inside it, or every risk-live report would parse as two grades and go None."""
    assert _parse_recommendation("Recommendation: HOLD (risk-live)") == "HOLD_RISK_LIVE"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok    {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)