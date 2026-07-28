"""Tests for the two spend guards: the budget ceiling and the duplicate-run skip.

Runs offline — no model calls, no billed work:

    python test_guards.py

Why these exist. A 30-ticker screen costs ~$11 at gemini-3.6-flash rates, and the
cost accounting was wrong by 3.5x for a while without anyone noticing, so an
unbounded run is a real risk. The reuse check is the other half: 16 runs in one
day, several of them the same ticker against the same filing, each paying again to
regenerate a near-identical report.
"""
import main


def _usage_costing(usd):
    """A usage dict whose LLM cost is approximately `usd`.

    Built from output tokens only, priced at the confirmed $7.50/1M output rate, so
    the arithmetic is easy to follow and does not depend on the cached-input split.
    """
    u = main._new_usage()
    u["by_model"]["gemini-3.6-flash"] = {
        "input": 0, "cached_input": 0,
        "output": int(usd / 7.50 * 1e6),
        "total": 0, "requests": 1,
    }
    return u


def _budget_cases():
    """(name, run_cost_usd, prior_day_usd, should_halt)"""
    per_run = main.BUDGET_PER_RUN_USD
    per_day = main.BUDGET_PER_DAY_USD
    return [
        ("under both ceilings", 1.0, 0.0, False),
        ("run cost hits the per-run ceiling", per_run, 0.0, True),
        ("run cost just under the per-run ceiling", per_run * 0.9, 0.0, False),
        ("window total crosses the per-day ceiling", 1.0, per_day - 0.5, True),
        ("prior spend high, combined still under", 0.5, per_day - 5.0, False),
    ]


def run():
    failures = []

    print(f"--- budget guard (per_run=${main.BUDGET_PER_RUN_USD:.2f}, "
          f"per_day=${main.BUDGET_PER_DAY_USD:.2f}) ---")
    for name, run_usd, prior_usd, should_halt in _budget_cases():
        try:
            main._check_budget(_usage_costing(run_usd), prior_usd, "NEXT")
            halted = False
        except main.BudgetExceeded:
            halted = True
        ok = halted == should_halt
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} "
              f"{'halted' if halted else 'allowed'}")

    # Mode switches must be honoured: a guard that ignores its own config is worse
    # than no guard, because the run looks protected when it is not.
    for label, attr, value, should_halt in (
        ("on_exceed=warn never halts", "BUDGET_ON_EXCEED", "warn", False),
        ("disabled guard never halts", "BUDGET_ENABLED", False, False),
    ):
        saved = getattr(main, attr)
        setattr(main, attr, value)
        try:
            main._check_budget(_usage_costing(9999.0), 9999.0, "NEXT")
            halted = False
        except main.BudgetExceeded:
            halted = True
        finally:
            setattr(main, attr, saved)
        ok = halted == should_halt
        failures += [] if ok else [label]
        print(f"{'PASS' if ok else 'FAIL'}  {label:<48} "
              f"{'halted' if halted else 'allowed'}")

    print()
    print("--- reuse fingerprint ---")
    base = {"BalanceSheetDate": "2026-03-31"}
    k_same = main._analysis_key("FISV", dict(base))
    checks = [
        ("same ticker + same filing -> same key",
         main._analysis_key("FISV", dict(base)) == k_same),
        ("new filing date -> different key",
         main._analysis_key("FISV", {"BalanceSheetDate": "2026-06-30"}) != k_same),
        ("different ticker -> different key",
         main._analysis_key("VICR", dict(base)) != k_same),
        # No balance-sheet date means we cannot establish what the report was based
        # on. Returning "" disables reuse for that ticker: pay again rather than
        # serve a report of unknown provenance.
        ("unknown balance-sheet date -> reuse disabled",
         main._analysis_key("FISV", {}) == ""),
    ]
    for name, got in checks:
        failures += [] if got else [name]
        print(f"{'PASS' if got else 'FAIL'}  {name:<48} {got}")

    # A prompt edit must invalidate reuse, or tuning the verdict stance would keep
    # serving reports written under the old rules.
    original = main.analyst_agent.instruction
    try:
        main.analyst_agent.instruction = original + "\nAn extra instruction."
        changed = main._analysis_key("FISV", dict(base)) != k_same
    finally:
        main.analyst_agent.instruction = original
    failures += [] if changed else ["prompt edit -> different key"]
    print(f"{'PASS' if changed else 'FAIL'}  {'prompt edit -> different key':<48} {changed}")

    restored = main._analysis_key("FISV", dict(base)) == k_same
    failures += [] if restored else ["key stable after restoring the prompt"]
    print(f"{'PASS' if restored else 'FAIL'}  "
          f"{'key stable after restoring the prompt':<48} {restored}")

    total = len(_budget_cases()) + 2 + len(checks) + 2
    print()
    print(f"{total - len(failures)}/{total} passed.")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
