"""Tests for Phase E — the buy case, and the buy-condition check.

Runs offline — no model calls, no billed work, no network:

    python test_buy_case.py

What is worth testing here, and why:

- **The Watch gate.** A buy case on an 'Avoid' would hand a reader entry conditions
  for a company the analysis argued against. `is_watch` is the single expression of
  that rule and three call sites depend on it agreeing with itself.
- **The prompt files carry no braces.** ADK reads `{...}` in an instruction as a
  session-state key, so one stray brace in a hand-edited Markdown file fails the run
  at template time — after the pipeline has already paid for bear, bull and analyst.
  Cheap to check here, expensive to discover there.
- **Every templated key is actually seeded.** Same failure, subtler cause: an
  instruction that references `{price_data}` while the runner seeds `price` fails
  identically, and only for the one agent that has drifted.
- **The refinement decision table.** Four outcomes across two inputs (verdict, was
  there a revision) and one of them — 'the review moved the verdict onto Watch, so
  write a buy case that did not exist before' — has no counterpart in the sale
  advisory it was modelled on, so it cannot be assumed correct by analogy.
- **The reuse fingerprint.** `--skip-buy-case` must not produce a report that is
  later served in place of a full one, exactly as `--skip-sale-advisor` must not.
"""
import io
import os
import re
import json
import contextlib

import main
import refine
import buy_case_agent


# --- helpers -------------------------------------------------------------------
def _placeholders(instruction: str) -> set:
    """The `{key}` names ADK will try to resolve from session state.

    Matches only bare identifiers in braces, which is exactly what ADK's templater
    treats as a state key. Markdown or JSON braces in an instruction would fail this
    test by not matching the pattern and then failing at runtime — so the two prompt
    files are checked separately, below, for containing no braces at all.
    """
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", instruction))


@contextlib.contextmanager
def _patched(obj, **attrs):
    """Temporarily replace attributes on a module, restoring them afterwards."""
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


def _agent_output(found: bool, content: str = ""):
    return lambda *a, **k: json.dumps({"found": found, "raw_content": content})


# --- cases ---------------------------------------------------------------------
def _watch_gate_cases():
    """(verdict, expected) — the single rule that decides who gets a buy case."""
    return [
        ("WATCH", True), ("Watch", True), ("  watch  ", True), ("watch", True),
        ("BUY", False), ("Buy", False), ("AVOID", False), ("", False), (None, False),
        # Not a verdict at all. A parse that went wrong must fail CLOSED: writing no
        # buy case is a gap `buy_case.py` repairs, writing one for an Avoid is not.
        ("WATCHLIST", False), ("watch and see", False),
    ]


def _recommendation_cases():
    """(text, expected) for the BUY/WAIT extraction the CLI reports."""
    return [
        ("## Buy Recommendation\n\nRecommendation: BUY\n", "BUY"),
        ("Recommendation: WAIT", "WAIT"),
        ("**Recommendation:** buy", "BUY"),
        ("`Recommendation: WAIT`", "WAIT"),
        # The justification paragraph routinely contains the word 'buy' while
        # explaining why not to. A naive substring search reads that as the answer.
        ("The bull case argues you should buy now, but 0 of 4 triggers are met.\n"
         "Recommendation: WAIT", "WAIT"),
        ("No recommendation line at all.", "UNKNOWN"),
        ("", "UNKNOWN"),
    ]


def _trigger_count_cases():
    """(text, expected) — a log figure, so tolerant of both layouts seen in the wild."""
    return [
        ("### Trigger 1 — Price\n### Trigger 2 — Event\n", 2),
        ("- **Trigger 1 — Price.**\n- **Trigger 2 — Event.**\n- **Trigger 3 — Event.**", 3),
        # The closing 'how many must fire' sentence names them again; counting
        # distinct NUMBERS rather than occurrences is what keeps this honest.
        ("### Trigger 1 — Price\n### Trigger 2 — Event\n"
         "*Rule:* Trigger 1 must fire alongside Trigger 2.", 2),
        ("A buy case with no triggers at all.", 0),
    ]


def run():
    failures = []

    def check(name, got):
        nonlocal failures
        failures += [] if got else [name]
        print(f"{'PASS' if got else 'FAIL'}  {name:<62} {got}")

    print("--- the Watch gate (buy_case_agent.is_watch) ---")
    for verdict, expected in _watch_gate_cases():
        check(f"is_watch({verdict!r}) is {expected}",
              buy_case_agent.is_watch(verdict) is expected)

    print("\n--- buy-check recommendation extraction ---")
    for text, expected in _recommendation_cases():
        got = buy_case_agent.extract_buy_recommendation(text)
        check(f"{expected:<7} <- {text.splitlines()[0][:44] if text else '(empty)'!r}",
              got == expected)

    print("\n--- trigger counting (logging only, so tolerant) ---")
    for text, expected in _trigger_count_cases():
        check(f"{expected} trigger(s) counted in {text.splitlines()[0][:36]!r}",
              buy_case_agent.count_triggers(text) == expected)

    print("\n--- prompt files are safe to embed in an ADK instruction ---")
    for name in ("buy-case-instructions.md", "buy-check-instructions.md"):
        with open(os.path.join(os.path.dirname(__file__), name), encoding="utf-8") as fh:
            body = fh.read()
        check(f"{name} contains no '{{' or '}}'",
              "{" not in body and "}" not in body)

    print("\n--- every templated key is seeded by its runner ---")
    # The keys each `run_*` function actually puts into session state. Kept as a
    # literal list rather than introspected, so that deleting a key from the runner
    # fails this test instead of silently agreeing with itself.
    seeded = [
        (buy_case_agent.buy_case_agent, {
            "ticker", "company_name", "verified_figures", "quarterly_data",
            "price_data", "final_report"}),
        (buy_case_agent.buy_check_agent, {
            "ticker", "company_name", "buy_conditions", "metrics_data",
            "quarterly_data", "price_data"}),
    ]
    for agent, keys in seeded:
        used = _placeholders(agent.instruction)
        check(f"{agent.name}: no unseeded keys ({sorted(used - keys) or 'none'})",
              not (used - keys))
        # The reverse direction is a warning, not a failure — seeding a key an
        # instruction has stopped using costs a few tokens, nothing more — but it is
        # usually the first sign of a half-finished edit.
        unused = keys - used
        if unused:
            print(f"NOTE  {agent.name} seeds unused key(s): {sorted(unused)}")

    print("\n--- the reuse fingerprint separates the skip variants ---")
    base = {"BalanceSheetDate": "2026-03-31"}
    keys = {
        "full": main._analysis_key("FISV", dict(base)),
        "no-phase-c": main._analysis_key("FISV", dict(base), skip_sale_advisor=True),
        "no-buy-case": main._analysis_key("FISV", dict(base), skip_buy_case=True),
        "neither": main._analysis_key("FISV", dict(base), True, True),
    }
    check("all four skip variants have distinct keys", len(set(keys.values())) == 4)
    check("no variant is empty", all(keys.values()))
    # A buy-case prompt edit must invalidate reuse: a reused report carries the buy
    # case copied alongside it, so serving it would serve a document written under
    # the old instructions.
    original = main._DOWNSTREAM_PROMPTS
    with _patched(main, _DOWNSTREAM_PROMPTS=original + "\nAn extra instruction."):
        moved = main._analysis_key("FISV", dict(base)) != keys["full"]
    check("buy-case prompt edit -> different key", moved)
    check("key restored after the edit is reverted",
          main._analysis_key("FISV", dict(base)) == keys["full"])

    print("\n--- price block degrades honestly when the quote is unavailable ---")
    with _patched(buy_case_agent,
                  fmp_price_snapshot=lambda t: json.dumps({"error": "no quote"})):
        block = buy_case_agent.price_data_block("ZZZZ")
    check("says the price is unavailable", "PRICE DATA UNAVAILABLE" in block)
    check("tells the agent not to quote a price as current",
          "do not quote any price as current" in block)
    with _patched(buy_case_agent, fmp_price_snapshot=lambda t: json.dumps({
            "symbol": "ZZZZ", "name": "Test Co", "price": 44.03, "previousClose": 44.29,
            "changePercentage": -0.587, "dayLow": 42.1, "dayHigh": 44.17,
            "yearLow": 28.16, "yearHigh": 55.95, "pct_from_52w_high": -21.3,
            "pct_above_52w_low": 56.4, "priceAvg50": 39.07, "priceAvg200": 38.64,
            "marketCap": 5581251914, "as_of": "2026-07-31T16:00"})):
        block = buy_case_agent.price_data_block("ZZZZ")
    check("renders the last price", "$44.03" in block)
    check("renders the 52-week range", "$28.16 - $55.95" in block)
    check("carries the as-of stamp", "2026-07-31T16:00" in block)

    print("\n--- the shared price snapshot (report section + prompt block) ---")
    # One quote per ticker feeds three places: the '## Price' section at the top of
    # the report, the CURRENT SHARE PRICE line in VERIFIED_FIGURES, and the buy
    # case's PRICE_DATA. They must agree, which is only guaranteed if all three are
    # rendered from the SAME dict rather than from three separate fetches.
    snap = {"symbol": "ZZZZ", "name": "Test Co", "price": 187.56, "previousClose": 190.0,
            "changePercentage": -1.28, "dayLow": 185.0, "dayHigh": 191.2,
            "yearLow": 61.44, "yearHigh": 329.88, "pct_from_52w_high": -43.1,
            "pct_above_52w_low": 205.3, "priceAvg50": 239.45, "priceAvg200": 135.07,
            "marketCap": 164_260_000_000, "as_of": "2026-07-31T16:00"}
    section = main._format_price_section(snap, "ZZZZ")
    figures = main._format_verified_figures({"TotalDebt": 5.28e9}, snap)
    with _patched(buy_case_agent, fmp_price_snapshot=lambda t: (_ for _ in ()).throw(
            AssertionError("re-fetched a quote it was handed"))):
        block = buy_case_agent.price_data_block("ZZZZ", snap)
    check("the report section prints the share price", "$187.56" in section)
    check("it is a table row, not a sentence", section.count("|") >= 14)
    check("the as-of stamp is human-readable (no ISO 'T')",
          "2026-07-31 16:00" in section and "2026-07-31T16:00" not in section)
    check("it says the price was true when the report was written",
          "at the moment this report was written" in section)
    check("VERIFIED_FIGURES carries the same price", "$187.56" in figures)
    check("and labels it as live rather than filed",
          "LIVE market figure, not a filing figure" in figures)
    check("the prompt block reuses the handed-over snapshot", "$187.56" in block)
    check("no price section is faked when the quote is missing",
          "could not be retrieved" in main._format_price_section({}, "ZZZZ"))
    check("a missing quote does not lose the rest of VERIFIED_FIGURES",
          "Total debt" in main._format_verified_figures({"TotalDebt": 5.28e9}, {}))

    print("\n--- the refinement decision table (refine._refresh_buy_case) ---")
    ticker = "ZZTESTBC"
    out_path = os.path.join("reports", f"{ticker}_Buy_Case.md")
    prior_case = ("> **Run ID:** `old-run`  \n> **Ticker:** ZZTESTBC\n\n"
                  "## Buy Case\n\n### Trigger 1 — Price\n- **Condition:** below $10.\n")

    def _table_case(name, verdict, revised, had_prior, expect_origin, expect_generated):
        generated = {"called": False}

        def _fake_write(*a, **k):
            generated["called"] = True
            return "stored buy case"

        with _patched(refine,
                      db_get_agent_output=_agent_output(had_prior, prior_case),
                      db_store_agent_output=lambda *a, **k: "ok"), \
             _patched(buy_case_agent, write_buy_case=_fake_write):
            with contextlib.redirect_stdout(io.StringIO()):
                origin = refine._refresh_buy_case(
                    ticker, "Test Co", "old-run", "new-run", "report body",
                    "figures", "quarters", {}, verdict, revised, main._new_usage(),
                    0.0, 5.0, 0.0,
                )
        check(f"{name}: origin is {expect_origin!r}", origin == expect_origin)
        check(f"{name}: {'writes' if expect_generated else 'writes no'} new buy case",
              generated["called"] is expect_generated)

    _table_case("verdict moved off Watch, had one", "BUY", True, True, "none", False)
    _table_case("verdict moved off Watch, had none", "AVOID", True, False, "none", False)
    _table_case("Watch, revision ran, had one", "WATCH", True, True, "regenerated", True)
    _table_case("Watch, revision ran, had none", "WATCH", True, False, "created", True)
    _table_case("Watch, no revision, had one", "WATCH", False, True, "carried", False)
    _table_case("Watch, no revision, had none", "WATCH", False, False, "created", True)

    # The carried path is the only one that writes a file itself (the generated paths
    # delegate to write_buy_case, stubbed above), so this is where to check it lands.
    check("the carried path wrote reports/<TICKER>_Buy_Case.md", os.path.exists(out_path))
    if os.path.exists(out_path):
        body = open(out_path, encoding="utf-8").read()
        check("carried text is stamped with the refinement's run id",
              "new-run" in body.split("## Buy Case")[0])
        check("the carried note names the run it came from",
              "Carried over from run `old-run`" in body)
        # Notes must not stack across successive refinements of the same ticker.
        check("the previous run's banner was stripped, not stacked",
              body.count("**Run ID:**") == 1)
        os.remove(out_path)

    # When the money runs out the previous buy case is carried with a visible warning
    # rather than shipped as though it were current — and if there is nothing to
    # carry, the run honestly ends with no buy case.
    with _patched(refine, db_get_agent_output=_agent_output(True, prior_case),
                  db_store_agent_output=lambda *a, **k: "ok"):
        with contextlib.redirect_stdout(io.StringIO()):
            origin = refine._refresh_buy_case(
                ticker, "Test Co", "old-run", "new-run", "report body", "figures",
                "quarters", {}, "WATCH", True, main._new_usage(),
                99.0, 5.0, 0.0,          # spent 99 against a 5.00 ceiling
            )
    check("unaffordable + prior exists -> carried with a staleness label",
          origin == "carried_stale")
    if os.path.exists(out_path):
        check("the stale label is in the document itself",
              "may be out of date" in open(out_path, encoding="utf-8").read())
        os.remove(out_path)

    with _patched(refine, db_get_agent_output=_agent_output(False),
                  db_store_agent_output=lambda *a, **k: "ok"):
        with contextlib.redirect_stdout(io.StringIO()):
            origin = refine._refresh_buy_case(
                ticker, "Test Co", "old-run", "new-run", "report body", "figures",
                "quarters", {}, "WATCH", True, main._new_usage(), 99.0, 5.0, 0.0,
            )
    check("unaffordable + nothing to carry -> no buy case", origin == "none")

    # The reservation that is supposed to make the branch above unreachable.
    est = refine._Estimator()
    check("a revision reserves the buy case as well as the advisory",
          est.full_round == est.revision + est.critique + est.advisory + est.buy_case)
    check("the buy-case reservation is non-zero while the feature is on",
          (est.buy_case > 0) is refine.REGENERATE_BUY_CASE)

    print()
    print(f"{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    if failures:
        print("FAILED: " + "; ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
