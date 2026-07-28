"""Tests for the reconciliation gate (main._reconcile_agent_figures).

Runs offline against a fixed candidate dict — no API keys, no network — so it can
be run on any checkout:

    python test_reconciliation.py

The cases are drawn from FISV run f37d47ef, where a bear-case agent asserted
$36.9B of total debt for a company carrying $29.3B (it had duplicated the market
-cap figure), and that number then propagated into the bull refutation, the final
verdict, and a sell trigger calibrated against a level that never existed.

The gate's job is narrow: flag figures presented as the CURRENT total debt, market
cap, or enterprise value that contradict what the pipeline read from the filings.
Changes ("rose by $11B") and figures pinned to another period ("$24.4B in 2023")
are legitimately different from today's balance sheet and must stay silent, or the
warnings that matter get lost in noise.
"""
import main

# FISV as of the 2026-03-31 balance sheet — the run that motivated the gate.
CANDIDATE = {
    "Symbol": "FISV",
    "TotalDebt": 29_306_000_000.0,
    "Cash": 829_000_000.0,
    "LiveMarketCap": 27_894_516_740.0,
    "EnterpriseValue": 56_390_000_000.0,
    "TotalEquity": 26_221_000_000.0,
    "EBIT": 5_140_000_000.0,
    "CapitalEmployed": 3_760_000_000.0,
}

# (description, text, should_flag)
CASES = [
    # --- Must flag: wrong assertions about the current level ---
    ("wrong debt, number after concept",
     "The company reports total debt of $36.9 billion against thin cash reserves.", True),
    ("wrong debt, number before concept",
     "Without a major asset sale, Fiserv's $36.9 billion debt burden becomes an "
     "ongoing drag on earnings power.", True),
    ("wrong debt, bolded mid-sentence",
     "lowering annual interest expense on its massive **$36.9 billion debt stack**", True),
    # A comparison row (3+ amounts) is skipped: the gate cannot tell which column
    # is the current period, and a corpus sweep found the column order inconsistent
    # between reports. Precision is preferred over coverage here.
    ("comparison table row is not adjudicated",
     "| **Market Cap** | ~$36.9B | ~$22.3B | ~$17.7B |", False),
    ("wrong market cap in a two-cell row still flags",
     "| **Market Cap** | ~$36.9B |", True),
    ("wrong enterprise value",
     "At an enterprise value of $75 billion the shares look expensive.", True),

    # --- Must not flag: a change, not a level ---
    ("debt delta", "Total debt jumped by over $11 billion, a 42% rise.", False),
    ("debt paydown", "Management paid down nearly $4 billion of debt.", False),

    # --- Must not flag: pinned to another period ---
    ("historical level", "Total debt stood at $24.4 billion in 2023.", False),
    ("forward guidance", "Debt is guided to reach $32 billion by FY2027.", False),

    # --- Must not flag: correct current figures ---
    ("correct debt", "Total debt stands at $29.3 billion against $0.83 billion of cash.", False),
    ("correct market cap", "The company carries a market capitalisation of $27.9 billion.", False),
    ("correct debt with 'in <label>' phrasing, cash following",
     "Fiserv carries $29.31 billion in total debt against $829 million in cash.", False),
    ("wrong debt with 'in <label>' phrasing",
     "Fiserv carries $36.9 billion in total debt against $829 million in cash.", True),
    ("parenthetical gloss between label and amount",
     "an enterprise value (the total price tag including debt minus cash) of "
     "$56.39 billion", False),
    ("rounding drift stays inside tolerance",
     "Total debt is approximately $30 billion.", False),

    # --- Must not flag: unrelated amount near the concept word ---
    ("unrelated deal size",
     "A potential $15 billion sale of the STAR debit network would cut debt.", False),
]

# VICR run: three warnings were raised against a report whose figures were all
# correct. Each came from the gate attributing a NEARBY but unrelated amount to a
# label -- an EBIT figure read as enterprise value, a cash figure read as total
# debt, and a number from the previous table row read as this row's value. These
# are verbatim constructions from that run; all must stay silent.
VICR_CANDIDATE = {
    "Symbol": "VICR",
    "TotalDebt": 7_608_000.0,
    "Cash": 453_582_000.0,
    "LiveMarketCap": 9_358_665_907.0,
    "EnterpriseValue": 8_912_966_907.0,
    "EBIT": 88_354_000.0,
}

VICR_CASES = [
    ("EBIT before the EV label",
     "Current Earnings Yield: 0.99% (based on TTM EBIT of $88.35M on a verified "
     "Enterprise Value of $8.91B).", False),
    ("cash before the debt label",
     "**Balance Sheet Cash / Debt:** Cash & Short-Term Investments of **$453.58M** "
     "vs. Total Debt of **$7.61M** (Net Cash: ~$445.97M).", False),
    ("net cash phrasing",
     "a verified net cash position of ~$446M ($453.58M cash and short-term "
     "investments vs. $7.61M total debt; net debt/EBITDA of -3.38)", False),
    ("peer table must not reach the row above",
     "| **Market Capitalisation** | **$9.36B** (Verified) | $6.75B | $6.78B |\n"
     "| **Enterprise Value (EV)** | **$8.91B** (Verified) | ~$10.1B | ~$7.2B |", False),
    ("EV correct in prose with EBIT following",
     "Vicor trades at an Enterprise Value of **$8.91 billion** on TTM operating "
     "income (EBIT) of just **$88.35 million**.", False),

    ("amount before the label wins over a distant one after it",
     "Vicor's market valuation sits at $9.36B Market Capitalization ($8.91B "
     "Enterprise Value) against trailing twelve-month operating profit of $88.35M.",
     False),
    ("new clause after a comma is not the label's amount",
     "$453.58M of cash against $7.61M total debt, giving it roughly $446 million "
     "in net cash to fund research.", False),
    ("'against' introduces a different quantity",
     "The company carries $7.61M in total debt against $453.58M in cash.", False),
    ("threshold language is not a current level",
     "Sell if total debt rises above $900.00M in any future quarter.", False),
    ("a named peer's figure is not the subject's",
     "* **Akebia Therapeutics, Inc. (AKBA):** a peer commercial-stage "
     "biopharmaceutical company ($327M market cap) selling niche therapies.", False),
    ("the subject's own ticker in parentheses still flags",
     "Vicor Corporation (VICR) reports a market capitalisation of $2.00B.", True),

    # The gate must still bite on a genuinely wrong figure in the same shapes.
    ("wrong EV despite adjacent correct EBIT",
     "TTM EBIT of $88.35M on a stated Enterprise Value of $2.10B.", True),
    ("wrong debt beside correct cash",
     "Cash of $453.58M vs. Total Debt of $900.00M.", True),
]

# SOLV run 68ac498d: the sale advisory was flagged for "total debt $6.00B" against
# a verified $5.08B. The advisory was correct -- it stated $5.08B three times and
# $6.00B was a SELL TRIGGER deliberately set $920M above the current level, exactly
# as the prompt requires. Sell triggers name levels the company has NOT reached, so
# a gate that reads them as claims flags every well-written advisory.
SOLV_CANDIDATE = {
    "Symbol": "SOLV",
    "TotalDebt": 5_080_000_000.0,
    "Cash": 561_000_000.0,
    "LiveMarketCap": 12_000_000_000.0,
    "EnterpriseValue": 16_519_000_000.0,
}

SOLV_CASES = [
    ("trigger heading: amount before the label",
     "### Sell Trigger 3: Re-leveraging Above $6.00B Total Debt or Interest "
     "Coverage Ratio Falling Below 2.0x for Two Consecutive Quarters", False),
    ("trigger in prose",
     "**Actionable Sell Threshold:** Total Debt increasing back above **$6.00B** "
     "(a $920M increase from current levels).", False),
    ("trigger in a comparison cell",
     "| **3. Balance Sheet Stress** | **$5.08B** Total Debt | Total Debt **> $6.00B** "
     "OR Interest Coverage **< 2.0x** for 2 quarters |", False),
    ("correct baseline statement is validated, not skipped",
     "* **Total Debt:** **$5.08B** ($5,080,000,000 as of 2026-03-31).", False),
    ("correct baseline in prose",
     "A medtech company carrying **$5.08B in total debt** cannot sustain negative "
     "organic cash generation.", False),

    # A wrong BASELINE must still be caught -- that is the whole point of the gate.
    ("wrong baseline beside a valid trigger",
     "Current total debt of $8.92B. Sell if total debt rises above $9.50B.", True),
]

# The gate must not re-read its own warning table when an already-stored report is
# checked a second time -- the table's columns would look like fresh wrong claims.
STORED_REPORT_WITH_WARNINGS = """
Vicor holds $453.58M of cash against $7.61M of total debt.

## Data Reconciliation Warnings

| Section | Figure | Stated in report | Verified from filings | Off by |
| :--- | :--- | ---: | ---: | ---: |
| Bull case | enterprise value | $88.35M | $8.91B | 99.0% |
| Sale advisory | total debt | $453.58M | $7.61M | 5861.9% |
"""


def _fmt(found):
    return [f"${f['stated'] / 1e9:.3f}B" for f in found] or "silent"


def run():
    failures = []
    for group, candidate, cases in (
        ("FISV", CANDIDATE, CASES),
        ("VICR", VICR_CANDIDATE, VICR_CASES),
        ("SOLV", SOLV_CANDIDATE, SOLV_CASES),
    ):
        print(f"--- {group} ---")
        for name, text, should_flag in cases:
            found = main._reconcile_agent_figures(text, candidate, "test")
            ok = bool(found) == should_flag
            print(f"{'PASS' if ok else 'FAIL'}  {name:<40} "
                  f"expected={'flag' if should_flag else 'silent':<6} got={_fmt(found)}")
            if not ok:
                failures.append(f"{group}:{name}")
        print()

    # A candidate with no verified figures must never flag anything: an on-demand
    # run whose Magic Formula metrics could not be computed has no ground truth to
    # check against, and guessing would be worse than staying quiet.
    if main._reconcile_agent_figures("Total debt of $99 billion.", {}, "test"):
        failures.append("empty candidate should produce no findings")
        print("FAIL  empty candidate produced findings")
    else:
        print(f"PASS  {'no verified figures -> silent':<40} expected=silent got=silent")

    # Re-checking a stored report must not re-flag its own warning table.
    found = main._reconcile_agent_figures(STORED_REPORT_WITH_WARNINGS, VICR_CANDIDATE, "test")
    if found:
        failures.append("warning table re-scanned")
        print(f"FAIL  {'stored warning table not re-scanned':<40} "
              f"expected=silent got={_fmt(found)}")
    else:
        print(f"PASS  {'stored warning table not re-scanned':<40} expected=silent got=silent")

    total = len(CASES) + len(VICR_CASES) + len(SOLV_CASES) + 2
    print(f"{total - len(failures)}/{total} passed.")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
