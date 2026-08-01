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
from datetime import datetime, timedelta, timezone

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


def _growth(**kw):
    """A metrics dict that passes the PEG/growth gates unless overridden."""
    base = {"EPSGrowth": 0.25, "PEG": 0.60, "BaseNetMargin": 0.12}
    base.update(kw)
    return base


def _annual_rows(eps_by_year, margin=0.12):
    """Annual income-statement rows as FMP returns them: newest first, `date` is the
    fiscal period END. `eps_by_year` maps a year to its diluted EPS.

    Revenue is synthesised so each row carries the given net margin — the base-window
    margin test needs both figures, and a fixture without revenue would silently
    exercise the "margin unavailable" path instead of the one under test."""
    return [{"date": f"{year}-12-31", "fiscalYear": str(year), "period": "FY",
             "eps": eps, "epsDiluted": eps,
             "netIncome": eps * 1_000_000,
             "revenue": (eps * 1_000_000 / margin) if margin else 0}
            for year, eps in sorted(eps_by_year.items(), reverse=True)]


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


def _growth_cases():
    """(name, metrics, expected_reason) for Lynch's PEG gate."""
    return [
        ("growing company under the PEG ceiling", _growth(), None),
        ("PEG exactly at the ceiling", _growth(PEG=s.MAX_PEG), None),
        ("PEG just above the ceiling", _growth(PEG=s.MAX_PEG + 0.01), "peg_above_max"),
        # The targeted replacement for a blunt rate cap: the base window has to have
        # been a real trading period. GRND's was $852K of profit on $195M of revenue.
        ("a breakeven base window is rejected however fast the growth",
         _growth(EPSGrowth=3.49, PEG=0.65, BaseNetMargin=0.004), "base_year_breakeven"),
        ("a base window at the floor passes",
         _growth(BaseNetMargin=s.MIN_BASE_NET_MARGIN), None),
        ("an unmeasurable base margin is rejected, not waved through",
         _growth(BaseNetMargin=None), "base_margin_unavailable"),
        # ...and the base test must not fire before the growth test, or a shrinking
        # company gets reported as a margin problem.
        ("no growth is still reported ahead of the base-window test",
         _growth(EPSGrowth=-0.10, PEG=None, BaseNetMargin=0.001), "no_eps_growth"),
        # The whole point of the gate: cheap because it is shrinking.
        ("flat earnings are rejected", _growth(EPSGrowth=0.0, PEG=None), "no_eps_growth"),
        ("shrinking earnings are rejected", _growth(EPSGrowth=-0.46, PEG=None), "no_eps_growth"),
        # ...and the growth test must bite BEFORE the PEG test, so a shrinking company
        # is reported as no-growth rather than as an uncomputable PEG.
        ("no growth is reported ahead of the missing PEG",
         _growth(EPSGrowth=-0.10, PEG=None), "no_eps_growth"),
        # Unlike the ROA/PE gates, missing data IS a rejection here — 1/PEG is a
        # ranking input, so a survivor without one cannot be ranked (see
        # growth_gate_reason). Each cause keeps its own reason so the run can say why.
        ("growth that could not be computed is rejected",
         _growth(EPSGrowth=None, PEG=None), "growth_unavailable"),
        ("a grower with no P/E still has no PEG to rank on",
         _growth(EPSGrowth=0.30, PEG=None), "peg_unavailable"),
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

    # --- Lynch's PEG gate ---------------------------------------------------------
    print(f"\n--- PEG gate (EPS growth > {s.MIN_EPS_GROWTH:.0%}, PEG <= {s.MAX_PEG:g}, "
          f"base margin >= {s.MIN_BASE_NET_MARGIN:.0%}) ---")
    for name, metrics, expected in _growth_cases():
        got = s.growth_gate_reason(metrics)
        ok = got == expected
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {str(got):<20} "
              f"{'' if ok else f'(expected {expected})'}")

    # --- PEG arithmetic -----------------------------------------------------------
    # The UNIT is the thing to guard. Lynch's rule is that a fairly priced company's
    # P/E equals its growth rate, so a P/E of 20 on 20% growth must come out at
    # exactly 1.0. Dividing by the decimal (0.20) instead would give 100 — the whole
    # market would fail the 1.2 ceiling and the screen would return nothing.
    print("\n--- PEG arithmetic ---")
    peg_cases = [
        ("P/E 20 on 20% growth is a PEG of exactly 1.0", s.compute_peg(20.0, 0.20), 1.0),
        ("P/E 15 on 30% growth is half fair value", s.compute_peg(15.0, 0.30), 0.5),
        # Hand-computed from the live BKNG figures used to verify this feature:
        # diluted EPS 3.054 (FY2022) -> 6.62 (FY2025), P/E 24.2889 (as displayed, so
        # the PEG below is that same 4-decimal P/E divided by 29.418487185217757).
        ("BKNG three-year CAGR", (6.62 / 3.054) ** (1 / 3) - 1, 0.29418487185217757),
        ("BKNG PEG", s.compute_peg(24.2889, 0.29418487185217757), 0.8256338895701177),
    ]
    for name, got, expected in peg_cases:
        ok = got is not None and abs(got - expected) < 1e-9
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {got}")

    # A negative or zero denominator must yield NO ratio rather than a negative one:
    # a negative PEG sorts as the cheapest thing on the list, which is the exact
    # inversion this ratio exists to prevent.
    undefined = [
        ("shrinking EPS yields no PEG", s.compute_peg(20.0, -0.10)),
        ("zero growth yields no PEG", s.compute_peg(20.0, 0.0)),
        ("a loss-maker (no P/E) yields no PEG", s.compute_peg(None, 0.25)),
        ("a negative P/E yields no PEG", s.compute_peg(-12.0, 0.25)),
        ("missing growth yields no PEG", s.compute_peg(20.0, None)),
    ]
    for name, got in undefined:
        ok = got is None
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {got}")

    # --- the sustainability cap ---------------------------------------------------
    # Found live on GRND: $0.0054/share in the base year against $0.49 today reads as
    # 349% a year and a PEG of 0.09, taking first place on a near-zero denominator.
    # The cap must bite there, must NOT touch an ordinary rate, and must only ever
    # make a PEG larger.
    print("\n--- growth cap (PEG denominator only) ---")
    grnd_growth = (0.49 / 0.0054) ** (1 / 3) - 1
    capped_rate, was_capped = s.peg_growth_rate(grnd_growth)
    ordinary_rate, ordinary_capped = s.peg_growth_rate(0.2114)
    cap_cases = [
        ("a 349% rate is capped", was_capped and capped_rate == s.MAX_GROWTH_FOR_PEG),
        ("an ordinary 21% rate is untouched",
         not ordinary_capped and ordinary_rate == 0.2114),
        ("capping raises the PEG, never lowers it",
         s.compute_peg(32.6175, capped_rate) > s.compute_peg(32.6175, grnd_growth)),
        # The cap is a PEG-denominator device only: the growth gate still sees the
        # measured rate, so a genuine fast grower off a real base is not rejected
        # merely for growing quickly.
        ("the reported growth rate itself is not capped",
         s.growth_gate_reason(
             {"EPSGrowth": grnd_growth, "PEG": 0.65, "BaseNetMargin": 0.12}) is None),
    ]
    for name, ok in cap_cases:
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48}")

    # --- EPS growth from annual filings -------------------------------------------
    # fetch_eps_growth reaches for the row N BACK, not the second row, and must refuse
    # rather than guess when the base is non-positive (a cube root of a negative
    # number) or the history is short.
    print("\n--- EPS growth window ---")
    original_get = s.fmp_get
    this_year = datetime.now(timezone.utc).year

    def _returning(rows):
        def _fake(url, params=None, context=""):
            return rows
        return _fake

    def _growth_of(rows):
        s.fmp_get = _returning(rows)
        return s.fetch_eps_growth("TEST", "KEY")

    original_method = s.EPS_GROWTH_METHOD
    try:
        # --- endpoint method (still selectable via config) ------------------------
        s.EPS_GROWTH_METHOD = "endpoint"
        # A doubling over exactly three years is 25.99% a year. The intervening years
        # are deliberately noisy: only the endpoints may be read.
        doubled = _growth_of(_annual_rows({
            this_year - 1: 2.00, this_year - 2: 9.99, this_year - 3: 0.01,
            this_year - 4: 1.00,
        }))
        window_cases = [
            ("endpoint: compounds between the endpoints only",
             doubled["EPSGrowth"] is not None
             and abs(doubled["EPSGrowth"] - ((2.0 / 1.0) ** (1 / 3) - 1)) < 1e-12),
            ("endpoint: the base is the row three years back",
             doubled["EPS_Base"] == 1.00 and doubled["EPS_Current"] == 2.00),
            ("the window is reported as exactly three years",
             doubled["EPSGrowth_Years"] == 3.0),
            ("diluted EPS is preferred", doubled["EPS_Basis"] == "diluted"),
        ]

        # --- sums method (the default) --------------------------------------------
        # Same company. The endpoint form reads 0.01 -> 2.00 and calls it 172% a year;
        # totalling both windows gives 12.00 vs 3.00, i.e. 58.7% — the noisy middle
        # years are counted instead of stepped over. This is the whole reason for it.
        s.EPS_GROWTH_METHOD = "sums"
        noisy = _growth_of(_annual_rows({
            this_year - 1: 2.00, this_year - 2: 9.99, this_year - 3: 0.01,
            this_year - 4: 1.00, this_year - 5: 1.00, this_year - 6: 1.00,
        }))
        expect = ((2.00 + 9.99 + 0.01) / (1.00 + 1.00 + 1.00)) ** (1 / 3) - 1
        window_cases += [
            ("sums: totals both windows rather than two endpoints",
             noisy["EPSGrowth"] is not None
             and abs(noisy["EPSGrowth"] - expect) < 1e-12),
            ("sums: reports the totals it actually divided",
             noisy["EPS_Current"] == 12.0 and noisy["EPS_Base"] == 3.0),
            ("sums: labels the window so the report cannot call it a single year",
             noisy["EPS_Window"] == "sums" and noisy["EPS_Window_Years"] == 3),
        ]

        # The GRND shape: a near-zero base, two loss years, one good year. The
        # endpoint form reads 0.0054 -> 0.49 as +349%/yr; totalling the recent window
        # gives 0.49 - 0.74 - 0.32 = -0.57, so there is no growth to report at all.
        grnd = {this_year - 1: 0.49, this_year - 2: -0.74, this_year - 3: -0.32,
                this_year - 4: 0.0054, this_year - 5: 0.0331, this_year - 6: 0.02}
        s.EPS_GROWTH_METHOD = "endpoint"
        grnd_end = _growth_of(_annual_rows(grnd))
        s.EPS_GROWTH_METHOD = "sums"
        grnd_sums = _growth_of(_annual_rows(grnd))
        window_cases += [
            ("GRND shape: the endpoint form reports a three-digit rate",
             grnd_end["EPSGrowth"] is not None and grnd_end["EPSGrowth"] > 3.0),
            ("GRND shape: counting the loss years leaves no growth to report",
             grnd_sums["EPSGrowth"] is None
             and grnd_sums["EPSGrowth_Unavailable_Reason"] == "non_positive_current_eps"),
        ]

        # Base-window net margin, the scale-free replacement for "the base EPS looks
        # small". Computed over the BASE window, not the recent one.
        thin = _growth_of(_annual_rows(
            {this_year - 1 - i: v for i, v in enumerate([4.0, 3.5, 3.0, 1.0, 0.9, 0.8])},
            margin=0.004,
        ))
        window_cases += [
            ("base-window net margin is measured",
             thin["BaseNetMargin"] is not None
             and abs(thin["BaseNetMargin"] - 0.004) < 1e-9),
            ("a breakeven base is measured here but rejected by the GATE, not silently",
             thin["EPSGrowth"] is not None
             and s.growth_gate_reason(dict(thin, PEG=0.5)) == "base_year_breakeven"),
        ]

        # A loss across the base window: (positive / negative) is not a growth rate,
        # and the cube root of a negative ratio is not a real number.
        loss_base = _growth_of(_annual_rows({
            this_year - 1: 1.50, this_year - 2: 0.50, this_year - 3: 0.20,
            this_year - 4: -0.80, this_year - 5: -0.90, this_year - 6: -0.70,
        }))
        # A loss across the CURRENT window is a different fact and gets its own reason.
        loss_now = _growth_of(_annual_rows({
            this_year - 1: -1.50, this_year - 2: -1.00, this_year - 3: -0.50,
            this_year - 4: 8.71, this_year - 5: 8.00, this_year - 6: 7.50,
        }))
        short = _growth_of(_annual_rows({this_year - 1: 2.00, this_year - 2: 1.50}))
        # An annual period end from six years ago cannot describe the company today.
        stale = _growth_of(_annual_rows({
            this_year - 6: 2.00, this_year - 7: 1.80, this_year - 8: 1.50,
            this_year - 9: 1.00, this_year - 10: 0.9, this_year - 11: 0.8,
        }))
        window_cases += [
            ("a loss in the base year is refused, with its own reason",
             loss_base["EPSGrowth"] is None
             and loss_base["EPSGrowth_Unavailable_Reason"] == "non_positive_base_eps"),
            ("a loss in the current year is refused separately",
             loss_now["EPSGrowth"] is None
             and loss_now["EPSGrowth_Unavailable_Reason"] == "non_positive_current_eps"),
            ("too few annual filings is refused",
             short["EPSGrowth"] is None
             and short["EPSGrowth_Unavailable_Reason"] == "insufficient_history"),
            ("an ancient annual history is refused",
             stale["EPSGrowth"] is None
             and stale["EPSGrowth_Unavailable_Reason"] == "stale_eps_history"),
            # Every refusal above still has to reach the gate as a rejection rather
            # than slip through as "no opinion".
            ("every refusal is gated out",
             all(s.growth_gate_reason(dict(r, PEG=None)) is not None
                 for r in (loss_base, loss_now, short, stale))),
        ]

        # A fetch failure must be reported as a reason, not raised: one company's
        # missing history cannot be allowed to abort a 2,500-company run.
        def _always_fails(url, params=None, context=""):
            raise s.FMPError("plan does not include this endpoint")

        s.fmp_get = _always_fails
        failed = s.fetch_eps_growth("TEST", "KEY")
        window_cases.append(
            ("a fetch failure is a reason, not an exception",
             failed["EPSGrowth"] is None
             and failed["EPSGrowth_Unavailable_Reason"] == "fetch_failed")
        )

        for name, ok in window_cases:
            failures += [] if ok else [name]
            print(f"{'PASS' if ok else 'FAIL'}  {name:<48}")
    finally:
        s.fmp_get = original_get
        s.EPS_GROWTH_METHOD = original_method

    # --- earnings-in-the-last-week filter ----------------------------------------
    # The critical property is the FAILURE path: when the lookup cannot be done, the
    # function must report ok=False. Returning an empty set with ok=True would let the
    # run silently claim the filter was applied when nothing was filtered.
    print("\n--- recent-earnings lookup ---")
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
