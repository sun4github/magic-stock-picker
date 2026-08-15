"""Dump every report-formatting function's output to a text file, for diffing.

The report code is judged by the exact bytes it emits: its output is stored in the
database, injected into agent prompts, and (via VERIFIED_FIGURES) hashed into the
reuse fingerprint. A refactor of it is correct only if every function returns the
same string it returned before, character for character — and "I moved it carefully"
is not evidence of that.

So: run this before a move and after it, and diff the two files. An empty diff is
the proof. A non-empty one names the function that drifted.

    python tools/snapshot_report_surface.py before.txt
    ... do the move ...
    python tools/snapshot_report_surface.py after.txt
    diff before.txt after.txt

Everything here is offline — fixed candidate dicts, no network, no API keys — except
that importing `main` configures logging and reads config.yaml. The fixtures are the
ones the test suite already exercises (screener-CSV shape, negative equity, the
legacy CSV missing ROIC_Unavailable_Reason, and the three reconciliation corpora),
plus the cases the tests do NOT cover, because a snapshot's value is entirely in the
paths no assertion is watching: every _no_peg_reason branch, the "sums" EPS window,
the capped-growth note, and the empty/None candidate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import main  # noqa: E402

# --- Fixtures ---------------------------------------------------------------
# Screener-CSV shape: raw ratio columns, no *_Pct keys.
CSV_SHAPE = {
    "Symbol": "ADBE", "EBIT": 9.09e9, "CapitalEmployed": 2.169e9,
    "EnterpriseValue": 1.0e11, "LiveMarketCap": 9.9e10, "ROC_Pct": "419.09%",
    "EY_Pct": "9.05%", "ROIC_InclGoodwill": 0.7015512850196804,
    "IntangiblesShareOfAssets": 0.50289, "ROA": 0.3037,
    "BalanceSheetDate": "2026-05-29",
}

# Negative equity: the goodwill-inclusive companion is genuinely uncomputable.
NEG_EQUITY = {
    "Symbol": "BKNG", "EBIT": 9.491e9, "CapitalEmployed": 7.84e8,
    "EnterpriseValue": 1.5736e11, "LiveMarketCap": 1.5444e11, "ROC_Pct": "1210.59%",
    "EY_Pct": "6.03%", "ROIC_InclGoodwill": None, "TotalEquity": -8.724e9,
    "ROIC_Unavailable_Reason": "negative_invested_capital", "ROA": 0.3424,
    "BalanceSheetDate": "2026-03-31",
}

# Same company from a CSV written before ROIC_Unavailable_Reason existed.
LEGACY = {k: v for k, v in NEG_EQUITY.items() if k != "ROIC_Unavailable_Reason"}

# Positive equity, ROIC still absent: the negative-equity story must not be invented.
ODD = dict(LEGACY, TotalEquity=5.0e9)

# A fully-populated candidate: ranked, growing, with the multi-year "sums" EPS
# window and a capped growth rate. Exercises the branches the four above skip.
FULL = {
    "Symbol": "HRB", "Final_Rank": 7, "ROC_Pct": "88.40%", "EY_Pct": "11.20%",
    "EPSGrowth_Pct": "18.30%", "PEG_Ratio": 0.74, "PE_Ratio": 13.5,
    "MagicFormula_Score": 42, "Composite_Score": 61.5,
    "EPSGrowth": 0.183, "EPSGrowth_Years": 3, "EPS_Window": "sums",
    "EPS_Window_Years": 3, "EPS_Basis": "diluted",
    "EPS_Base": 4.11, "EPS_Base_Date": "2023-04-30",
    "EPS_Current": 6.82, "EPS_Current_Date": "2026-04-30",
    "BaseNetMargin": 0.0912, "EPSGrowth_Capped": True, "EPSGrowth_ForPEG": 0.50,
    "TotalDebt": 3.02e9, "Cash": 1.1e9, "TotalEquity": -1.2e8,
    "TotalAssets": 4.4e9, "GoodwillAndIntangibles": 1.05e9,
    "InvestedCapital": 1.8e9, "EnterpriseValue": 9.9e9, "LiveMarketCap": 8.0e9,
    "EBIT": 9.2e8, "CapitalEmployed": 1.04e9, "EBIT_Basis": "TTM",
    "BalanceSheetDate": "2026-04-30", "ROIC_InclGoodwill": 0.4822,
    "IntangiblesShareOfAssets": 0.2386, "ROA": 0.2091, "ROA_NetIncome": 0.1502,
}

# A fractional growth window (a changed fiscal year end) must not read as "3".
FRACTIONAL = dict(FULL, EPSGrowth_Years=3.25, EPS_Window="endpoints")

PRICE = {
    "price": 62.41, "as_of": "2026-08-14T20:00:00", "yearLow": 44.02,
    "yearHigh": 71.15, "pct_from_52w_high": -12.28, "priceAvg50": 58.9,
    "priceAvg200": 55.12, "marketCap": 8.0e9,
}
# A quote that came back partially filled — every formatter has an "n/a" path.
PARTIAL_PRICE = {"price": 62.41, "as_of": None, "marketCap": None}

# Reconciliation corpora, from the runs that motivated the gate.
FISV = {
    "Symbol": "FISV", "TotalDebt": 29_306_000_000.0, "Cash": 829_000_000.0,
    "LiveMarketCap": 27_894_516_740.0, "EnterpriseValue": 56_390_000_000.0,
    "TotalEquity": 26_221_000_000.0, "EBIT": 5_140_000_000.0,
    "CapitalEmployed": 3_760_000_000.0,
}
VICR = {
    "Symbol": "VICR", "TotalDebt": 7_608_000.0, "Cash": 453_582_000.0,
    "LiveMarketCap": 9_358_665_907.0, "EnterpriseValue": 8_912_966_907.0,
    "EBIT": 88_354_000.0,
}
SOLV = {
    "Symbol": "SOLV", "TotalDebt": 5_080_000_000.0, "Cash": 561_000_000.0,
    "LiveMarketCap": 12_000_000_000.0, "EnterpriseValue": 16_519_000_000.0,
}

RECONCILE_TEXTS = [
    ("fisv-after", FISV,
     "The company reports total debt of $36.9 billion against thin cash reserves."),
    ("fisv-before", FISV,
     "Without a major asset sale, Fiserv's $36.9 billion debt burden becomes an "
     "ongoing drag on earnings power."),
    ("fisv-bold", FISV,
     "lowering annual interest expense on its massive **$36.9 billion debt stack**"),
    ("fisv-comparison-row", FISV, "| **Market Cap** | ~$36.9B | ~$22.3B | ~$17.7B |"),
    ("fisv-two-cell-row", FISV, "| **Market Cap** | ~$36.9B |"),
    ("fisv-wrong-ev", FISV,
     "At an enterprise value of $75 billion the shares look expensive."),
    ("fisv-delta", FISV, "Total debt jumped by over $11 billion, a 42% rise."),
    ("fisv-paydown", FISV, "Management paid down nearly $4 billion of debt."),
    ("fisv-historical", FISV, "Total debt stood at $24.4 billion in 2023."),
    ("fisv-guidance", FISV, "Debt is guided to reach $32 billion by FY2027."),
    ("vicr-ebit-before-ev", VICR,
     "Current Earnings Yield: 0.99% (based on TTM EBIT of $88.35M on a verified "
     "Enterprise Value of $8.91B)."),
    ("vicr-cash-before-debt", VICR,
     "**Balance Sheet Cash / Debt:** Cash & Short-Term Investments of **$453.58M** "
     "vs. Total Debt of **$7.61M** (Net Cash: ~$445.97M)."),
    ("vicr-net-cash", VICR,
     "a verified net cash position of ~$446M ($453.58M cash and short-term "
     "investments vs. $7.61M total debt; net debt/EBITDA of -3.38)"),
    ("vicr-peer-table", VICR,
     "| **Market Capitalisation** | **$9.36B** (Verified) | $6.75B | $6.78B |\n"
     "| **Enterprise Value (EV)** | **$8.91B** (Verified) | ~$10.1B | ~$7.2B |"),
    ("vicr-ev-prose", VICR,
     "Vicor trades at an Enterprise Value of **$8.91 billion** on TTM operating "
     "income (EBIT) of just **$88.35 million**."),
    ("vicr-before-wins", VICR,
     "Vicor's market valuation sits at $9.36B Market Capitalization ($8.91B "
     "Enterprise Value) against trailing twelve-month operating profit of $88.35M."),
    ("vicr-new-clause", VICR,
     "$453.58M of cash against $7.61M total debt, giving it roughly $446 million "
     "in net cash to fund research."),
    ("vicr-against", VICR,
     "The company carries $7.61M in total debt against $453.58M in cash."),
    ("vicr-threshold", VICR,
     "Sell if total debt rises above $900.00M in any future quarter."),
    ("vicr-peer-ticker", VICR,
     "* **Akebia Therapeutics, Inc. (AKBA):** a peer commercial-stage "
     "biopharmaceutical company ($327M market cap) selling niche therapies."),
    ("vicr-own-ticker", VICR,
     "Vicor Corporation (VICR) reports a market capitalisation of $2.00B."),
    ("vicr-wrong-ev", VICR,
     "TTM EBIT of $88.35M on a stated Enterprise Value of $2.10B."),
    ("vicr-wrong-debt", VICR, "Cash of $453.58M vs. Total Debt of $900.00M."),
    ("solv-trigger-heading", SOLV,
     "### Sell Trigger 3: Re-leveraging Above $6.00B Total Debt or Interest "
     "Coverage Ratio Falling Below 2.0x for Two Consecutive Quarters"),
    ("solv-trigger-prose", SOLV,
     "**Actionable Sell Threshold:** Total Debt increasing back above **$6.00B** "
     "(a $920M increase from current levels)."),
    ("solv-trigger-cell", SOLV,
     "| **3. Balance Sheet Stress** | **$5.08B** Total Debt | Total Debt **> $6.00B** "
     "OR Interest Coverage **< 2.0x** for 2 quarters |"),
    ("solv-correct-baseline", SOLV,
     "* **Total Debt:** **$5.08B** ($5,080,000,000 as of 2026-03-31)."),
    ("solv-correct-prose", SOLV,
     "A medtech company carrying **$5.08B in total debt** cannot sustain negative "
     "organic cash generation."),
    ("solv-wrong-baseline", SOLV,
     "Current total debt of $8.92B. Sell if total debt rises above $9.50B."),
    ("stored-report-not-rescanned", VICR, """
Vicor holds $453.58M of cash against $7.61M of total debt.

## Data Reconciliation Warnings

| Section | Figure | Stated in report | Verified | Basis | Off by |
| :--- | :--- | ---: | ---: | :--- | ---: |
| Bull case | enterprise value | $88.35M | $8.91B | market price | 99.0% |
| Sale advisory | total debt | $453.58M | $7.61M | filings | 5861.9% |
"""),
    ("empty-text", VICR, ""),
    ("no-verified-figures", {}, "Total debt of $99 billion."),
]

CANDIDATES = [
    ("csv-shape", CSV_SHAPE), ("neg-equity", NEG_EQUITY), ("legacy", LEGACY),
    ("odd", ODD), ("full", FULL), ("fractional", FRACTIONAL),
    ("fisv", FISV), ("vicr", VICR), ("solv", SOLV),
    ("empty", {}), ("none", None),
]

# Every branch of _no_peg_reason, which no test currently reads.
NO_PEG = [
    ("has-peg", {"PEG_Ratio": 0.8}),
    ("shrinking", {"EPSGrowth": -0.04}),
    ("no-pe", {"EPSGrowth": 0.12, "PE_Ratio": None}),
] + [
    (reason, {"EPSGrowth_Unavailable_Reason": reason}) for reason in (
        "non_positive_base_eps", "non_positive_current_eps", "insufficient_history",
        "stale_eps_history", "no_eps_reported", "fetch_failed", "unrecognised_code",
    )
]

PRESENT_VALUES = [
    None, "", "  ", "nan", "NaN", "none", "None", 0, 0.0, 1, -1.5, "42%",
    float("nan"), "text",
]


def resolve(name):
    """A report symbol, wherever it currently lives.

    Every one of these was an attribute of `main` before the report code moved into
    `report/`. The ones the rest of the codebase calls are still re-exported there;
    the gate's tuning constants are not, because nothing outside the gate ever read
    them. Looking in both places is what lets one snapshot file compare across the
    move — the printed name and value are the same either way, so an unchanged
    constant still diffs empty.
    """
    if hasattr(main, name):
        return getattr(main, name)
    import report
    from report import reconciliation
    for mod in (report, reconciliation):
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(name)


def section(out, title):
    out.write("\n" + "=" * 78 + f"\n== {title}\n" + "=" * 78 + "\n")


def main_snapshot(path):
    with open(path, "w", encoding="utf-8") as out:
        section(out, "_prompt_version")
        out.write(main._prompt_version() + "\n")

        section(out, "_present")
        for v in PRESENT_VALUES:
            out.write(f"{v!r:>12} -> {main._present(v)!r}\n")

        section(out, "_normalize_candidate")
        for name, cand in CANDIDATES:
            norm = main._normalize_candidate(cand)
            out.write(f"--- {name}\n")
            for k in sorted(norm or {}):
                out.write(f"  {k} = {norm[k]!r}\n")

        section(out, "_growth_window")
        for name, cand in CANDIDATES:
            out.write(f"{name:>12} -> {main._growth_window(cand)!r}\n")

        section(out, "_eps_window_phrase")
        for name, cand in CANDIDATES:
            out.write(f"--- {name}\n{main._eps_window_phrase(cand)}\n")

        section(out, "_no_peg_reason")
        for name, cand in NO_PEG + CANDIDATES:
            out.write(f"--- {name}\n{main._no_peg_reason(cand)}\n")

        section(out, "_verified_figures")
        for name, cand in CANDIDATES:
            figs = main._verified_figures(cand)
            out.write(f"--- {name}\n")
            for k in sorted(figs):
                out.write(f"  {k} = {figs[k]!r}\n")

        section(out, "_format_verified_figures")
        for name, cand in CANDIDATES:
            for pname, price in (("no-price", None), ("price", PRICE),
                                 ("partial", PARTIAL_PRICE)):
                out.write(f"--- {name} / {pname}\n")
                out.write(main._format_verified_figures(
                    main._normalize_candidate(cand), price) + "\n")

        section(out, "_format_screen_context")
        for name, cand in CANDIDATES:
            out.write(f"--- {name}\n")
            out.write(main._format_screen_context(
                main._normalize_candidate(cand) or {}) + "\n")

        section(out, "_format_price_section")
        for pname, price in (("full", PRICE), ("partial", PARTIAL_PRICE),
                             ("empty", {}), ("none", None)):
            out.write(f"--- {pname}\n")
            out.write(main._format_price_section(price, "ZZZZ") + "\n")

        section(out, "_format_magic_formula_section")
        for name, cand in CANDIDATES:
            out.write(f"--- {name}\n")
            out.write(main._format_magic_formula_section(
                main._normalize_candidate(cand)) + "\n")

        section(out, "_line_bounds / _amount_for_label / _reconcile_agent_figures")
        for name, cand, text in RECONCILE_TEXTS:
            found = main._reconcile_agent_figures(text, cand, "snapshot")
            out.write(f"--- {name}\n")
            for f in found:
                out.write("  " + repr(sorted(f.items())) + "\n")
            if not found:
                out.write("  (silent)\n")

        section(out, "_finding_basis")
        for key in ("TotalDebt", "Cash", "LiveMarketCap", "EnterpriseValue",
                    "TotalEquity", "Unknown"):
            out.write(f"{key:>22} -> {main._finding_basis(key)}\n")

        section(out, "reconciliation_warning")
        for name, cand, text in RECONCILE_TEXTS:
            for f in main._reconcile_agent_figures(text, cand, "snapshot"):
                out.write(f"--- {name}\n")
                out.write(main.reconciliation_warning("ZZZZ", f) + "\n")
                out.write(main.reconciliation_warning("ZZZZ", f, True) + "\n")

        section(out, "_format_reconciliation_section")
        all_findings = []
        for name, cand, text in RECONCILE_TEXTS:
            found = main._reconcile_agent_figures(text, cand, "snapshot")
            all_findings += found
            if found:
                out.write(f"--- {name}\n")
                out.write(main._format_reconciliation_section(found) + "\n")
        out.write("--- empty\n")
        out.write(repr(main._format_reconciliation_section([])) + "\n")
        out.write("--- all findings together\n")
        out.write(main._format_reconciliation_section(all_findings) + "\n")
        # Filings-only, so the market-price legend must NOT appear.
        out.write("--- filings only\n")
        out.write(main._format_reconciliation_section(
            [f for f in all_findings if f.get("basis") == "filings"]) + "\n")

        section(out, "constants")
        for const in ("_RECONCILIATION_HEADING", "RECONCILE_TOLERANCE",
                      "_FIELD_TOLERANCE", "_RECONCILED_FIELDS", "_MULTIPLIER",
                      "_MARKET_DERIVED_FIELDS", "_RATIO_TO_PCT_FIELD",
                      "_COMPARISON_ROW_MIN_AMOUNTS", "_FORWARD_SCAN",
                      "_BACKWARD_GAP_MAX", "_CONNECTOR_GAP_MAX",
                      "_THRESHOLD_PROXIMITY", "_DELTA_LOOKBEHIND",
                      "_PERIOD_PROXIMITY", "_CONCEPT_LOOKBEHIND"):
            value = resolve(const)
            # Sort dicts and sets before printing. A set's repr order follows string
            # hashing, which is randomised per process, so an unsorted set diffs
            # against itself between two runs of this tool and buries a real change
            # in noise.
            if isinstance(value, dict):
                value = sorted((k, repr(v)) for k, v in value.items())
            elif isinstance(value, (set, frozenset)):
                value = sorted(value)
            out.write(f"{const} = {value!r}\n")


if __name__ == "__main__":
    main_snapshot(sys.argv[1])
    print(f"wrote {sys.argv[1]}")
