"""Backward-looking company fundamentals from Financial Modeling Prep.

The profile, the annual ratio and key-metric payloads, and the quarterly trend
series that answers "is this still true?". Everything here describes periods the
company has already reported; the forward-looking feeds live in forward.py.

The field whitelists are the interesting part and they are deliberately generous —
see the commentary on each.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import json
import os
import time

import requests

from magic_formula_starter_screener import FMPError, fmp_get
from mcp_app import logger, mcp


# ==========================================
# TOOL 3: FMP Metrics Extractor
# ==========================================
# Fields kept from FMP's ratio/key-metric payloads. The whitelists are deliberately
# generous — everything the research instructions ask for (revenue growth, gross and
# operating margin, free cash flow, total debt, P/E vs its 5-year average, leverage,
# returns, valuation multiples) plus the usual supporting ratios. What is dropped is
# working-capital-cycle exotica (days-payable, cash conversion cycle, Graham net-net,
# turnover ratios) that no prompt references and no report has ever cited.
_RATIO_FIELDS = {
    "date", "fiscalYear", "period",
    # margins
    "grossProfitMargin", "operatingProfitMargin", "ebitMargin", "ebitdaMargin",
    "netProfitMargin", "pretaxProfitMargin",
    # valuation
    "priceToEarningsRatio", "priceToBookRatio", "priceToSalesRatio",
    "priceToFreeCashFlowRatio", "priceToOperatingCashFlowRatio",
    "enterpriseValueMultiple", "priceToEarningsGrowthRatio",
    # leverage & liquidity
    "debtToEquityRatio", "debtToAssetsRatio", "debtToCapitalRatio",
    "longTermDebtToCapitalRatio", "interestCoverageRatio", "financialLeverageRatio",
    "currentRatio", "quickRatio", "cashRatio", "solvencyRatio",
    # per-share & payout
    "revenuePerShare", "netIncomePerShare", "freeCashFlowPerShare",
    "operatingCashFlowPerShare", "bookValuePerShare",
    "dividendYieldPercentage", "dividendPayoutRatio",
    # efficiency
    "assetTurnover", "effectiveTaxRate",
}


_KEY_METRIC_FIELDS = {
    "date", "fiscalYear", "period",
    "marketCap", "enterpriseValue", "investedCapital", "workingCapital",
    "earningsYield", "freeCashFlowYield",
    "evToEBITDA", "evToSales", "evToFreeCashFlow", "evToOperatingCashFlow",
    "returnOnEquity", "returnOnAssets", "returnOnInvestedCapital",
    "returnOnCapitalEmployed", "returnOnTangibleAssets", "operatingReturnOnAssets",
    "netDebtToEBITDA", "currentRatio", "incomeQuality", "intangiblesToTotalAssets",
    "capexToRevenue", "capexToOperatingCashFlow", "capexToDepreciation",
    "researchAndDevelopementToRevenue", "stockBasedCompensationToRevenue",
    "salesGeneralAndAdministrativeToRevenue",
    "freeCashFlowToEquity", "freeCashFlowToFirm", "taxBurden", "interestBurden",
}


def _prune_rows(rows, keep):
    """Keep only whitelisted keys, and drop keys whose value is None — a null adds
    payload without adding information."""
    if not isinstance(rows, list):
        return rows
    return [{k: v for k, v in row.items() if k in keep and v is not None}
            for row in rows if isinstance(row, dict)]


@mcp.tool()
def fmp_company_profile(ticker: str) -> str:
    """
    Look up what a company ACTUALLY DOES: its sector, industry, and a description of
    its business, from Financial Modeling Prep.

    Use this to VERIFY a candidate competitor before naming it as one. A company is
    only a competitor if it sells something that competes for the same customer spend
    as the subject's largest revenue segment — being the same size, or sitting in the
    same broad sector, is not enough.

    Exists because a bear case once named Frontdoor (FTDR) as a peer of H&R Block on
    the strength of a provider peer list. One call to this tool returns "provider of
    extensive home service plans… repair or replacement of key components", against a
    tax preparer — which settles it in a sentence.

    Returns JSON with symbol, companyName, sector, industry, description, country,
    marketCap and isActivelyTrading, or {"error": ...}.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return json.dumps({"error": "FMP_API_KEY not configured"})
    try:
        time.sleep(0.20)  # 300 calls/min Starter rate limit
        data = fmp_get(
            "https://financialmodelingprep.com/stable/profile",
            params={"symbol": ticker, "apikey": api_key},
            context=f"{ticker} profile",
        )
        row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict) or not row.get("symbol"):
            return json.dumps({"error": f"No profile found for {ticker}"})
        keep = ("symbol", "companyName", "sector", "industry", "country",
                "marketCap", "isActivelyTrading", "description")
        return json.dumps({k: row.get(k) for k in keep}, separators=(",", ":"))
    except FMPError as e:
        return json.dumps({"error": f"Could not fetch profile for {ticker}: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def fmp_metrics_extractor(ticker: str) -> str:
    """
    Fetches 3-year metric trends, 5-year P/E history/average, analyst consensus
    targets, and an UNVERIFIED peer cohort from Financial Modeling Prep.

    The peer cohort is deliberately not called "competitors" — see the comment at
    the `stock-peers` call below for why that label was actively misleading.

    Uses the current /stable API (the legacy /api/v3 and /api/v4 endpoints were
    retired 2025-08-31). All endpoints below are available on the Starter plan.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return "FMP_API_KEY not configured."

    base = "https://financialmodelingprep.com/stable"

    def _get(path, **params):
        params["apikey"] = api_key
        time.sleep(0.20)  # 300 calls/min Starter rate limit
        resp = requests.get(f"{base}/{path}", params=params, timeout=20)
        try:
            return resp.json()
        except Exception:
            return {"error": f"HTTP {resp.status_code}", "body": resp.text[:200]}

    try:
        key_metrics_3y = _get("key-metrics", symbol=ticker, limit=3)   # 3-year trailing metrics
        ratios_5y = _get("ratios", symbol=ticker, limit=5)             # 5-year ratios incl. P/E
        # FMP returns 64 ratio fields x 5 years and 47 key-metric fields x 3 years.
        # The research instructions reference a small fraction of them; the rest is
        # payload that every agent pays for on every turn. Pruning to a generous
        # whitelist is lossless for the questions actually asked (see _RATIO_FIELDS).
        key_metrics_3y = _prune_rows(key_metrics_3y, _KEY_METRIC_FIELDS)
        ratios_5y = _prune_rows(ratios_5y, _RATIO_FIELDS)
        ratings_snapshot = _get("ratings-snapshot", symbol=ticker)     # replaces legacy /v3/rating
        price_target = _get("price-target-consensus", symbol=ticker)   # analyst consensus targets
        grades = _get("grades-consensus", symbol=ticker)               # analyst buy/hold/sell consensus
        # NOT a competitor list, despite the endpoint name. FMP returns a size-and-
        # sector bucket: for HRB (tax preparation) it returns Allison Transmission
        # (truck gearboxes), Boyd Gaming (casinos), Churchill Downs (horse racing) and
        # Frontdoor (home warranties), while OMITTING Intuit — the one company that is
        # actually its main competitor. Labelling this "competitors" caused a bear case
        # to reach for Frontdoor as a comparable, which it did while visibly doubting
        # the premise. The field is kept because a size/sector cohort has some value,
        # but it now travels with a name and a note that say what it really is.
        peers = _get("stock-peers", symbol=ticker)

        # Compute the 5-year average P/E from the ratios history.
        pe_values = [
            row.get("priceToEarningsRatio")
            for row in (ratios_5y if isinstance(ratios_5y, list) else [])
            if isinstance(row.get("priceToEarningsRatio"), (int, float))
        ]
        pe_5y_average = round(sum(pe_values) / len(pe_values), 2) if pe_values else None

        result = {
            # NOTE: key_metrics and ratios below are ANNUAL periods. They describe
            # multi-year trend and valuation history only — they say nothing about
            # the most recent quarter. Recent-quarter deterioration is covered by
            # fmp_quarterly_trends, which is a separate, mandatory input.
            "period_basis": "ANNUAL (fiscal years) — see quarterly_data for recent quarters",
            "key_metrics_3y": key_metrics_3y,
            "ratios_5y": ratios_5y,
            "pe_5y_average": pe_5y_average,
            "ratings_snapshot": ratings_snapshot,
            "price_target_consensus": price_target,
            "grades_consensus": grades,
            # Renamed from "competitors" — see the comment at the fetch site. The
            # caveat rides INSIDE the payload rather than only in a prompt, so it
            # reaches every agent that reads this blob regardless of instructions.
            "peer_group_note": (
                "UNVERIFIED. This list comes from the data provider's peer endpoint, "
                "which groups by market size and broad sector, NOT by business model. "
                "It regularly contains companies in unrelated industries and regularly "
                "OMITS the subject's real competitors, including its largest one. Do "
                "not describe any of these as a competitor, and do not compare margins "
                "or multiples against them, unless you have separately established "
                "that it competes in the same business. Real competitors are often "
                "private and will not appear here at all — say so rather than "
                "substituting whatever this list contains."
            ),
            "fmp_peer_group_unverified": peers,
        }
        # Compact separators, not indent=2: the pretty-printing was pure whitespace
        # billed as input tokens on every agent turn.
        return json.dumps(result, separators=(",", ":"))
    except Exception as e:
        return f"Error fetching FMP metrics for {ticker}: {str(e)}"


# ==========================================
# TOOL 3b: FMP Quarterly Trends (recency check)
# ==========================================
def _pick(row: dict, *keys):
    """First non-None value among `keys` — FMP renames fields between endpoints."""
    for k in keys:
        v = row.get(k)
        if v is not None:
            return v
    return None


def _yoy_pct(current, prior):
    """Year-over-year % change, guarding division by zero and sign flips.
    Returns None when a comparison would be meaningless (missing or zero base)."""
    if current is None or prior is None or not prior:
        return None
    return round(100.0 * (current - prior) / abs(prior), 1)


@mcp.tool()
def fmp_quarterly_trends(ticker: str) -> str:
    """
    Fetches the last 8 QUARTERS of income statement, cash flow, and balance sheet
    for a company, and builds an explicit year-over-year comparison of each of the
    last 4 quarters against the same quarter a year earlier (Q vs Q-4).

    This exists because the annual metrics in fmp_metrics_extractor cannot show
    recent deterioration: a company whose last full fiscal year looked fine can be
    contracting sharply in its two most recent quarters. Use this to check whether
    the most recent quarter CONFIRMS or CONTRADICTS the multi-year trend.

    Returns JSON with `quarters` (raw per-quarter figures, newest first) and
    `yoy_comparison` (same-quarter-last-year deltas for revenue, operating income,
    net income, operating cash flow, capex, free cash flow, cash, and total debt).
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return "FMP_API_KEY not configured."

    base = "https://financialmodelingprep.com/stable"

    def _get(path):
        time.sleep(0.20)  # 300 calls/min Starter rate limit
        try:
            resp = requests.get(
                f"{base}/{path}",
                params={"symbol": ticker, "period": "quarter", "limit": 8, "apikey": api_key},
                timeout=20,
            )
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"[{ticker}] quarterly fetch failed for {path}: {e}")
            return []

    inc = _get("income-statement")
    cfs = _get("cash-flow-statement")
    bal = _get("balance-sheet-statement")

    if not inc:
        return (f"No quarterly income statement available for {ticker}. "
                "Treat the absence of quarterly data as a gap in the evidence, "
                "not as confirmation that recent quarters were fine.")

    # Index the cash-flow and balance-sheet rows by period end date so a missing or
    # out-of-order statement never silently misaligns a quarter against another.
    cfs_by_date = {r.get("date"): r for r in cfs if r.get("date")}
    bal_by_date = {r.get("date"): r for r in bal if r.get("date")}

    quarters = []
    for row in inc[:8]:
        date = row.get("date")
        c = cfs_by_date.get(date, {})
        b = bal_by_date.get(date, {})
        capex = _pick(c, "capitalExpenditure")
        quarters.append({
            "date": date,
            "period": row.get("period"),
            "fiscalYear": row.get("fiscalYear"),
            "revenue": row.get("revenue"),
            "grossProfit": row.get("grossProfit"),
            "operatingIncome": row.get("operatingIncome"),
            "netIncome": row.get("netIncome"),
            "interestExpense": row.get("interestExpense"),
            "operatingCashFlow": _pick(c, "operatingCashFlow", "netCashProvidedByOperatingActivities"),
            # FMP reports capex as a negative number; normalise to a positive spend
            # figure so "capex rose" reads the same direction as the source filings.
            "capitalExpenditure": abs(capex) if isinstance(capex, (int, float)) else None,
            "freeCashFlow": _pick(c, "freeCashFlow"),
            "cashAndShortTermInvestments": b.get("cashAndShortTermInvestments"),
            "totalDebt": b.get("totalDebt"),
            "totalStockholdersEquity": b.get("totalStockholdersEquity"),
        })

    # Same-quarter-last-year comparison. Comparing Q1-26 to Q4-25 would confuse
    # seasonality with deterioration, so every delta is Q vs the quarter 4 back.
    #
    # Rendered as markdown tables rather than nested JSON. The JSON form repeated
    # each field name once per quarter and, worse, wrote every value TWICE — once
    # in `quarters` and again inside `yoy_comparison` as current/year_ago. Tables
    # name each field once and the YoY block carries only the deltas, which is a
    # ~75% payload reduction for identical information across four agents.
    metrics = [
        ("revenue", "Revenue"),
        ("grossProfit", "Gross profit"),
        ("operatingIncome", "Operating income"),
        ("netIncome", "Net income"),
        ("operatingCashFlow", "Operating cash flow"),
        ("capitalExpenditure", "Capital expenditure"),
        ("freeCashFlow", "Free cash flow"),
        ("cashAndShortTermInvestments", "Cash & ST investments"),
        ("totalDebt", "Total debt"),
        ("interestExpense", "Interest expense"),
    ]

    def _m(v):
        if v is None:
            return "n/a"
        return f"{v / 1e6:,.0f}"

    lines = [
        f"QUARTERLY DATA — {ticker}. All amounts in USD millions. "
        f"Most recent quarter: {quarters[0]['date']}.",
        "",
        "## Quarterly figures (newest first)",
        "",
        "| Metric | " + " | ".join(q["date"] for q in quarters) + " |",
        "| :--- | " + " | ".join("---:" for _ in quarters) + " |",
    ]
    for key, label in metrics:
        lines.append(f"| {label} | " + " | ".join(_m(q.get(key)) for q in quarters) + " |")

    n_yoy = min(4, max(0, len(quarters) - 4))
    if n_yoy:
        pairs = [(quarters[i], quarters[i + 4]) for i in range(n_yoy)]
        lines += [
            "",
            "## Year-over-year change (%), each quarter vs the SAME quarter one year earlier",
            "",
            "| Metric | " + " | ".join(f"{c['date']} vs {p['date']}" for c, p in pairs) + " |",
            "| :--- | " + " | ".join("---:" for _ in pairs) + " |",
        ]
        for key, label in metrics:
            cells = []
            for cur, prior in pairs:
                pct = _yoy_pct(cur.get(key), prior.get(key))
                cells.append("n/a" if pct is None else f"{pct:+.1f}%")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "These percentages are already seasonally aligned (same quarter, one year "
        "apart). A negative figure on revenue, operating income, or operating cash "
        "flow in the most recent quarter is recent deterioration and must be "
        "reported even when the multi-year annual trend looks healthy.",
    ]
    return "\n".join(lines)
