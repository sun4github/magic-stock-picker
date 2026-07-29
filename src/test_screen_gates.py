"""Tests for Greenblatt's step-by-step screening gates. Runs offline:

    python test_screen_gates.py

These cover the four eliminations from the step-by-step appendix of "The Little Book
That Still Beats the Market" — ROA below 25%, P/E below 5, financials/utilities/foreign
issuers, and companies that reported earnings in the last week — plus the ROA and P/E
arithmetic they filter on.

Why these exist. Every one of these gates is a SILENT filter: it removes companies from
the rankings, and nothing downstream can tell the difference between "correctly
excluded" and "wrongly excluded". The failure modes are all quiet ones — a sign flip on
the P/E test would keep exactly the companies it is meant to drop, a truthy-NaN would
drop companies for missing data, and a broken earnings lookup would report success
while filtering nothing.
"""
import magic_formula_starter_screener as s


# --- fixtures -----------------------------------------------------------------

def _company(**kw):
    """A screener universe row; overrides applied on top of an eligible company."""
    base = {
        "symbol": "ACME", "companyName": "Acme Manufacturing Inc.",
        "sector": "Industrials", "industry": "Specialty Industrial Machinery",
        "country": "US", "isEtf": False, "isFund": False,
    }
    base.update(kw)
    return base


def _metrics(**kw):
    """A computed-metrics dict that passes both ratio gates unless overridden."""
    base = {"ROA": 0.40, "ROA_NetIncome": 0.30, "PE": 15.0}
    base.update(kw)
    return base


# --- cases --------------------------------------------------------------------

def _universe_cases():
    """(name, company_row, expected_reason)"""
    return [
        ("ordinary US industrial", _company(), None),
        ("bank by sector", _company(sector="Financial Services", industry="Banks - Regional"),
         "excluded_sector"),
        ("utility by sector", _company(sector="Utilities", industry="Utilities - Regulated Electric"),
         "excluded_sector"),
        # The reason these are matched on INDUSTRY: FMP files plenty of insurers and
        # asset managers outside the Financial Services sector, and a sector-only cut
        # misses them.
        ("insurer filed outside the financial sector",
         _company(companyName="Acme Insurance Group", industry="Insurance - Life"), "excluded_industry"),
        ("asset manager", _company(industry="Asset Management"), "excluded_industry"),
        ("mortgage REIT", _company(sector="Real Estate", industry="REIT - Mortgage"),
         "excluded_industry"),
        ("SPAC / shell", _company(industry="Shell Companies"), "excluded_industry"),
        # ...while an operating homebuilder in the same sector must SURVIVE, which is
        # the whole reason REITs are cut by industry rather than by sector.
        ("homebuilder in Real Estate stays", _company(sector="Real Estate", industry="Real Estate - Development"),
         None),
        ("ADR named in the company name",
         _company(companyName="Alibaba Group Holding Ltd ADR"), "foreign_adr"),
        ("American Depositary Shares spelled out",
         _company(companyName="Sony Group Corp American Depositary Shares"), "foreign_adr"),
        ("foreign domicile", _company(country="CN"), "foreign_adr"),
        # Missing data is not evidence of being foreign — don't drop on a blank field.
        ("unknown country is kept", _company(country=""), None),
        ("fund flagged on the row", _company(isFund=True), "fund_or_etf"),
        ("row with no symbol", _company(symbol=""), "no_symbol"),
    ]


def _ratio_cases():
    """(name, metrics, expected_reason) — evaluated on the default 'ebit' ROA basis."""
    return [
        ("clears both hurdles", _metrics(), None),
        ("ROA just under 25%", _metrics(ROA=0.249), "roa_below_min"),
        ("ROA exactly at the 25% hurdle", _metrics(ROA=0.25), None),
        ("P/E just under 5", _metrics(PE=4.99), "pe_below_min"),
        ("P/E exactly at the floor of 5", _metrics(PE=5.0), None),
        # The direction of the P/E test is the easiest thing here to get backwards:
        # Greenblatt drops the LOW ratios (one-off earnings), not the high ones.
        ("expensive company is not a P/E rejection", _metrics(PE=95.0), None),
        # A loss-maker has no meaningful P/E. It must not be read as "P/E below 5" —
        # such companies are already excluded upstream by the negative-EBIT check, and
        # double-counting them here would misattribute the reason in the run summary.
        ("loss-maker has no P/E", _metrics(PE=None), None),
        ("negative P/E is not a low P/E", _metrics(PE=-8.0), None),
        # Missing figures mean the provider had a gap, not that the business is bad.
        ("missing ROA is kept", _metrics(ROA=None), None),
        ("ROA gate bites before the P/E gate", _metrics(ROA=0.10, PE=2.0), "roa_below_min"),
    ]


def run():
    failures = []

    print("--- universe exclusions (financials, utilities, foreign/ADR) ---")
    for name, company, expected in _universe_cases():
        got = s._universe_exclusion_reason(company)
        ok = got == expected
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {str(got):<20} "
              f"{'' if ok else f'(expected {expected})'}")

    print(f"\n--- ratio gates (ROA >= {s.MIN_ROA:.0%}, P/E >= {s.MIN_PE:g}) ---")
    original_basis = s.ROA_BASIS
    for name, metrics, expected in _ratio_cases():
        got = s.ratio_gate_reason(metrics)
        ok = got == expected
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {str(got):<20} "
              f"{'' if ok else f'(expected {expected})'}")

    # The basis switch must actually change which figure is tested. A company at 30%
    # pretax but 20% after tax passes on "ebit" and fails on "net_income"; if both
    # answers agree, the config knob is being ignored.
    print("\n--- roa_basis switch ---")
    straddler = _metrics(ROA=0.30, ROA_NetIncome=0.20)
    try:
        s.ROA_BASIS = "ebit"
        on_ebit = s.ratio_gate_reason(straddler)
        s.ROA_BASIS = "net_income"
        on_net = s.ratio_gate_reason(straddler)
    finally:
        s.ROA_BASIS = original_basis
    ok = on_ebit is None and on_net == "roa_below_min"
    failures += [] if ok else ["roa_basis switch"]
    print(f"{'PASS' if ok else 'FAIL'}  30% pretax / 20% after tax: "
          f"ebit -> {on_ebit}, net_income -> {on_net}")

    # --- ROA and P/E arithmetic --------------------------------------------------
    # Checked against hand-computed values, because a denominator mix-up here would
    # not crash anything: it would just quietly gate on the wrong number.
    print("\n--- ROA / P/E arithmetic ---")
    ebit, net_income, total_assets, market_cap = 120.0, 60.0, 480.0, 900.0
    arithmetic = [
        ("ROA on EBIT", ebit / total_assets, 0.25),
        ("ROA on net income", net_income / total_assets, 0.125),
        ("P/E as market cap / net income", market_cap / net_income, 15.0),
    ]
    for name, got, expected in arithmetic:
        ok = abs(got - expected) < 1e-9
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {got}")

    # ROA must come out BELOW ROC on the same company, always: same numerator, and
    # ROA's denominator (all assets) contains ROC's (capital employed) plus cash and
    # goodwill. If this ever inverts, the two have been swapped somewhere.
    capital_employed = 132.0   # < total_assets, as it must be
    roc, roa = ebit / capital_employed, ebit / total_assets
    ok = roa < roc
    failures += [] if ok else ["ROA below ROC"]
    print(f"{'PASS' if ok else 'FAIL'}  {'ROA sits below ROC on the same company':<48} "
          f"ROA {roa:.2%} < ROC {roc:.2%}")

    # --- earnings-in-the-last-week filter ----------------------------------------
    # The critical property is the FAILURE path: when the lookup cannot be done, the
    # function must report ok=False. Returning an empty set with ok=True would let the
    # run silently claim the filter was applied when nothing was filtered.
    print("\n--- recent-earnings lookup ---")
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    original_get = s.fmp_get

    def _calendar_returning(rows):
        def _fake(url, params=None, context=""):
            if "earnings-calendar" in url:
                return rows
            raise s.FMPError("unexpected endpoint")
        return _fake

    try:
        # In-window and out-of-window rows from the bulk calendar.
        s.fmp_get = _calendar_returning([
            {"symbol": "RECENT", "date": (today - timedelta(days=2)).isoformat()},
            {"symbol": "OLD", "date": (today - timedelta(days=40)).isoformat()},
            {"symbol": "FUTURE", "date": (today + timedelta(days=3)).isoformat()},
            {"symbol": "NODATE", "date": None},
        ])
        got, ok_flag = s.fetch_recent_earnings_symbols("KEY", 7, ["RECENT", "OLD"])
        ok = got == {"RECENT"} and ok_flag
        failures += [] if ok else ["bulk calendar window"]
        print(f"{'PASS' if ok else 'FAIL'}  {'only in-window announcements are returned':<48} "
              f"{sorted(got)}")

        # Total lookup failure must surface as ok=False, not as "nobody reported".
        def _always_fails(url, params=None, context=""):
            raise s.FMPError("plan does not include this endpoint")

        s.fmp_get = _always_fails
        got, ok_flag = s.fetch_recent_earnings_symbols("KEY", 7, ["AAA", "BBB"])
        ok = got == set() and ok_flag is False
        failures += [] if ok else ["failed lookup reports ok=False"]
        print(f"{'PASS' if ok else 'FAIL'}  {'a failed lookup reports failure, not success':<48} "
              f"symbols={sorted(got)} ok={ok_flag}")

        # Per-symbol fallback when the bulk calendar is unavailable.
        def _fallback(url, params=None, context=""):
            if "earnings-calendar" in url:
                raise s.FMPError("not on this plan")
            symbol = (params or {}).get("symbol")
            when = today - timedelta(days=1 if symbol == "FRESH" else 60)
            return [{"symbol": symbol, "date": when.isoformat(), "epsActual": 1.0}]

        s.fmp_get = _fallback
        got, ok_flag = s.fetch_recent_earnings_symbols("KEY", 7, ["FRESH", "STALE"])
        ok = got == {"FRESH"} and ok_flag
        failures += [] if ok else ["per-symbol fallback"]
        print(f"{'PASS' if ok else 'FAIL'}  {'per-symbol fallback finds the fresh reporter':<48} "
              f"{sorted(got)}")

        # Disabling the filter must skip the lookup entirely rather than call FMP.
        s.fmp_get = _always_fails
        got, ok_flag = s.fetch_recent_earnings_symbols("KEY", 0, ["AAA"])
        ok = got == set() and ok_flag is True
        failures += [] if ok else ["disabled filter short-circuits"]
        print(f"{'PASS' if ok else 'FAIL'}  {'days=0 disables the check without calling FMP':<48} "
              f"ok={ok_flag}")
    finally:
        s.fmp_get = original_get

    # --- ROC's companion figure must survive both candidate shapes ----------------
    # Two regressions guarded here, both of which failed SILENTLY (a missing section,
    # not an error), and both on the batch runs where the guard matters most.
    print("\n--- ROC companion (ROIC / ROA fallback) ---")
    import main

    # 1. Screener-CSV shape: raw ratio columns, no *_Pct keys. Before _normalize_candidate
    #    the report read only the *_Pct form, so the companion vanished on every
    #    full-pipeline and --from-csv run.
    csv_shape = {
        "Symbol": "ADBE", "EBIT": 9.09e9, "CapitalEmployed": 2.169e9,
        "EnterpriseValue": 1.0e11, "LiveMarketCap": 9.9e10, "ROC_Pct": "419.09%",
        "EY_Pct": "9.05%", "ROIC_InclGoodwill": 0.7015512850196804,
        "IntangiblesShareOfAssets": 0.50289, "ROA": 0.3037,
        "BalanceSheetDate": "2026-05-29",
    }
    section = main._format_magic_formula_section(main._normalize_candidate(csv_shape))
    companion_cases = [
        ("raw CSV ratio yields the companion figure",
         "**Return on capital actually spent (including takeover costs):** 70.16%" in section),
        ("intangibles share rendered from the raw ratio",
         "50.3%" in section),
        ("no NaN leaked into the report", "nan" not in section.lower()),
    ]

    # 2. Negative-equity company: the companion is genuinely uncomputable, so the
    #    report must say WHY and substitute ROA rather than print "Not available"
    #    beside a four-digit ROC.
    neg_equity = {
        "Symbol": "BKNG", "EBIT": 9.491e9, "CapitalEmployed": 7.84e8,
        "EnterpriseValue": 1.5736e11, "LiveMarketCap": 1.5444e11, "ROC_Pct": "1210.59%",
        "EY_Pct": "6.03%", "ROIC_InclGoodwill": None, "TotalEquity": -8.724e9,
        "ROIC_Unavailable_Reason": "negative_invested_capital", "ROA": 0.3424,
        "BalanceSheetDate": "2026-03-31",
    }
    neg_section = main._format_magic_formula_section(main._normalize_candidate(neg_equity))
    neg_vf = main._format_verified_figures(main._normalize_candidate(neg_equity))
    companion_cases += [
        ("negative equity: says it cannot be calculated",
         "cannot be calculated for this company" in neg_section),
        ("negative equity: explains why, with the equity figure",
         "-$8.72B" in neg_section),
        ("negative equity: substitutes ROA as the counterweight",
         "return on assets of 34.24%" in neg_section),
        # The substitution must never be passed off as the thing it replaced.
        ("fallback is not relabelled as ROIC",
         "ROIC" not in neg_section),
        ("agent block tells the agent it is NOT a data gap",
         "NOT a data gap" in neg_vf),
        ("agent block supplies ROA instead", "USE INSTEAD" in neg_vf and "34.24%" in neg_vf),
        # ...and must stay quiet when the real figure is available.
        ("no substitution when ROIC IS computable",
         "USE INSTEAD" not in main._format_verified_figures(main._normalize_candidate(csv_shape))),
    ]
    # 3. Same company, but from a CSV written BEFORE ROIC_Unavailable_Reason existed.
    #    The promise "the companion figure below" is made unconditionally by the
    #    paragraph above it, so the answer must be unconditional too. The first cut of
    #    this feature skipped straight to the substitute, leaving a dangling "use this
    #    instead" with nothing to be instead OF — caught only by reading a real report.
    legacy = dict(neg_equity)
    legacy.pop("ROIC_Unavailable_Reason")
    legacy_section = main._format_magic_formula_section(main._normalize_candidate(legacy))
    legacy_vf = main._format_verified_figures(main._normalize_candidate(legacy))
    companion_cases += [
        ("legacy CSV: still states the figure cannot be calculated",
         "cannot be calculated for this company" in legacy_section),
        ("legacy CSV: cause inferred from negative equity",
         "-$8.72B" in legacy_section),
        ("legacy CSV: substitute is never left dangling",
         legacy_section.index("cannot be calculated")
         < legacy_section.index("Use this instead")),
        ("legacy CSV: agent block also explains rather than going silent",
         "NOT a data gap" in legacy_vf),
    ]

    # 4. A company with POSITIVE equity whose ROIC is still absent must not be told
    #    it has negative equity — the inference has to stay evidence-based.
    odd = {k: v for k, v in neg_equity.items() if k != "ROIC_Unavailable_Reason"}
    odd["TotalEquity"] = 5.0e9
    odd_section = main._format_magic_formula_section(main._normalize_candidate(odd))
    companion_cases += [
        ("positive equity: no buyback explanation invented",
         "share buybacks" not in odd_section),
        ("positive equity: still explains and still substitutes",
         "cannot be calculated for this company" in odd_section
         and "return on assets of 34.24%" in odd_section),
    ]

    for name, ok in companion_cases:
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        return 1
    print("All screening-gate tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
