"""The deterministic report sections, and the VERIFIED_FIGURES block the agents read.

Two audiences, one source of numbers. `_format_screen_context` and
`_format_magic_formula_section` are written for a human reader; 
`_format_verified_figures` says the same things to a model, in the imperative, as
figures it must not contradict. They are kept in one module because they must not
drift apart — a guard added to one and not the other is how the goodwill-inclusive
ROIC came to be missing from batch runs while the reader-facing section had it.

Extracted verbatim from main.py, where this lived among the pipeline orchestration
it has nothing to do with. The bodies are unchanged — every one of these functions
is judged by the exact bytes it emits (stored in the database, injected into agent
prompts, hashed into the reuse fingerprint), so the move was verified by diffing
`tools/snapshot_report_surface.py` output before and after, not by inspection.

Names keep their leading underscore. They are private to the report layer by
intent, but `refine`, `buy_case`, `buy_case_agent`, `sale_advisory` and four test
modules call them as `main._name`, and renaming them here would have meant editing
~40 call sites in the same change that moved the code — losing the byte-identical
guarantee that makes the move checkable. `main` re-exports them under their old
names; the renaming, if it happens, is its own reviewable step.
"""
from magic_formula_starter_screener import MAX_PEG, format_money

from .figures import (
    _eps_window_phrase,
    _growth_window,
    _no_peg_reason,
    _present,
    _verified_figures,
)


def _format_screen_context(candidate: dict) -> str:
    """Turn a Magic Formula candidate row (or single-ticker computed metrics) into
    the value/quality/growth bull context that the agents weigh against the bear case."""
    rank = candidate.get("Final_Rank")
    if rank is not None:
        header = (
            "This stock was surfaced by a Magic Formula screen (Joel Greenblatt's value "
            "+ quality strategy) extended with Peter Lynch's PEG growth test. A high "
            "rank means it is statistically cheap (high earnings yield), high-quality "
            "(high return on capital), AND growing at a rate its price does not yet "
            "reflect (low PEG)."
        )
        rank_line = f"- Screen rank: #{rank} (lower is better)\n"
    else:
        header = (
            "Magic Formula value/quality metrics and the PEG growth test were computed "
            "on demand for this ticker (it was not ranked against a screen universe). A "
            "high ROC + high earnings yield + a low PEG means it is statistically cheap, "
            "high-quality, and growing."
        )
        rank_line = "- Screen rank: n/a (single-ticker run)\n"
    ctx = (
        header
        + " This is the bullish starting point that must be weighed against the bear case.\n"
        + rank_line
        + f"- Return on Capital (quality signal): {candidate.get('ROC_Pct')}\n"
        + f"- Earnings Yield (cheapness signal): {candidate.get('EY_Pct')}"
    )
    # Lynch's growth signal. Stated as the third leg of the ranking rather than as a
    # footnote, because it is the one that answers the objection the other two invite:
    # a high earnings yield on shrinking earnings is a value trap, not a bargain.
    growth_pct = _present(candidate.get("EPSGrowth_Pct"))
    peg_ratio = _present(candidate.get("PEG_Ratio"))
    if growth_pct or peg_ratio:
        ctx += (
            f"\n- Earnings-per-share growth, compound annual over the last "
            f"{_growth_window(candidate)} fiscal years "
            f"(growth signal): {growth_pct or 'Not available'}"
            f"\n- PEG ratio (P/E divided by that growth rate in percentage points; "
            f"below 1.0 is Lynch's fair-value line, and the screen requires "
            f"{MAX_PEG:g} or better): "
            f"{peg_ratio if peg_ratio is not None else 'Not available'}"
        )
    if candidate.get("MagicFormula_Score") is not None:
        ctx += f"\n- Combined Magic Formula score (ROC + earnings yield ranks): {candidate.get('MagicFormula_Score')}"
    if _present(candidate.get("Composite_Score")) is not None:
        ctx += (f"\n- Composite screen score (those two ranks plus the 1/PEG rank): "
                f"{candidate.get('Composite_Score')}")
    return ctx


def _format_verified_figures(candidate: dict, price: dict = None) -> str:
    """Human-and-model readable VERIFIED_FIGURES block for the agent prompts.

    Every number here came from the same balance sheet / income statement the
    Magic Formula ratios were computed from, so an agent contradicting this block
    is contradicting the pipeline's own basis for screening the company.

    `price` is the live quote, and is the one entry here that did NOT come from a
    filing — it is included anyway, labelled as what it is, because the alternative
    is worse. Without it each agent reaches for a price of its own from news or
    recollection, and a report ends up discussing "the stock at $210" in one section
    and "$187" in another. Optional so the callers that have no quote to hand keep
    working unchanged."""
    candidate = candidate or {}
    figs = _verified_figures(candidate)
    if not figs:
        return (
            "No independently verified figures are available for this ticker (the "
            "Magic Formula metrics could not be computed). Treat every financial "
            "figure you cite as unverified and attribute it to its source."
        )

    def _m(key):
        return format_money(figs[key]) if key in figs else "Not available"

    lines = [
        "Computed directly from the company's most recent filings by this pipeline. "
        "These override any conflicting figure from news, search, or recollection.",
        "",
        f"- Total debt: {_m('TotalDebt')}",
        f"- Cash and short-term investments: {_m('Cash')}",
        f"- Market capitalisation: {_m('LiveMarketCap')}",
        f"- Enterprise value: {_m('EnterpriseValue')}",
        f"- Total shareholders' equity: {_m('TotalEquity')}",
        f"- Total assets: {_m('TotalAssets')}",
        f"- Goodwill and acquired intangibles: {_m('GoodwillAndIntangibles')}",
        f"- Operating profit (EBIT, {candidate.get('EBIT_Basis') or 'basis not stated'}): {_m('EBIT')}",
        f"- Balance sheet as of: {candidate.get('BalanceSheetDate') or 'Not available'}",
    ]

    # The live price, kept visually apart from the filing figures because it is a
    # different KIND of fact: those are fixed until the next filing, this one changed
    # while you were reading. Agents get one price, stated once, with its timestamp.
    if price and isinstance(price.get("price"), (int, float)):
        lines += [
            "",
            f"- CURRENT SHARE PRICE: ${price['price']:,.2f} (as of "
            f"{price.get('as_of') or 'today'}). This is a LIVE market figure, not a "
            f"filing figure. Use THIS price whenever you state what the stock costs, "
            f"and quote its date beside it; do not substitute a price from a news "
            f"article or an analyst note without saying which is which. The market "
            f"capitalisation above is this price multiplied by shares outstanding, so "
            f"the two must never be quoted in a way that contradicts each other.",
        ]

    # The two return figures side by side. Reported together because quoting the
    # Greenblatt ROC alone for an acquisition-built company reliably produces the
    # claim that it is an elite-efficiency business, which the goodwill-inclusive
    # figure usually contradicts by an order of magnitude.
    roc_pct = _present(candidate.get("ROC_Pct"))
    roic_pct = _present(candidate.get("ROIC_InclGoodwill_Pct"))
    intang_pct = _present(candidate.get("IntangiblesShareOfAssets_Pct"))
    roa_pct_vf = _present(candidate.get("ROA_Pct"))
    lines += [
        "",
        "Two different return-on-capital measures — cite BOTH, never the first alone:",
        f"- Magic Formula ROC (EXCLUDES goodwill/intangibles): {roc_pct or 'Not available'}",
        f"- ROIC including goodwill (return on capital actually spent, "
        f"EBIT / (equity + debt - cash)): {roic_pct or 'Not available'}",
        f"- Goodwill and intangibles as a share of total assets: {intang_pct or 'Not available'}",
    ]
    # When the companion cannot be computed, the mandate above would otherwise leave
    # the agent with the headline ROC and nothing to weigh it against — on precisely
    # the companies (negative equity from heavy buybacks) whose headline is most
    # distorted. Substitute ROA explicitly, labelled as what it is.
    if not roic_pct:
        reason = _present(candidate.get("ROIC_Unavailable_Reason"))
        equity_raw = _present(candidate.get("TotalEquity"))
        # Same inference as the reader-facing section: the explicit reason is absent
        # on CSVs written before that column existed, but negative equity is visible.
        if reason is None and isinstance(equity_raw, (int, float)) and equity_raw < 0:
            reason = "negative_invested_capital"
        if reason == "negative_invested_capital":
            lines.append(
                "- NOTE: ROIC including goodwill CANNOT be computed for this company. It "
                "has bought back more stock than its retained earnings cover, so "
                "shareholders' equity is negative and invested capital (equity + debt - "
                "cash) is not positive — there is no denominator to divide by. This is "
                "NOT a data gap and must not be reported as one."
            )
        elif reason:
            lines.append(
                "- NOTE: ROIC including goodwill could not be computed for this company "
                "from the available balance-sheet data."
            )
        if roa_pct_vf:
            lines.append(
                f"- USE INSTEAD as the conservative counterweight — Return on Assets "
                f"(EBIT / TOTAL assets, which INCLUDES goodwill, intangibles and cash): "
                f"{roa_pct_vf}. Cite this alongside the Magic Formula ROC. Do not call it "
                f"ROIC — it is a different, blunter measure — but it serves the same "
                f"purpose here: the Magic Formula ROC excludes the purchase price of past "
                f"acquisitions, and this figure does not."
            )
    if intang_pct:
        lines.append(
            "  A high share here means the company was largely assembled by "
            "acquisition, so the Magic Formula ROC is a screening artifact of "
            "excluding the purchase price — not evidence of operating efficiency."
        )

    # --- Growth and the PEG test ---------------------------------------------
    # The screen's third leg, and the answer to the objection the first two invite:
    # a high earnings yield on shrinking earnings is a value trap, not a bargain.
    # Included here (rather than only in the reader-facing section) so an agent
    # arguing "the market has not priced in growth" has to argue against the actual
    # measured rate, and so a bear case citing a growth figure can be checked.
    pe_ratio = _present(candidate.get("PE_Ratio"))
    growth_pct = _present(candidate.get("EPSGrowth_Pct"))
    peg_ratio = _present(candidate.get("PEG_Ratio"))
    growth_years = _growth_window(candidate)
    eps_now = _present(candidate.get("EPS_Current"))
    eps_base = _present(candidate.get("EPS_Base"))
    lines += [
        "",
        "Growth and valuation (Peter Lynch's PEG test — the screen's third ranking "
        "factor alongside the two returns above):",
        f"- Price-to-earnings ratio (market capitalisation / trailing twelve-month net "
        f"income): {pe_ratio if pe_ratio is not None else 'Not available'}",
        f"- Earnings-per-share growth rate (compound annual over {growth_years} fiscal "
        f"years): {growth_pct or 'Not available'}",
        f"- PEG ratio (the P/E divided by that growth rate in PERCENTAGE POINTS, so a "
        f"P/E of 20 on 20% growth is 1.0): "
        f"{peg_ratio if peg_ratio is not None else 'Not available'}",
    ]
    if candidate.get("EPSGrowth_Capped") and _present(candidate.get("EPSGrowth_ForPEG")) is not None:
        lines.append(
            f"- NOTE: the PEG above was computed against a growth rate capped at "
            f"{round(float(candidate['EPSGrowth_ForPEG']) * 100, 2)}% a year. Measured "
            f"persistence, not opinion: companies growing faster than 50% a year went "
            f"on to a MEDIAN of -20% over the next three years, and the 95th "
            f"percentile of realised 3-year growth is 63%. The two figures above "
            f"therefore do not divide into each other — this is deliberate, not an "
            f"error, and the cap only ever makes the PEG larger."
        )
    if eps_now is not None and eps_base is not None:
        # Name the actual measure. `fetch_eps_growth` prefers diluted but falls back to
        # basic when a filer reports only that, and labelling basic figures as diluted
        # would be exactly the kind of small false claim VERIFIED_FIGURES exists to
        # stop the agents from making.
        window = _present(candidate.get("EPS_Window"))
        span = _present(candidate.get("EPS_Window_Years")) or 3
        label = (f"TOTAL {_present(candidate.get('EPS_Basis')) or 'reported'} earnings "
                 f"per share over {span} fiscal years"
                 if window == "sums"
                 else f"{_present(candidate.get('EPS_Basis')) or 'reported'} earnings "
                      f"per share")
        lines.append(
            f"- The growth rate was computed from {label}: {eps_base} for the window "
            f"ending {_present(candidate.get('EPS_Base_Date')) or 'not stated'}, "
            f"against {eps_now} for the window ending "
            f"{_present(candidate.get('EPS_Current_Date')) or 'not stated'}."
        )
        if window == "sums":
            lines.append(
                "  Measured as multi-year TOTALS rather than two endpoint years, so a "
                "loss year inside the window is counted at its real negative value "
                "and a one-off spike is diluted rather than annualised. Do not "
                "recompute this rate from two single-year EPS figures and report a "
                "different answer."
            )
    base_margin = _present(candidate.get("BaseNetMargin_Pct"))
    if base_margin:
        lines.append(
            f"- Net profit margin over the BASE window: {base_margin}. This is the "
            f"check that the starting point was a real trading period rather than a "
            f"breakeven one — a near-zero base turns any recovery into a three-digit "
            f"growth rate that says nothing about the business."
        )
    if peg_ratio is None:
        # Say what is missing and why, rather than leaving an agent to fill the gap
        # with a PEG it recalled or invented — the failure mode this whole block exists
        # to prevent. The cause is a fact about the company far more often than it is
        # a data outage, and a shrinking-earnings cause is itself bear evidence.
        lines.append(
            f"- NOTE: a PEG ratio could NOT be computed for this company, because "
            f"{_no_peg_reason(candidate)}. Do not substitute a PEG from any other "
            f"source, and do not treat its absence as evidence either way about "
            f"growth — say plainly that the screen's growth test could not be applied "
            f"here, and why."
        )
    return "\n".join(lines)


def _format_magic_formula_section(candidate: dict) -> str:
    """Deterministic '## Magic Formula Metrics' section, injected into every
    final report regardless of what the LLM chooses to restate. Built directly
    from the screener/on-demand candidate data (not the LLM), so Earnings
    Yield, ROC, their basis (TTM vs Annual fallback), and the underlying
    formula/components are always present and accurate. Every field is always
    present -- falling back to 'Not available' rather than being omitted -- and
    when the ratios could not be computed the section states the plain-English
    reason (e.g. the company is loss-making) instead of leaving the reader
    guessing whether it was a data glitch or a fact about the business."""
    candidate = candidate or {}
    rank = candidate.get("Final_Rank")
    rank_str = f"#{rank}" if rank not in (None, "") else "Not available (single-ticker run)"

    ebit_raw = candidate.get("EBIT")
    ebit = format_money(ebit_raw)
    capital_employed = format_money(candidate.get("CapitalEmployed"))
    enterprise_value = format_money(candidate.get("EnterpriseValue"))
    market_cap = format_money(candidate.get("LiveMarketCap"))
    basis = candidate.get("EBIT_Basis") or "Not available"

    ey_pct = candidate.get("EY_Pct")
    roc_pct = candidate.get("ROC_Pct")

    lines = [
        "## Magic Formula Metrics",
        "",
        "These are the numbers this screen ranks companies on. The first two are Joel "
        "Greenblatt's Magic Formula: one asks whether the business is cheap, the other "
        "whether it is any good. The third is Peter Lynch's PEG ratio, which asks "
        "whether it is growing — because the first two compare profit to price at a "
        "single moment, and cannot tell a steady business apart from one whose profits "
        "are on the way down. The workings are shown so you can check them yourself.",
        "",
        f"- **Earnings Yield (is it cheap?):** {ey_pct or 'Not available'}",
        f"  - How it is worked out: operating profit ({ebit}) divided by enterprise "
        f"value ({enterprise_value}).",
        "  - In plain English: what the company earns from its actual business, compared "
        "with what it would really cost to buy the whole company outright. A higher "
        "percentage means you pay less for each dollar of profit.",
        f"- **Return on Capital (is it a good business?):** {roc_pct or 'Not available'}",
        f"  - How it is worked out: operating profit ({ebit}) divided by capital employed "
        f"({capital_employed}).",
        "  - In plain English: how much profit the business squeezes out of the money "
        "tied up in running it. A higher percentage means the company turns its "
        "resources into profit more efficiently than rivals.",
        "  - Important limit: this figure deliberately leaves out what the company PAID "
        "to buy other businesses. That is the right choice for ranking companies against "
        "each other, but it can make a company built by takeovers look extraordinarily "
        "efficient. The companion figure below shows the return on the money actually "
        "spent.",
        "",
    ]
    # Companion ROIC. Shown whenever it could be computed, because the gap between
    # the two figures is the single most misread number in these reports: a very
    # large Magic Formula ROC next to a modest goodwill-inclusive ROIC means the
    # company bought its earnings rather than generating them from a small asset base.
    roic_pct = _present(candidate.get("ROIC_InclGoodwill_Pct"))
    intangibles_pct = _present(candidate.get("IntangiblesShareOfAssets_Pct"))
    invested_capital = format_money(candidate.get("InvestedCapital"))
    if roic_pct:
        lines += [
            f"- **Return on capital actually spent (including takeover costs):** {roic_pct}",
            f"  - How it is worked out: operating profit ({ebit}) divided by all the money "
            f"invested in the company ({invested_capital} — shareholders' money plus "
            f"borrowings, less spare cash).",
            "  - In plain English: what the business earns on every dollar ever put into "
            "it, including the price paid for acquisitions. This is the more conservative "
            "of the two, and for a company assembled by takeover it is the more honest "
            "guide to how good the business really is.",
        ]
        if intangibles_pct:
            lines.append(
                f"  - Goodwill and acquired intangibles are {intangibles_pct} of this "
                f"company's total assets. The higher that share, the wider the gap "
                f"between the two figures above, and the more weight the second deserves."
            )
        lines.append("")
    else:
        # The companion could not be computed. Say so, say WHY, and put a conservative
        # figure in its place — otherwise the reader is left with a headline return in
        # the hundreds or thousands of percent and nothing to weigh it against, on
        # exactly the companies where that headline is least trustworthy.
        reason = _present(candidate.get("ROIC_Unavailable_Reason"))
        roa_for_fallback = _present(candidate.get("ROA_Pct"))
        equity_raw = _present(candidate.get("TotalEquity"))
        total_equity_fmt = format_money(equity_raw)

        # The paragraph above ends by promising "the companion figure below". That
        # promise must be answered EVERY time, even when we cannot say why the figure
        # is missing — otherwise the section reads as though a number were simply
        # dropped, and the substitute below becomes an "instead" with no antecedent.
        lines.append(
            "- **Return on capital actually spent (including takeover costs):** "
            "cannot be calculated for this company."
        )
        # Infer the cause when the explicit reason is absent — it is missing for CSVs
        # written before that column existed, and negative equity is visible from the
        # figures we already carry. Better to explain from evidence than stay silent.
        if reason is None and isinstance(equity_raw, (int, float)) and equity_raw < 0:
            reason = "negative_invested_capital"

        if reason == "negative_invested_capital":
            lines += [
                f"  - Why: the company has returned more money to shareholders — through "
                f"share buybacks — than it has accumulated in retained profits, so its "
                f"balance-sheet equity is negative ({total_equity_fmt}). Once you add its "
                f"debts and subtract its cash, the total capital invested in it comes out "
                f"at or below zero, and you cannot work out a return on a negative amount.",
                "  - This is a fact about how the company is financed, not a gap in the "
                "data. It is common in mature businesses that buy back stock heavily.",
            ]
        else:
            lines.append(
                "  - Why: the money invested in this company (shareholders' funds plus "
                "borrowings, less spare cash) does not come out as a positive figure on "
                "the latest balance sheet, so there is nothing to divide the profit by. "
                "This is a feature of how the company is financed, not a missing number."
            )
        if roa_for_fallback:
            lines += [
                f"  - **Use this instead as the conservative check: return on assets of "
                f"{roa_for_fallback}** — operating profit measured against *everything* "
                f"the company owns, including spare cash and the goodwill from past "
                f"takeovers. It is a blunter measure than the one above and is not the "
                f"same thing, but it serves the same purpose: the headline return on "
                f"capital leaves those items out, and this figure does not. Where the two "
                f"are far apart, trust this one.",
            ]
            if intangibles_pct:
                lines.append(
                    f"  - Goodwill and acquired intangibles are {intangibles_pct} of this "
                    f"company's total assets."
                )
        lines.append("")
    # --- Lynch's growth test -------------------------------------------------
    # Rendered as a first-class ranking figure, next to ROC and earnings yield,
    # because it now IS one: survivors are ordered on 1/PEG alongside the other two.
    # Written out in full (rate, ratio, and the two EPS figures behind them) because
    # the PEG is the one ratio here whose UNITS are routinely got wrong — a reader who
    # cannot see that the denominator is "24.8", not "0.248", has no way to check it.
    growth_pct = _present(candidate.get("EPSGrowth_Pct"))
    peg_ratio = _present(candidate.get("PEG_Ratio"))
    growth_years = _growth_window(candidate)
    eps_now = _present(candidate.get("EPS_Current"))
    eps_base = _present(candidate.get("EPS_Base"))
    pe_for_peg = _present(candidate.get("PE_Ratio"))
    lines += [
        f"- **Profit growth (is it getting bigger?):** {growth_pct or 'Not available'} "
        f"a year",
        f"  - How it is worked out: {_eps_window_phrase(candidate)} — the "
        f"steady yearly rate that would take you from the first figure to the second "
        f"over {growth_years} years."
        # Only said when it applies. A standing "a minus sign means..." legend beside
        # a healthy positive rate is noise; beside a negative one it is the point.
        + (" The rate is negative, which means profit per share FELL over the period."
           if isinstance(_present(candidate.get("EPSGrowth")), (int, float))
           and candidate["EPSGrowth"] < 0 else ""),
        "  - In plain English: how fast the profit belonging to each share you own has "
        "actually grown. Per share matters: a company can grow its total profit while "
        "issuing so many new shares that your slice does not grow at all.",
        f"- **PEG ratio (is the growth already in the price?):** "
        f"{peg_ratio if peg_ratio is not None else 'Not available'}",
        f"  - How it is worked out: the price-to-earnings ratio "
        f"({pe_for_peg if pe_for_peg is not None else 'not available'}) divided by the "
        f"growth rate above written as a plain number — so 20% growth becomes 20, not "
        f"0.20.",
    ]
    # When the sustainability cap bit, the two figures above no longer divide into
    # each other and a reader checking the arithmetic would think one of them wrong.
    # Say what was actually divided by, and why, rather than quietly publishing a sum
    # that does not add up.
    if candidate.get("EPSGrowth_Capped") and _present(candidate.get("EPSGrowth_ForPEG")) is not None:
        capped_at = round(float(candidate["EPSGrowth_ForPEG"]) * 100, 2)
        lines.append(
            f"  - Note: the growth rate used for this calculation was held down to "
            f"{capped_at}% a year, rather than the higher figure above. Very fast "
            f"growth almost never continues: of the companies in our sample that had "
            f"been growing faster than 50% a year, nearly three quarters went on to "
            f"SHRINKING profits over the following three years, and only about one "
            f"company in twenty ever sustains more than 63%. Paying for growth above "
            f"that rate means paying for something that usually does not arrive. "
            f"Holding the figure down makes this ratio less flattering, never more."
        )
    lines += [
        "  - In plain English: Peter Lynch's test of whether you are paying a fair "
        "price for growth. At 1.0 the price you pay for each dollar of profit is "
        "exactly matched by how fast that profit is rising; below 1.0 you are getting "
        f"the growth cheaply; above it you are paying up front for growth that has not "
        f"happened yet. This screen only lets a company through at {MAX_PEG:g} or lower.",
        "  - Its limit: it takes the last few years' growth as a guide to the future, "
        "which is exactly what fails when a business turns. Treat it as one reading "
        "among several, not a forecast.",
    ]
    if peg_ratio is None:
        lines.append(f"  - Why this is missing: {_no_peg_reason(candidate)}.")
    lines.append("")

    # --- Greenblatt's step-by-step gate figures ---
    # These are NOT the ranking metrics, and the section says so, because "Return on
    # Assets" and "Return on Capital" are the single easiest pair to conflate in these
    # reports: same operating profit on top, very different denominators underneath.
    # `_present` matters here specifically: in --from-csv mode these arrive via
    # pandas, so an absent value is NaN, and NaN is truthy — a bare `if roa_pct`
    # would happily print "Return on assets: nan".
    roa_pct = _present(candidate.get("ROA_Pct"))
    roa_ni_pct = _present(candidate.get("ROA_NetIncome_Pct"))
    pe_ratio = _present(candidate.get("PE_Ratio"))
    if roa_pct or pe_ratio:
        lines += [
            "### The screening hurdles this company had to clear",
            "",
            "Greenblatt's step-by-step instructions for building the screen by hand use "
            "two rougher measures as entry conditions, because they are what an ordinary "
            "stock screener will give you. They decide who gets INTO the list; the two "
            "ratios above decide the order.",
            "",
        ]
        if roa_pct:
            lines.append(f"- **Return on assets:** {roa_pct} (operating profit divided by "
                         f"every asset the company owns).")
            lines.append(
                "  - Not the same thing as return on capital above. Both start from the "
                "same operating profit, but return on capital divides by only the money "
                "actually tied up in trading — equipment and working funds — while return "
                "on assets divides by EVERYTHING on the books, including spare cash and "
                "the goodwill left over from past takeovers. So return on assets is always "
                "the lower and blunter of the two, and it is the one Greenblatt's manual "
                "screen filters on purely because a free screener will show it to you. "
                "The hurdle is deliberately high — 25% — to make up for how rough it is."
            )
            if roa_ni_pct:
                lines.append(
                    f"  - On the stricter after-tax measure (bottom-line profit divided by "
                    f"total assets) the same company scores {roa_ni_pct}."
                )
        if pe_ratio:
            lines.append(
                f"- **Price-to-earnings ratio:** {pe_ratio} (the company's market value "
                f"divided by its bottom-line profit)."
            )
            lines.append(
                "  - The screen throws out anything below 5. That sounds backwards — a low "
                "price-to-earnings ratio is supposed to mean cheap — but a ratio that low "
                "almost always means a one-off event flattered the profit figure, such as "
                "selling a division or winning a lawsuit. That profit will not repeat, so "
                "the bargain is an illusion."
            )
        # The growth hurdle is listed with the others because it is applied the same
        # way — as an entry condition — even though the same figure ALSO feeds the
        # ranking above. Saying so here keeps a reader from thinking the PEG shown
        # earlier was decorative.
        if peg_ratio is not None or growth_pct:
            lines.append(
                "- **Growth and the PEG ratio:** this screen adds a hurdle of its own, "
                "which is not part of Greenblatt's original method. A company must be "
                f"growing its profit per share at all over the last few years, that "
                f"growth must have started from a genuinely profitable period rather "
                f"than a break-even one, and its PEG ratio must be {MAX_PEG:g} or "
                f"lower. The reason is that the two ratios above "
                "are snapshots — this year's profit set against today's price — so they "
                "cannot tell a company earning steadily apart from one whose profits are "
                "falling. Both can show the same attractive percentage. Worse, a "
                "shrinking company often keeps a tempting-looking figure, because once "
                "the market expects further decline it marks the share price down faster "
                "than the profits have actually fallen. What you are buying is a "
                "shrinking stream of profit at an apparent discount, and the discount "
                "disappears along with the profits. The same PEG figure is "
                "then used again in the ranking, so a cheap, well-run company that is "
                "also growing quickly ranks above one that is merely cheap and well-run."
            )
        lines.append("")

    lines += [
        "### The numbers behind the ratios",
        f"- Operating profit (EBIT — earnings before interest and taxes; profit from the "
        f"core business before loan interest and tax): {ebit}",
        f"- Enterprise value (the company's market price plus its debts, minus its spare "
        f"cash — the true cost of buying it outright): {enterprise_value}",
        f"- Capital employed (the money tied up running the business: equipment and "
        f"buildings, plus day-to-day working funds): {capital_employed}",
        f"- Market value of all shares (market capitalisation): {market_cap}",
        # Debt and cash are printed here as the reader-facing counterpart to the
        # VERIFIED_FIGURES block the agents were given, so a figure quoted in the
        # bull or bear case can be checked against the filings without leaving the page.
        f"- Total debt owed (read directly from the filings): "
        f"{format_money(candidate.get('TotalDebt'))}",
        f"- Cash and short-term investments: {format_money(candidate.get('Cash'))}",
        f"- Balance sheet date these figures come from: "
        f"{candidate.get('BalanceSheetDate') or 'Not available'}",
        f"- Reporting period used (basis): {basis} "
        f"(TTM = the last four quarters combined; Annual = the most recent full-year "
        f"report, used only when quarterly figures are unavailable)",
        f"- Magic Formula rank: {rank_str}",
    ]

    # When the ratios are missing, say why — this is usually a meaningful fact
    # about the company (loss-making, no capital employed), not a data outage.
    message = candidate.get("message")
    if not (ey_pct and roc_pct):
        lines += [
            "",
            "### Why these figures are unavailable",
            message or (
                "The Magic Formula ratios could not be calculated for this company from "
                "the available financial data."
            ),
            "",
            "This does not by itself make the company a bad investment — it means this "
            "particular value-and-quality screen cannot score it, so weigh the bull and "
            "bear cases below on their own merits.",
        ]

    return "\n".join(lines) + "\n"
