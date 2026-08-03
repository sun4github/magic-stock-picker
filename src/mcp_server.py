import os
import time
import json
import logging
import requests
import asyncio
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional

from mcp.server.fastmcp import FastMCP
import psycopg2
from psycopg2.extras import Json
import numpy as np

# Load Env
load_dotenv()

# Initialize FastMCP Server
mcp = FastMCP("MagicFormulaSkepticalDecomposer")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_server")

# ==========================================
# TOOL 1: Magic Formula Screener
# ==========================================
from magic_formula_starter_screener import (
    main as run_screener_main,
    OUTPUT_FILENAME,
    compute_company_metrics_detailed,
    attach_growth_metrics,
    fmp_get,
    FMPError,
)

@mcp.tool()
def run_magic_formula_screener() -> str:
    """
    Executes the magic formula screener tool and outputs the top 30 ranked companies.
    Returns JSON array of the top 30 candidates.
    """
    logger.info("Executing Magic Formula Screener...")
    # Run the screener which writes to CSV
    run_screener_main()

    # Read the CSV
    try:
        df = pd.read_csv(OUTPUT_FILENAME)
        # Top 30
        top_30 = df.head(30).to_dict(orient="records")
        return json.dumps(top_30, indent=2)
    except Exception as e:
        logger.error(f"Error reading screener output: {e}")
        return json.dumps([])


@mcp.tool()
def compute_ticker_magic_metrics(ticker: str) -> str:
    """
    Computes Magic Formula Return on Capital (ROC) and Earnings Yield for a SINGLE
    ticker, reusing the screener's own math. Used for on-demand runs where the full
    screener is not executed, so the bull-case agent still gets the value/quality
    signal. Returns a candidate-shaped JSON object (or {"error": ...}).
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return json.dumps({"error": "FMP_API_KEY not configured"})
    try:
        # /stable/batch-quote is restricted on the Starter plan; use the
        # market-capitalization endpoint (available on Starter) for live market cap.
        # Routed through fmp_get for timeout + retry + clear errors.
        time.sleep(0.20)
        mc = fmp_get(
            "https://financialmodelingprep.com/stable/market-capitalization",
            params={"symbol": ticker, "apikey": api_key},
            context=f"{ticker} market-cap",
        )
        live_cap = mc[0].get("marketCap", 0) if isinstance(mc, list) and mc else 0
        metrics = compute_company_metrics_detailed(ticker, live_cap, api_key)
        if not metrics.get("ok"):
            # Not a failure to look up — usually a real, reportable fact about the
            # company (e.g. it is losing money, so the ratios are undefined). Pass
            # the plain-English reason through so the final report can explain the
            # gap instead of showing a bare 'Not available'.
            return json.dumps({
                "error": f"Could not compute ROC/Earnings Yield for {ticker}",
                "reason": metrics.get("reason"),
                "message": metrics.get("message"),
                "EBIT": metrics.get("EBIT"),
                "EBIT_Basis": metrics.get("EBIT_Basis"),
                "CapitalEmployed": metrics.get("CapitalEmployed"),
                "LiveMarketCap": live_cap,
            })
        # Lynch's growth figures. Costs one extra request, spent unconditionally here
        # (the screener spends it only on gate survivors) because an on-demand run is
        # asking about ONE named company and the PEG is part of the answer.
        attach_growth_metrics(ticker, metrics, api_key)

        roic = metrics.get("ROIC_InclGoodwill")
        intang_share = metrics.get("IntangiblesShareOfAssets")
        roa = metrics.get("ROA")
        roa_ni = metrics.get("ROA_NetIncome")
        pe = metrics.get("PE")
        growth = metrics.get("EPSGrowth")
        peg = metrics.get("PEG")
        return json.dumps({
            "Symbol": ticker,
            "CompanyName": metrics.get("CompanyName", ticker),
            "ROC_Pct": f"{round(metrics['ROC'] * 100, 2)}%",
            "EY_Pct": f"{round(metrics['EarningsYield'] * 100, 2)}%",
            # Greenblatt's step-by-step gate figures. Reported, never enforced here:
            # an on-demand run is asking about ONE named company, so refusing to
            # compute its ratios because the screen would have rejected it would
            # answer a question nobody asked. The report says which side of the
            # 25% / P/E-5 cuts it falls on and lets the reader weigh that.
            "ROA_Pct": f"{round(roa * 100, 2)}%" if roa is not None else None,
            "ROA_NetIncome_Pct": f"{round(roa_ni * 100, 2)}%" if roa_ni is not None else None,
            "PE_Ratio": round(pe, 2) if pe is not None else None,
            # Lynch's PEG test. Reported, never enforced here, for the same reason as
            # the two gate figures above: the report says which side of the "PEG at or
            # below 1.2, and growing at all" cuts this company falls on. Both the raw
            # ratio and the display string go out, because the batch and single-ticker
            # candidate shapes must agree (agent_architecture.md §2.I).
            "EPSGrowth": growth,
            "EPSGrowth_Pct": f"{round(growth * 100, 2)}%" if growth is not None else None,
            # What the PEG was actually divided by: the same rate unless the
            # sustainability cap bit (see MAX_GROWTH_FOR_PEG in the screener).
            "EPSGrowth_ForPEG": metrics.get("EPSGrowth_ForPEG"),
            "EPSGrowth_Capped": metrics.get("EPSGrowth_Capped"),
            "PEG": peg,
            "PEG_Ratio": round(peg, 2) if peg is not None else None,
            "EPS_Current": metrics.get("EPS_Current"),
            "EPS_Base": metrics.get("EPS_Base"),
            "EPS_Current_Date": metrics.get("EPS_Current_Date"),
            "EPS_Base_Date": metrics.get("EPS_Base_Date"),
            "EPS_Basis": metrics.get("EPS_Basis"),
            # Which window the growth was measured over ("sums" = N-year totals vs
            # the prior N years; "endpoint" = the two-point CAGR), and whether the
            # base window was a real trading period or a breakeven one.
            "EPS_Window": metrics.get("EPS_Window"),
            "EPS_Window_Years": metrics.get("EPS_Window_Years"),
            "BaseNetMargin": metrics.get("BaseNetMargin"),
            "BaseNetMargin_Pct": (
                f"{round(metrics['BaseNetMargin'] * 100, 2)}%"
                if metrics.get("BaseNetMargin") is not None else None
            ),
            "EPSGrowth_Years": metrics.get("EPSGrowth_Years"),
            # Why the growth rate is missing, when it is, so the report can explain it
            # instead of printing a bare "Not available" (see ROIC_Unavailable_Reason).
            "EPSGrowth_Unavailable_Reason": metrics.get("EPSGrowth_Unavailable_Reason"),
            "NetIncome": metrics.get("NetIncome"),
            "EBIT_Basis": metrics.get("EBIT_Basis"),
            "EBIT": metrics.get("EBIT"),
            "CapitalEmployed": metrics.get("CapitalEmployed"),
            "EnterpriseValue": metrics.get("EnterpriseValue"),
            "LiveMarketCap": live_cap,
            "Final_Rank": None,
            "MagicFormula_Score": None,
            "Composite_Score": None,
            # Authoritative balance-sheet figures + goodwill-inclusive ROIC. These
            # are what the reconciliation gate checks agent prose against, and what
            # keeps a rollup's headline ROC from being read as operating efficiency.
            "TotalDebt": metrics.get("TotalDebt"),
            "Cash": metrics.get("Cash"),
            "TotalEquity": metrics.get("TotalEquity"),
            "TotalAssets": metrics.get("TotalAssets"),
            "GoodwillAndIntangibles": metrics.get("GoodwillAndIntangibles"),
            "InvestedCapital": metrics.get("InvestedCapital"),
            "ROIC_InclGoodwill_Pct": f"{round(roic * 100, 2)}%" if roic is not None else None,
            # Why the companion is missing, so the report can say so instead of
            # printing a bare "Not available" next to a four-digit ROC.
            "ROIC_Unavailable_Reason": metrics.get("ROIC_Unavailable_Reason"),
            "IntangiblesShareOfAssets_Pct": f"{round(intang_share * 100, 1)}%" if intang_share is not None else None,
            "BalanceSheetDate": metrics.get("BalanceSheetDate"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})

# ==========================================
# TOOL 2: SEC EDGAR Extractor
# ==========================================
try:
    from edgar import set_identity, Company
except ImportError:
    pass

@mcp.tool()
def fetch_sec_10k_data(ticker: str) -> str:
    """
    Extracts Item 1 segment data and >10% customer concentration notes from SEC 10-K for a ticker.
    """
    sec_user_agent = os.getenv("SEC_USER_AGENT", "Your Name admin@domain.com")
    set_identity(sec_user_agent)
    
    # Rate limit logic: delay 0.12 seconds
    time.sleep(0.12)
    
    try:
        company = Company(ticker)
        if not company:
            return f"Could not find SEC data for {ticker}"
            
        filings = company.get_filings(form="10-K")
        if not filings:
            return f"No 10-K filings found for {ticker}"
            
        latest_10k = filings[0].obj()

        # edgar-tools exposes Item 1 (Business) via item indexing / the
        # .business property, not a `.item_1` attribute.
        try:
            item_1 = str(latest_10k["Item 1"] or "")
        except Exception:
            item_1 = str(getattr(latest_10k, "business", "") or "")

        content = f"--- SEC 10-K Data for {ticker} ---\n\n"
        content += item_1[:30000] # Limiting to 30k chars

        # Scan Item 1 (Business) + Item 1A (Risk Factors) for >10% customer
        # concentration notes. The TenK object has no `.text` attribute.
        scan_text = item_1
        try:
            scan_text += "\n\n" + str(getattr(latest_10k, "risk_factors", "") or "")
        except Exception:
            pass
        concentration_notes = []
        for paragraph in scan_text.split('\n\n'):
            if 'customer' in paragraph.lower() and '10%' in paragraph:
                concentration_notes.append(paragraph.strip())
                
        if concentration_notes:
            content += "\n\n--- Customer Concentration Notes (>10%) ---\n"
            content += "\n\n".join(concentration_notes[:5]) # Top 5 mentions
        
        return content
    except Exception as e:
        return f"Error fetching SEC data for {ticker}: {str(e)}"

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

# ==========================================
# TOOL 4a: FMP Stock News (recent factual, ticker-tagged financial news)
# ==========================================
@mcp.tool()
def fmp_stock_news(ticker: str) -> str:
    """
    Fetches recent, ticker-tagged financial news for a company from Financial
    Modeling Prep. Use this for HARD, factual, company-specific developments:
    earnings, guidance changes, product launches, M&A, downgrades/upgrades,
    lawsuits, and other concrete catalysts. Returns dated headlines with source
    and a text snippet.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return "FMP_API_KEY not configured."

    time.sleep(0.20)  # 300 calls/min Starter rate limit
    url = "https://financialmodelingprep.com/stable/news/stock"
    try:
        res = requests.get(url, params={"symbols": ticker, "limit": 15, "apikey": api_key}, timeout=20)
        data = res.json()
        if not isinstance(data, list):
            return json.dumps({"note": "No FMP news returned", "raw": data})
        articles = [
            {
                "publishedDate": a.get("publishedDate"),
                "publisher": a.get("publisher") or a.get("site"),
                "title": a.get("title"),
                "snippet": (a.get("text") or "")[:500],
                "url": a.get("url"),
            }
            for a in data
        ]
        return json.dumps(articles, indent=2)
    except Exception as e:
        return f"Error fetching FMP stock news for {ticker}: {str(e)}"


# ==========================================
# TOOL 4b: Web Search Tool (Tavily) — qualitative bear-case / risk research
# ==========================================
@mcp.tool()
def web_search_tool(query: str) -> str:
    """
    Runs an open-web search via Tavily for QUALITATIVE research: bear-case
    theses, valuation-drop risks, competitive/regulatory threats, analyst
    criticism, and short-seller arguments that go beyond factual news headlines.
    Returns ranked results (title, url, content) plus a synthesized answer.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "TAVILY_API_KEY not configured."

    time.sleep(0.5)  # gentle throttle
    try:
        res = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "query": query,
                "search_depth": "advanced",   # highest relevance (2 credits/search)
                "topic": "general",
                "time_range": "year",         # news/analysis within the last year
                "max_results": 5,
                "include_answer": True,
            },
            timeout=30,
        )
        res.raise_for_status()
        data = res.json()
        payload = {
            "answer": data.get("answer"),
            "results": [
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "content": item.get("content"),
                    "score": item.get("score"),
                }
                for item in data.get("results", [])
            ],
        }
        return json.dumps(payload, indent=2)
    except Exception as e:
        return f"Error executing web search: {str(e)}"

# ==========================================
# TOOL 5: FORWARD-LOOKING FEEDS (what the buy case is built from)
# ==========================================
# Everything above this point is BACKWARD-looking: filings, realised quarters, annual
# ratios. That is the right bias for a verdict, and it is the wrong bias for the one
# question a 'Watch' leaves open — *at what price, or on what evidence, does this
# become a Buy?* Answering that needs the market's forward expectations (what
# earnings analysts expect and what they will pay for them), the calendar of events
# that could confirm or break those expectations, and the current price the range is
# measured from.
#
# These feeds are deliberately kept separate from `fmp_metrics_extractor` rather than
# folded into it. Four agents read that blob on every turn and none of them should be
# reasoning from analyst projections: the bear/bull/analyst chain weighs realised
# results, and a `Buy` may not rest on a speculative catalyst (see main.py's analyst
# instruction). Only the buy-case and buy-check agents get these.
def _stable_get(path: str, context: str, **params):
    """One GET against FMP's /stable API. Returns parsed JSON, or None on failure.

    Returns None rather than raising because every caller here is assembling a
    best-effort picture: a missing price-target feed should cost the report that one
    field, not the whole tool call. Callers say so in the payload rather than
    presenting a hole as a zero.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return None
    params["apikey"] = api_key
    try:
        time.sleep(0.20)  # 300 calls/min Starter rate limit
        resp = requests.get(f"https://financialmodelingprep.com/stable/{path}",
                            params=params, timeout=25)
        data = resp.json()
    except Exception as e:
        logger.warning(f"FMP {path} failed ({context}): {e}")
        return None
    # A plan-gated endpoint answers 402 with a plain-text upgrade notice, which json()
    # may still parse as a string. Treat anything that is not a list/dict as absent.
    if isinstance(data, dict) and data.get("Error Message"):
        logger.warning(f"FMP {path} error ({context}): {data['Error Message']}")
        return None
    return data if isinstance(data, (list, dict)) else None


def _quote_row(ticker: str) -> dict:
    """Current quote for one symbol, or {} — shared by the price and estimate tools."""
    data = _stable_get("quote", f"{ticker} quote", symbol=ticker)
    return data[0] if isinstance(data, list) and data else {}


def _pct(part, whole):
    """`part` as a percentage of `whole`, or None when that would be meaningless."""
    if part is None or whole in (None, 0):
        return None
    return round(100.0 * part / whole, 1)


@mcp.tool()
def fmp_price_snapshot(ticker: str) -> str:
    """
    Current price for a company, with the levels a buy range is measured against:
    previous close, the day's range, the 52-week high/low, and the 50-day and 200-day
    moving averages.

    Use this whenever a price threshold is being written or tested. A buy trigger such
    as "buy below $38" is only meaningful next to what the stock costs today and where
    that sits in its own recent range — a target 60% below the 52-week low is not a
    plan, it is a way of never buying.

    Returns JSON with symbol, price, previousClose, changePercentage, dayLow, dayHigh,
    yearHigh, yearLow, priceAvg50, priceAvg200, marketCap, exchange, plus computed
    `pct_from_52w_high` / `pct_above_52w_low` and `as_of` — or {"error": ...}.
    """
    row = _quote_row(ticker)
    if not row.get("price"):
        return json.dumps({"error": f"No current quote available for {ticker}."})
    price = row.get("price")
    out = {k: row.get(k) for k in (
        "symbol", "name", "price", "previousClose", "change", "changePercentage",
        "dayLow", "dayHigh", "yearHigh", "yearLow", "priceAvg50", "priceAvg200",
        "marketCap", "exchange", "volume",
    )}
    hi, lo = row.get("yearHigh"), row.get("yearLow")
    out["pct_from_52w_high"] = _yoy_pct(price, hi)      # negative = below the high
    out["pct_above_52w_low"] = _yoy_pct(price, lo)
    ts = row.get("timestamp")
    out["as_of"] = (datetime.fromtimestamp(ts).isoformat(timespec="minutes")
                    if isinstance(ts, (int, float)) else datetime.now().isoformat(timespec="minutes"))
    out["note"] = ("Price is live and moves every session. Any threshold written "
                   "against it must state the price it was written at, or a reader "
                   "cannot tell a 10% discount from a stale number.")
    return json.dumps(out, separators=(",", ":"))


# FMP's Starter plan serves `analyst-estimates` for ANNUAL periods only — the
# quarterly variant answers 402. That is enough for a forward P/E, which is
# conventionally quoted on a fiscal year, but it means no quarter-by-quarter
# estimate path exists here; do not add one without re-checking the plan.
_ESTIMATE_FIELDS = ("date", "revenueAvg", "revenueLow", "revenueHigh", "ebitAvg",
                    "netIncomeAvg", "epsAvg", "epsLow", "epsHigh",
                    "numAnalystsRevenue", "numAnalystsEps")


@mcp.tool()
def fmp_forward_estimates(ticker: str) -> str:
    """
    What analysts expect this company to EARN and SELL in future fiscal years, what
    that implies about the multiple you would be paying today (forward P/E), and how
    many analysts stand behind each figure.

    Use this to answer "what would I be paying for the future, not the past?". It
    returns, for each future fiscal year on record: consensus revenue, EBIT, net
    income and EPS (mean, low, high), the number of contributing analysts, the implied
    forward P/E at today's price (and its range across the low/high EPS estimates),
    and the growth each year implies over the last ACTUAL fiscal year. It also returns
    the consensus price target, the most recent individual analyst target changes
    (with the price at the time), and the buy/hold/sell grade split.

    The coverage figures are not decoration. A forward P/E derived from a single
    analyst's EPS estimate is one person's opinion wearing the clothes of a market
    consensus, and the payload says so explicitly when the count is thin or the
    low/high spread is wide. Quote the basis whenever you quote the multiple.

    Returns JSON, or {"error": ...}.
    """
    quote = _quote_row(ticker)
    price = quote.get("price")

    rows = _stable_get("analyst-estimates", f"{ticker} estimates",
                       symbol=ticker, period="annual", limit=10)
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if not rows and price is None:
        return json.dumps({"error": f"No forward estimates or quote available for {ticker}."})

    # FMP returns furthest-out first and includes estimates for years already
    # reported. Split on today's date rather than trusting the order: a past-year
    # "estimate" is a record of what analysts thought, not a forecast, and mixing the
    # two is how a forward P/E ends up quoted off a year that has already happened.
    today = datetime.now().strftime("%Y-%m-%d")
    future = sorted([r for r in rows if (r.get("date") or "") > today],
                    key=lambda r: r.get("date") or "")
    past = sorted([r for r in rows if (r.get("date") or "") <= today],
                  key=lambda r: r.get("date") or "", reverse=True)

    # The base the growth rates are measured from: the last ANNUAL filing, not the
    # last estimate. Comparing an estimate against an estimate would report the
    # analysts' own internal consistency as if it were company growth.
    actual = _stable_get("income-statement", f"{ticker} annual actual",
                         symbol=ticker, period="annual", limit=1)
    actual = actual[0] if isinstance(actual, list) and actual else {}
    base_rev = actual.get("revenue")
    base_eps = actual.get("epsDiluted") or actual.get("eps")

    def _fwd(row):
        eps, eps_lo, eps_hi = row.get("epsAvg"), row.get("epsLow"), row.get("epsHigh")
        item = {k: row.get(k) for k in _ESTIMATE_FIELDS if row.get(k) is not None}
        if price and isinstance(eps, (int, float)) and eps > 0:
            item["forward_pe"] = round(price / eps, 1)
            # Widest defensible reading of the same price: the cheapest multiple comes
            # from the most optimistic EPS. Printing the band next to the point
            # estimate is what stops a 14x headline from hiding a 9x-31x disagreement.
            if isinstance(eps_hi, (int, float)) and eps_hi > 0:
                item["forward_pe_at_high_eps"] = round(price / eps_hi, 1)
            if isinstance(eps_lo, (int, float)) and eps_lo > 0:
                item["forward_pe_at_low_eps"] = round(price / eps_lo, 1)
        item["revenue_growth_vs_last_actual_pct"] = _yoy_pct(row.get("revenueAvg"), base_rev)
        item["eps_growth_vs_last_actual_pct"] = _yoy_pct(eps, base_eps)
        # How many fiscal years that growth is spread over. A forward year two or
        # three years out compounds, and quoting its cumulative growth as though it
        # were annual is the easiest way to make an ordinary forecast look like a
        # transformation. The caller annualises; this makes it possible to.
        try:
            item["fiscal_years_from_last_actual"] = (
                int(row["date"][:4]) - int(actual["date"][:4])) or None
        except Exception:
            pass
        n_eps = row.get("numAnalystsEps") or 0
        if n_eps and n_eps <= 2:
            item["coverage_warning"] = (
                f"Only {n_eps} analyst estimate(s) for EPS. This is one or two "
                f"opinions, not a consensus — do not describe it as 'the market expects'.")
        if (isinstance(eps_lo, (int, float)) and isinstance(eps_hi, (int, float))
                and eps_lo > 0 and eps_hi / eps_lo >= 1.5):
            item["spread_warning"] = (
                f"Estimates disagree widely (EPS {eps_lo} to {eps_hi}). The mean is a "
                f"midpoint between materially different views of this business.")
        return {k: v for k, v in item.items() if v is not None}

    payload = {
        "symbol": ticker,
        "current_price": price,
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "last_actual_fiscal_year": (
            {"date": actual.get("date"), "revenue": base_rev, "epsDiluted": base_eps}
            if actual else None
        ),
        "forward_fiscal_years": [_fwd(r) for r in future],
        # Kept because it is the only cheap check on whether this company's analysts
        # have historically been anywhere near right. Compare these to the actual
        # results in QUARTERLY_DATA / the filings before leaning on the forward year.
        "past_fiscal_year_estimates": [
            {k: r.get(k) for k in ("date", "revenueAvg", "epsAvg", "numAnalystsEps")
             if r.get(k) is not None}
            for r in past[:2]
        ],
        "price_target_consensus": (_stable_get("price-target-consensus",
                                               f"{ticker} price target", symbol=ticker) or None),
        "recent_analyst_target_changes": [
            {"date": (a.get("publishedDate") or "")[:10],
             "firm": a.get("analystCompany"), "analyst": a.get("analystName"),
             "priceTarget": a.get("priceTarget"), "priceWhenPosted": a.get("priceWhenPosted"),
             "headline": a.get("newsTitle")}
            for a in (_stable_get("price-target-news", f"{ticker} target news",
                                  symbol=ticker, limit=8) or [])
            if isinstance(a, dict)
        ],
        "grades_consensus": (_stable_get("grades-consensus", f"{ticker} grades",
                                         symbol=ticker) or None),
        "basis_note": (
            "forward_pe = current_price / that fiscal year's CONSENSUS MEAN EPS. These "
            "are ESTIMATES, not results: they are revised constantly, they are "
            "systematically optimistic at long horizons, and on this data plan they "
            "are available for FISCAL YEARS ONLY (no quarterly estimates). Fiscal "
            "years do not align with calendar years for many companies — quote the "
            "period-end date shown in `date`, never 'next year'. Analyst price targets "
            "are opinions about price, not about the business; report who set one and "
            "when, and never treat a consensus target as a valuation."
        ),
    }
    if not future:
        payload["coverage_note"] = (
            f"No FUTURE fiscal-year estimates are on record for {ticker}. That is "
            f"common for small and thinly-covered companies and is itself a finding: "
            f"there is no analyst consensus here to build a forward multiple on. Say "
            f"so plainly rather than substituting a trailing multiple for a forward one.")
    return json.dumps(payload, separators=(",", ":"))


@mcp.tool()
def fmp_earnings_calendar(ticker: str) -> str:
    """
    When a company next reports, what analysts expect it to report, and how its last
    four reports landed against expectations.

    Works for ANY symbol, which is the point: a buy case for a supplier is often
    decided by what its largest CUSTOMERS say, so call this for the customer's ticker
    as well as the subject's. If NVDA's case rests on hyperscaler capital spending,
    the dates MSFT and META report are dates on which the case can be checked.

    Returns JSON with `upcoming` (scheduled date + EPS/revenue estimates) and
    `recent` (reported date, actual vs. estimated EPS and revenue, plus a computed
    surprise percentage), or {"error": ...}.

    Scheduled dates before a company confirms them are PROVISIONAL and move by days —
    never write a trigger that turns on the exact date without saying so.
    """
    ticker = (ticker or "").strip().upper()
    rows = _stable_get("earnings", f"{ticker} earnings calendar", symbol=ticker, limit=12)
    rows = [r for r in rows if isinstance(r, dict) and r.get("date")] if isinstance(rows, list) else []
    if not rows:
        return json.dumps({"error": f"No earnings calendar available for {ticker}."})

    today = datetime.now().strftime("%Y-%m-%d")
    # `epsActual` is backfilled with a lag, so a report from two days ago can still
    # show a null actual. Split on the DATE (the same rule the screener's recent-
    # earnings gate uses) rather than on whether an actual has arrived.
    upcoming = sorted([r for r in rows if r["date"] > today], key=lambda r: r["date"])
    recent = sorted([r for r in rows if r["date"] <= today], key=lambda r: r["date"], reverse=True)

    def _reported(r):
        item = {
            "date": r.get("date"),
            "epsActual": r.get("epsActual"), "epsEstimated": r.get("epsEstimated"),
            "revenueActual": r.get("revenueActual"), "revenueEstimated": r.get("revenueEstimated"),
        }
        item["eps_surprise_pct"] = _yoy_pct(r.get("epsActual"), r.get("epsEstimated"))
        item["revenue_surprise_pct"] = _yoy_pct(r.get("revenueActual"), r.get("revenueEstimated"))
        return {k: v for k, v in item.items() if v is not None}

    return json.dumps({
        "symbol": ticker,
        "as_of": today,
        "upcoming": [{"date": r["date"], "epsEstimated": r.get("epsEstimated"),
                      "revenueEstimated": r.get("revenueEstimated")} for r in upcoming[:3]],
        "recent": [_reported(r) for r in recent[:4]],
        "note": ("Upcoming dates are the provider's schedule and are provisional until "
                 "the company confirms them; treat them as a week, not a day. A null "
                 "actual on a date that has passed usually means the provider has not "
                 "backfilled it yet, not that the company failed to report."),
    }, separators=(",", ":"))


@mcp.tool()
def fmp_revenue_segments(ticker: str) -> str:
    """
    Where a company's revenue actually comes from: its product/segment split and its
    geographic split, for the last two fiscal years, with each line as a share of
    total and its year-over-year change.

    Use this before naming a customer, an end market, or an upstream consumer as
    something that matters to the revenue line. A segment worth 3% of sales does not
    move the thesis however exciting its end market is, and "who buys this" is a
    question the segment table usually answers better than a press release does.

    Returns JSON with `product_segments` and `geographic_segments`, or {"error": ...}.
    """
    def _series(path, label):
        rows = _stable_get(path, f"{ticker} {label}", symbol=ticker, period="annual")
        rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("data"), dict)] \
            if isinstance(rows, list) else []
        rows = sorted(rows, key=lambda r: r.get("date") or "", reverse=True)[:2]
        if not rows:
            return None
        latest, prior = rows[0], (rows[1] if len(rows) > 1 else {"data": {}})
        total = sum(v for v in latest["data"].values() if isinstance(v, (int, float)))
        out = {
            "fiscal_year_end": latest.get("date"),
            "prior_fiscal_year_end": prior.get("date"),
            "total": total or None,
            "lines": sorted(
                [{"segment": k, "revenue": v,
                  "pct_of_total": _pct(v, total),
                  "yoy_pct": _yoy_pct(v, prior["data"].get(k))}
                 for k, v in latest["data"].items() if isinstance(v, (int, float))],
                key=lambda d: d["revenue"], reverse=True,
            ),
        }
        # Companies stop disclosing a breakdown when it stops being material, and the
        # provider simply keeps serving the last year it has. H&R Block's geographic
        # split ends in FY2016 — a decade stale, and perfectly capable of being read
        # as current by anyone who does not check the date. Say it in the payload.
        try:
            age = datetime.now().year - int((latest.get("date") or "")[:4])
        except Exception:
            age = 0
        if age >= 3:
            out["stale_warning"] = (
                f"This is the most recent {label} breakdown on record and it is about "
                f"{age} years old (fiscal year ending {latest.get('date')}). The "
                f"company has not disclosed one since. Do not present these shares as "
                f"the current mix.")
        return out

    product = _series("revenue-product-segmentation", "product segments")
    geo = _series("revenue-geographic-segmentation", "geographic segments")
    if not product and not geo:
        return json.dumps({"error": f"No segment disclosure available for {ticker}."})
    return json.dumps({
        "symbol": ticker,
        "product_segments": product,
        "geographic_segments": geo,
        "note": ("Segments are as the company chooses to report them and are ANNUAL. "
                 "They tell you what it sells and where, not who it sells to — a "
                 "named customer needs a filing (the SEC data carries >10% customer "
                 "concentration) or a source, not an inference from a segment name."),
    }, separators=(",", ":"))


# Pages of the M&A feed scanned by `fmp_pending_ma_filings`. Two pages of 250 covered
# roughly two years of filings when this was written, which is far enough back for a
# transaction still awaiting completion. The endpoint has no symbol filter on this
# plan (`mergers-acquisitions-search` answers 402), so the filtering is done here.
_MA_PAGES = 2
_MA_PAGE_SIZE = 250


@mcp.tool()
def fmp_pending_ma_filings(ticker: str) -> str:
    """
    SEC merger/acquisition filings naming this company, as either acquirer or target.

    A pending transaction is the single largest thing that can invalidate a price-based
    buy trigger: a company under an agreed bid trades on the offer, not on its
    earnings, and a buy range derived from a forward multiple becomes meaningless.
    Check this before writing one.

    STRICTLY LIMITED, and the limitation matters: this covers registration statements
    filed with the SEC (S-4 and similar) over roughly the last two years. It does NOT
    cover rumoured deals, cash tender offers that file differently, private-side talks,
    or anything reported in the press but not yet filed. An empty result means "no
    such filing was found", never "no deal is happening" — use `web_search_tool` and
    `fmp_stock_news` for the rest.

    Returns JSON with any matching filings (acquirer, target, transaction date, SEC
    link) plus the coverage caveat.
    """
    ticker = (ticker or "").strip().upper()
    hits, scanned = [], 0
    for page in range(_MA_PAGES):
        rows = _stable_get("mergers-acquisitions-latest", f"{ticker} M&A page {page}",
                           page=page, limit=_MA_PAGE_SIZE)
        if not isinstance(rows, list) or not rows:
            break
        scanned += len(rows)
        for r in rows:
            if not isinstance(r, dict):
                continue
            if ticker in {(r.get("symbol") or "").upper(),
                          (r.get("targetedSymbol") or "").upper()}:
                hits.append({
                    "role": "acquirer" if (r.get("symbol") or "").upper() == ticker else "target",
                    "acquirer": r.get("companyName"), "acquirerSymbol": r.get("symbol"),
                    "target": r.get("targetedCompanyName"), "targetSymbol": r.get("targetedSymbol"),
                    "transactionDate": r.get("transactionDate"),
                    "filingAccepted": r.get("acceptedDate"), "link": r.get("link"),
                })
    return json.dumps({
        "symbol": ticker,
        "filings": hits,
        "filings_scanned": scanned,
        "coverage": ("SEC registration statements (S-4 and similar) only, roughly the "
                     "last two years. Rumoured, unannounced, and differently-filed "
                     "transactions do not appear. An empty list is not evidence that "
                     "no transaction is pending."),
    }, separators=(",", ":"))


# ==========================================
# DATABASE INITIALIZATION & TOOLS
# ==========================================
def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL not set")
    return psycopg2.connect(db_url)

def initialize_database():
    """Ensure the schema exists. This mirrors sql-schema.sql exactly (the
    authoritative schema): a pipeline_runs parent table referenced by
    agent_outputs and final_reports via FK, a `metadata` (not metadata_json)
    JSONB column, and a verdict CHECK constrained to BUY/SELL/HOLD.
    Uses IF NOT EXISTS so it is a safe no-op when tables already exist."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP WITH TIME ZONE,
                status VARCHAR(50) NOT NULL DEFAULT 'RUNNING',
                top_30_tickers TEXT[],
                model_requests INTEGER,
                input_tokens BIGINT,
                output_tokens BIGINT,
                total_tokens BIGINT,
                search_requests INTEGER,
                llm_cost_usd NUMERIC(12, 6),
                search_cost_usd NUMERIC(12, 6),
                total_cost_usd NUMERIC(12, 6)
            )
        ''')

        # Idempotently add the usage/cost columns to pre-existing pipeline_runs tables.
        for col_def in [
            "completed_at TIMESTAMP WITH TIME ZONE",
            "model_requests INTEGER",
            "input_tokens BIGINT",
            "output_tokens BIGINT",
            "total_tokens BIGINT",
            "search_requests INTEGER",
            "llm_cost_usd NUMERIC(12, 6)",
            "search_cost_usd NUMERIC(12, 6)",
            "total_cost_usd NUMERIC(12, 6)",
            # Set only by the critic refinement loop (refine.py): the run whose report
            # this run reviewed. NULL for every ordinary pipeline run, which is also
            # the test for "is this a refinement" — no separate flag to keep in sync.
            # Without it the only link back to the reviewed run lived in a JSONB
            # metadata field, which is not something the web UI can reasonably join on.
            "refines_run_id UUID",
        ]:
            cur.execute(f"ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS {col_def}")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_runs_refines "
                    "ON pipeline_runs(refines_run_id) WHERE refines_run_id IS NOT NULL")

        # Drop the provider-specific Brave columns (superseded by search_*).
        cur.execute("ALTER TABLE pipeline_runs DROP COLUMN IF EXISTS brave_requests")
        cur.execute("ALTER TABLE pipeline_runs DROP COLUMN IF EXISTS brave_cost_usd")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS agent_outputs (
                output_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                run_id UUID REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
                ticker VARCHAR(10) NOT NULL,
                agent_type VARCHAR(50) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                raw_content TEXT NOT NULL,
                metadata JSONB,
                embedding vector(768)
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS final_reports (
                report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                run_id UUID REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
                ticker VARCHAR(10) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                verdict VARCHAR(10) CHECK (verdict IN ('BUY', 'WATCH', 'AVOID', 'HOLD', 'SELL')),
                markdown_report TEXT NOT NULL,
                embedding vector(768)
            )
        ''')

        # The verdict vocabulary is Buy/Watch/Avoid (written for a non-owner). Legacy
        # BUY/SELL/HOLD values are kept valid so pre-existing rows don't break. This
        # replaces the original constraint on already-created tables.
        cur.execute("ALTER TABLE final_reports DROP CONSTRAINT IF EXISTS final_reports_verdict_check")
        cur.execute(
            "ALTER TABLE final_reports ADD CONSTRAINT final_reports_verdict_check "
            "CHECK (verdict IN ('BUY', 'WATCH', 'AVOID', 'HOLD', 'SELL'))"
        )

        # Fingerprint of the inputs a report was produced from (ticker + balance
        # sheet date + prompt version). Lets a re-run detect that nothing material
        # has changed and reuse the stored report instead of paying to regenerate an
        # identical analysis. Nullable: rows written before this existed simply do
        # not participate in reuse.
        cur.execute("ALTER TABLE final_reports ADD COLUMN IF NOT EXISTS analysis_key TEXT")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_final_reports_analysis_key "
            "ON final_reports(ticker, analysis_key)"
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_outputs_ticker ON agent_outputs(ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_final_reports_ticker ON final_reports(ticker)")

        # ticker_runs: a lean per-ticker index of every pipeline run, used to drive
        # the web UI (ticker picker -> run list -> reports). The heavy report text
        # stays in agent_outputs / final_reports; the UI joins on run_id.
        cur.execute('''
            CREATE TABLE IF NOT EXISTS ticker_runs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                ticker VARCHAR(10) NOT NULL,
                run_id UUID NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
                company_name TEXT,
                verdict VARCHAR(10),
                run_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                magic_rank INTEGER,
                UNIQUE (ticker, run_id)
            )
        ''')
        # Idempotent add for tables created before the rank was recorded. Existing
        # rows keep NULL: the screener CSV those runs came from has long since been
        # overwritten, so their rank is genuinely unknown rather than zero.
        cur.execute("ALTER TABLE ticker_runs ADD COLUMN IF NOT EXISTS magic_rank INTEGER")
        # The share price at the moment the ticker was analysed, and when that was.
        # Stored rather than fetched live by the viewer on purpose: the web app is
        # read-only, has no FMP credential, and is meant to stay that way — and the
        # useful number in a run listing is what the stock cost WHEN THE VERDICT WAS
        # REACHED, not what it costs while you happen to be reading. Rows written
        # before this column existed keep NULL, which the UI renders as an em dash;
        # backfilling them is impossible, since the price that day is gone.
        cur.execute("ALTER TABLE ticker_runs ADD COLUMN IF NOT EXISTS share_price NUMERIC")
        cur.execute("ALTER TABLE ticker_runs ADD COLUMN IF NOT EXISTS price_as_of TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ticker_runs_ticker ON ticker_runs(ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ticker_runs_date ON ticker_runs(run_date DESC)")

        # critic_memory: the independent critic's findings, kept as LONG-TERM memory
        # across refinement sessions (see critic_agent.py / refine.py). The analyst is
        # shown it before every revision so a point it already conceded is not
        # re-corrected, and the critic is shown it so a point already settled is not
        # re-raised — that repetition is what turns a bounded feedback loop into one
        # that burns its whole budget relitigating round 1.
        #
        # Deliberately NO foreign key onto pipeline_runs. Every other table here
        # cascades away with its run, which is correct for per-run artefacts. This one
        # is memory: if it vanished with the run it was learned from, the same fallacy
        # would come back on the next refinement and be paid for a second time. The
        # run ids are recorded for provenance, not as constraints.
        cur.execute('''
            CREATE TABLE IF NOT EXISTS critic_memory (
                memory_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                ticker VARCHAR(10) NOT NULL,
                source_run_id UUID,
                refine_run_id UUID,
                iteration INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                severity VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN',
                finding_type VARCHAR(80),
                title TEXT,
                finding TEXT NOT NULL,
                analyst_response TEXT,
                status VARCHAR(16) NOT NULL DEFAULT 'OPEN'
            )
        ''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_critic_memory_ticker "
                    "ON critic_memory(ticker, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_critic_memory_refine_run "
                    "ON critic_memory(refine_run_id)")

        # Backfill ticker_runs from any pre-existing final_reports so the UI shows
        # historical runs. company_name is unknown for old rows (left NULL).
        cur.execute('''
            INSERT INTO ticker_runs (ticker, run_id, verdict, run_date)
            SELECT fr.ticker, fr.run_id, fr.verdict, COALESCE(pr.started_at, fr.created_at)
            FROM final_reports fr
            JOIN pipeline_runs pr ON fr.run_id = pr.run_id
            ON CONFLICT (ticker, run_id) DO NOTHING
        ''')

        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

# Note: In production you'd call this outside module load, but we do it here for simplicity
initialize_database()

# Embedding calls are billed but were never counted anywhere in the cost estimate.
# Four vectors are generated per ticker (BEAR_CASE, BULL_CASE, SALE_CASE, final
# report), and a fifth (BUY_CASE) for a ticker whose verdict lands on 'Watch'. The
# orchestrator drains this counter after each ticker rather than threading a usage
# object through every db_* tool signature.
EMBEDDING_USAGE = {"chars": 0, "requests": 0}


def drain_embedding_usage() -> dict:
    """Return counts accumulated since the last drain, and reset."""
    snapshot = dict(EMBEDDING_USAGE)
    EMBEDDING_USAGE["chars"] = 0
    EMBEDDING_USAGE["requests"] = 0
    return snapshot


EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768          # must match vector(768) in sql-schema.sql
_EMBED_FAILURES = {"n": 0}


def get_embedding(text: str) -> List[float]:
    """Embed text for semantic search over stored reports.

    Previously this imported `google.generativeai` (the legacy SDK, not installed
    here) inside a bare `except: pass`, so every call fell through to a zero vector
    in complete silence. Every embedding written to the database was zeros and
    similarity search was returning noise. Two fixes: use the SDK the project
    actually depends on, and make a failure LOUD — a degraded fallback that says
    nothing is how that went unnoticed.

    `text-embedding-004` is retired (404 on the current API). `gemini-embedding-001`
    replaces it and defaults to 3072 dimensions, so output_dimensionality is pinned
    to 768 to match the schema.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.error("GOOGLE_API_KEY not set — storing a ZERO embedding. "
                     "Semantic search over this row will not work.")
        return [0.0] * EMBED_DIMS
    try:
        from google import genai
        from google.genai import types as genai_types

        payload = text[:10000]
        client = genai.Client(api_key=api_key)
        result = client.models.embed_content(
            model=EMBED_MODEL,
            contents=payload,
            config=genai_types.EmbedContentConfig(output_dimensionality=EMBED_DIMS),
        )
        values = list(result.embeddings[0].values)
        if len(values) != EMBED_DIMS:
            raise ValueError(f"expected {EMBED_DIMS} dims, got {len(values)}")
        # The embed response carries no usage metadata, so bill from the payload
        # actually sent; the caller converts characters to tokens.
        EMBEDDING_USAGE["chars"] += len(payload)
        EMBEDDING_USAGE["requests"] += 1
        return values
    except Exception as e:
        _EMBED_FAILURES["n"] += 1
        # Log the first few in full, then stay quiet to avoid flooding a 30-ticker run.
        if _EMBED_FAILURES["n"] <= 3:
            logger.error(
                f"Embedding failed ({type(e).__name__}: {str(e)[:200]}). Storing a "
                f"ZERO vector — this row will not be findable by semantic search."
            )
        elif _EMBED_FAILURES["n"] == 4:
            logger.error("Further embedding failures suppressed for this run.")
        return [0.0] * EMBED_DIMS

@mcp.tool()
def db_create_pipeline_run(run_id: str, tickers: List[str],
                           refines_run_id: str = "") -> str:
    """
    Creates the parent pipeline_runs row for a batch execution. This MUST be
    called before storing any agent_outputs/final_reports, because those tables
    have a foreign key onto pipeline_runs(run_id).

    `refines_run_id` is set only by the critic refinement loop, naming the run whose
    report this one reviewed. A non-NULL value IS the marker that a run is a
    refinement rather than an analysis — the web UI reads it to link the two and to
    show whether the critic ended up agreeing (`status`).
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO pipeline_runs (run_id, status, top_30_tickers, refines_run_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
        ''', (run_id, "RUNNING", list(tickers), (refines_run_id or "").strip() or None))
        conn.commit()
        cur.close()
        conn.close()
        return f"Created pipeline run {run_id} with {len(tickers)} tickers"
    except Exception as e:
        logger.error(f"Error creating pipeline run {run_id}: {e}")
        return f"Error creating pipeline run: {str(e)}"

@mcp.tool()
def db_update_pipeline_status(run_id: str, status: str) -> str:
    """
    Updates the status of a pipeline_runs row (e.g. 'COMPLETED', 'FAILED').
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE pipeline_runs SET status = %s WHERE run_id = %s", (status, run_id))
        conn.commit()
        cur.close()
        conn.close()
        return f"Updated pipeline run {run_id} status to {status}"
    except Exception as e:
        logger.error(f"Error updating pipeline status {run_id}: {e}")
        return f"Error updating pipeline status: {str(e)}"

@mcp.tool()
def db_finalize_pipeline_run(
    run_id: str,
    status: str,
    model_requests: int,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    search_requests: int,
    llm_cost_usd: float,
    search_cost_usd: float,
    total_cost_usd: float,
) -> str:
    """
    Finalizes a pipeline_runs row: sets the terminal status, completion time,
    and the aggregated token/search usage and estimated cost for the run.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            UPDATE pipeline_runs
            SET status = %s,
                completed_at = CURRENT_TIMESTAMP,
                model_requests = %s,
                input_tokens = %s,
                output_tokens = %s,
                total_tokens = %s,
                search_requests = %s,
                llm_cost_usd = %s,
                search_cost_usd = %s,
                total_cost_usd = %s
            WHERE run_id = %s
        ''', (status, model_requests, input_tokens, output_tokens, total_tokens,
              search_requests, llm_cost_usd, search_cost_usd, total_cost_usd, run_id))
        conn.commit()
        cur.close()
        conn.close()
        return f"Finalized pipeline run {run_id} ({status}, ${total_cost_usd:.4f})"
    except Exception as e:
        logger.error(f"Error finalizing pipeline run {run_id}: {e}")
        return f"Error finalizing pipeline run: {str(e)}"

@mcp.tool()
def db_store_agent_output(run_id: str, ticker: str, agent_type: str, raw_content: str,
                          metadata_json: str, embed: bool = True) -> str:
    """
    Stores agent output in PostgreSQL. When `embed` is True a text-embedding-004
    vector is generated for semantic search; pass embed=False for raw provenance
    rows (e.g. SEC/metrics) to store the text with a NULL embedding and skip the
    embedding API call.
    """
    try:
        embedding = get_embedding(raw_content) if embed else None
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO agent_outputs (run_id, ticker, agent_type, raw_content, metadata, embedding)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (run_id, ticker, agent_type, raw_content, metadata_json, embedding))
        conn.commit()
        cur.close()
        conn.close()
        return f"Successfully stored output for {ticker} by {agent_type}"
    except Exception as e:
        logger.error(f"Error storing agent output for {ticker}/{agent_type}: {e}")
        return f"Error storing agent output: {str(e)}"

@mcp.tool()
def db_find_reusable_report(ticker: str, analysis_key: str, max_age_hours: int = 24) -> str:
    """
    Finds a previously generated report for `ticker` whose inputs fingerprint
    (`analysis_key`) matches and which is younger than `max_age_hours`.

    Used to skip paying to regenerate an identical analysis. The age bound matters:
    the fingerprint covers the filings and the prompt version, but the agents also
    do live news and web research, so an old report can be stale even when the
    financials have not moved. Returns JSON with run_id/verdict/markdown_report/
    created_at, or {"found": false}.

    It also returns the reused run's `share_price` / `price_as_of`. A reuse serves the
    EARLIER report unchanged — including the price section printed at the top of it —
    so the new run's listing row must carry that same price. Storing a fresh quote
    against a reused report would put one price in the run table and a different one
    in the document it links to, which is the exact inconsistency the price column
    exists to remove.
    """
    if not analysis_key:
        return json.dumps({"found": False, "reason": "no analysis key"})
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            SELECT f.run_id, f.verdict, f.markdown_report, f.created_at,
                   EXTRACT(EPOCH FROM (NOW() - f.created_at)) / 3600.0 AS age_hours,
                   t.share_price, t.price_as_of
              FROM final_reports f
              LEFT JOIN ticker_runs t
                     ON t.run_id = f.run_id AND t.ticker = f.ticker
             WHERE f.ticker = %s AND f.analysis_key = %s
               AND f.created_at > NOW() - (%s || ' hours')::interval
             ORDER BY f.created_at DESC
             LIMIT 1
        ''', (ticker, analysis_key, str(int(max_age_hours))))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return json.dumps({"found": False})
        return json.dumps({
            "found": True,
            "run_id": str(row[0]),
            "verdict": row[1],
            "markdown_report": row[2],
            "created_at": str(row[3]),
            "age_hours": round(float(row[4]), 1),
            "share_price": float(row[5]) if row[5] is not None else None,
            "price_as_of": row[6],
        })
    except Exception as e:
        logger.error(f"Error looking up reusable report for {ticker}: {e}")
        return json.dumps({"found": False, "error": str(e)})


@mcp.tool()
def db_copy_ticker_outputs(src_run_id: str, dst_run_id: str, ticker: str) -> str:
    """
    Copies a ticker's stored report and agent outputs from one run to another.

    Used by the duplicate-run skip: when a run reuses an earlier report, the new
    run still needs its own rows or the web UI would list the ticker and then fail
    to load anything for it. Embeddings are copied as-is rather than regenerated —
    the text is identical, so re-embedding would be paid work for no change.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO final_reports (run_id, ticker, verdict, markdown_report, embedding, analysis_key)
            SELECT %s, ticker, verdict, markdown_report, embedding, analysis_key
              FROM final_reports
             WHERE run_id = %s AND ticker = %s
             ORDER BY created_at DESC LIMIT 1
        ''', (dst_run_id, src_run_id, ticker))
        reports = cur.rowcount
        cur.execute('''
            INSERT INTO agent_outputs (run_id, ticker, agent_type, raw_content, metadata, embedding)
            SELECT %s, ticker, agent_type, raw_content, metadata, embedding
              FROM agent_outputs
             WHERE run_id = %s AND ticker = %s
        ''', (dst_run_id, src_run_id, ticker))
        outputs = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return f"Copied {reports} report and {outputs} agent outputs for {ticker}"
    except Exception as e:
        logger.error(f"Error copying outputs for {ticker}: {e}")
        return f"Error: {e}"


@mcp.tool()
def db_spend_since(hours: int = 24) -> str:
    """
    Total estimated spend recorded across pipeline runs in the last `hours`.
    Backs the daily budget guard. Returns JSON {total_usd, runs}.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            SELECT COALESCE(SUM(total_cost_usd), 0), COUNT(*)
              FROM pipeline_runs
             WHERE started_at > NOW() - (%s || ' hours')::interval
        ''', (str(int(hours)),))
        total, runs = cur.fetchone()
        cur.close()
        conn.close()
        return json.dumps({"total_usd": float(total or 0), "runs": int(runs or 0)})
    except Exception as e:
        logger.error(f"Error computing recent spend: {e}")
        return json.dumps({"total_usd": 0.0, "runs": 0, "error": str(e)})


@mcp.tool()
def db_store_final_report(run_id: str, ticker: str, verdict: str, markdown_report: str,
                          analysis_key: str = "") -> str:
    """
    Stores final report in PostgreSQL database with vector embeddings.
    The verdict is normalized to the uppercase BUY/WATCH/AVOID values allowed by
    the final_reports CHECK constraint (WATCH is the neutral default).

    `analysis_key` fingerprints the inputs this report was produced from so a later
    run can detect that nothing material changed and reuse it (see db_find_reusable_report).
    """
    try:
        normalized = (verdict or "").strip().upper()
        if normalized not in ("BUY", "WATCH", "AVOID"):
            normalized = "WATCH"
        embedding = get_embedding(markdown_report)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO final_reports (run_id, ticker, verdict, markdown_report, embedding, analysis_key)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (run_id, ticker, normalized, markdown_report, embedding, analysis_key or None))
        conn.commit()
        cur.close()
        conn.close()
        return f"Successfully stored final report for {ticker} with verdict {normalized}"
    except Exception as e:
        logger.error(f"Error storing final report for {ticker}: {e}")
        return f"Error storing final report: {str(e)}"

@mcp.tool()
def db_store_ticker_run(run_id: str, ticker: str, company_name: str, verdict: str,
                        magic_rank: Optional[int] = None,
                        share_price=None, price_as_of: str = None) -> str:
    """
    Records a per-ticker index row in ticker_runs (ticker -> run + verdict + date).
    This drives the web UI's ticker picker and run list. Idempotent per (ticker, run_id).

    `magic_rank` is the ticker's Final_Rank in the screen that selected it (1 = best),
    used by the UI to order a run's decisions within each Buy/Watch/Avoid group. Pass
    None for on-demand single-ticker runs, which never went through a ranking.

    `share_price` / `price_as_of` are the quote the analysis was written against, so a
    run listing can show a price column without the read-only viewer needing a market
    data credential of its own. Both optional: a run whose quote could not be fetched
    stores NULL rather than a stale or invented number.
    """
    try:
        normalized = (verdict or "").strip().upper()
        # Guard the cast: --from-csv hands this through pandas, so the rank can arrive
        # as a numpy int, a float, or NaN, none of which psycopg2 binds to an INTEGER.
        try:
            rank = int(magic_rank) if magic_rank is not None and magic_rank == magic_rank else None
        except (TypeError, ValueError):
            rank = None
        conn = get_db_connection()
        cur = conn.cursor()
        # Same guard as the rank, for the same reason: a price arriving from pandas
        # can be a numpy float or NaN.
        try:
            price = (float(share_price)
                     if share_price is not None and share_price == share_price else None)
        except (TypeError, ValueError):
            price = None
        cur.execute('''
            INSERT INTO ticker_runs (ticker, run_id, company_name, verdict, magic_rank,
                                     share_price, price_as_of)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, run_id)
            DO UPDATE SET company_name = EXCLUDED.company_name,
                          verdict = EXCLUDED.verdict,
                          magic_rank = EXCLUDED.magic_rank,
                          -- COALESCE so a re-write that could not fetch a quote does
                          -- not erase a price the first write captured.
                          share_price = COALESCE(EXCLUDED.share_price, ticker_runs.share_price),
                          price_as_of = COALESCE(EXCLUDED.price_as_of, ticker_runs.price_as_of)
        ''', (ticker, run_id, company_name, normalized, rank, price, price_as_of))
        conn.commit()
        cur.close()
        conn.close()
        return f"Recorded ticker_run for {ticker} ({normalized})"
    except Exception as e:
        logger.error(f"Error recording ticker_run for {ticker}: {e}")
        return f"Error recording ticker_run: {str(e)}"

@mcp.tool()
def db_search_historical_reports(query_text: str, ticker: str = "", limit: int = 5) -> str:
    """
    Converts query_text to an embedding and runs a cosine similarity search against final_reports.
    """
    try:
        embedding = get_embedding(query_text)
        conn = get_db_connection()
        cur = conn.cursor()
        
        if ticker:
            cur.execute('''
                SELECT ticker, verdict, markdown_report, 1 - (embedding <=> %s::vector) as similarity
                FROM final_reports
                WHERE ticker = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            ''', (embedding, ticker, embedding, limit))
        else:
            cur.execute('''
                SELECT ticker, verdict, markdown_report, 1 - (embedding <=> %s::vector) as similarity
                FROM final_reports
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            ''', (embedding, embedding, limit))
            
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        results = []
        for row in rows:
            results.append({
                "ticker": row[0],
                "verdict": row[1],
                "markdown_report_snippet": row[2][:500] + "...",
                "similarity": float(row[3])
            })
            
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error searching historical reports: {str(e)}"


@mcp.tool()
def db_get_sale_case(ticker: str, run_id: str = "") -> str:
    """
    Fetch a SALE_CASE (Phase C sale-advisory conditions) for a ticker. When `run_id`
    is given, returns that specific run's SALE_CASE (so a held position is evaluated
    against the exact thesis it was bought under); otherwise returns the most recent
    one. Returns JSON with run_id, created_at, and the raw sale conditions (the
    specific, measurable sell triggers). Returns an error JSON if no matching sale
    advisory is on record.
    """
    try:
        ticker = ticker.strip().upper()
        run_id = (run_id or "").strip()
        conn = get_db_connection()
        cur = conn.cursor()
        if run_id:
            cur.execute('''
                SELECT run_id::text, created_at, raw_content
                FROM agent_outputs
                WHERE ticker = %s AND agent_type = 'SALE_CASE' AND run_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            ''', (ticker, run_id))
        else:
            cur.execute('''
                SELECT run_id::text, created_at, raw_content
                FROM agent_outputs
                WHERE ticker = %s AND agent_type = 'SALE_CASE'
                ORDER BY created_at DESC
                LIMIT 1
            ''', (ticker,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            where = f"in run {run_id}" if run_id else "on record"
            return json.dumps({"error": f"No SALE_CASE found for {ticker} {where}. Run the analysis pipeline first."})
        return json.dumps({
            "ticker": ticker,
            "run_id": row[0],
            "created_at": row[1].isoformat() if row[1] else None,
            "sale_conditions": row[2],
        })
    except Exception as e:
        return json.dumps({"error": f"Error fetching sale case for {ticker}: {str(e)}"})


@mcp.tool()
def db_get_buy_case(ticker: str, run_id: str = "") -> str:
    """
    Fetch a BUY_CASE (the entry conditions written for a ticker whose verdict was
    'Watch') for a ticker. With `run_id`, returns that specific run's BUY_CASE;
    otherwise the most recent one. Returns JSON with run_id, created_at, verdict of
    the run it belongs to, and the raw buy conditions (the price range and the
    measurable events that would make this a Buy). Returns an error JSON if none is
    on record.

    Deliberately shaped like `db_get_sale_case` rather than routed through
    `db_get_agent_output`: `--buy-check` reads this the way `--sell-check` reads that
    one, and the two commands should fail with the same shape of message when there
    is nothing to check. The verdict join is the one addition — a buy case is only
    ever written for a 'Watch', so knowing the verdict of the run it came from is what
    lets the caller warn that a later report has moved off Watch.
    """
    try:
        ticker = ticker.strip().upper()
        run_id = (run_id or "").strip()
        conn = get_db_connection()
        cur = conn.cursor()
        base = '''
            SELECT a.run_id::text, a.created_at, a.raw_content, f.verdict
            FROM agent_outputs a
            LEFT JOIN final_reports f ON f.run_id = a.run_id AND f.ticker = a.ticker
            WHERE a.ticker = %s AND a.agent_type = 'BUY_CASE'
        '''
        if run_id:
            cur.execute(base + " AND a.run_id = %s ORDER BY a.created_at DESC LIMIT 1",
                        (ticker, run_id))
        else:
            cur.execute(base + " ORDER BY a.created_at DESC LIMIT 1", (ticker,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            where = f"in run {run_id}" if run_id else "on record"
            return json.dumps({"error": (
                f"No BUY_CASE found for {ticker} {where}. A buy case is written only "
                f"for a report whose verdict is 'Watch' — run the analysis pipeline "
                f"first, or `python buy_case.py {ticker}` against an existing Watch "
                f"report.")})
        return json.dumps({
            "ticker": ticker,
            "run_id": row[0],
            "created_at": row[1].isoformat() if row[1] else None,
            "buy_conditions": row[2],
            "run_verdict": (row[3] or "").upper() or None,
        })
    except Exception as e:
        return json.dumps({"error": f"Error fetching buy case for {ticker}: {str(e)}"})


@mcp.tool()
def db_get_agent_output(ticker: str, agent_type: str, run_id: str = "") -> str:
    """
    Fetch one stored agent output (BEAR_CASE, BULL_CASE, SALE_CASE, BUY_CASE,
    SEC_DATA, QUANT_METRICS, CRITIC_REVIEW) for a ticker. With `run_id`, returns that run's
    output; otherwise the most recent one. Returns JSON with run_id, created_at and
    raw_content, or {"found": false}.

    The general form of db_get_sale_case, added for the refinement loop, which needs
    the bear and bull cases the analyst was actually given in order to check the
    report's summaries of them against the originals. db_get_sale_case is left as it
    is: --sell-check depends on its exact error-string shape.
    """
    try:
        ticker = ticker.strip().upper()
        run_id = (run_id or "").strip()
        conn = get_db_connection()
        cur = conn.cursor()
        if run_id:
            cur.execute('''
                SELECT run_id::text, created_at, raw_content
                FROM agent_outputs
                WHERE ticker = %s AND agent_type = %s AND run_id = %s
                ORDER BY created_at DESC LIMIT 1
            ''', (ticker, agent_type, run_id))
        else:
            cur.execute('''
                SELECT run_id::text, created_at, raw_content
                FROM agent_outputs
                WHERE ticker = %s AND agent_type = %s
                ORDER BY created_at DESC LIMIT 1
            ''', (ticker, agent_type))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return json.dumps({"found": False})
        return json.dumps({
            "found": True,
            "ticker": ticker,
            "agent_type": agent_type,
            "run_id": row[0],
            "created_at": row[1].isoformat() if row[1] else None,
            "raw_content": row[2],
        })
    except Exception as e:
        return json.dumps({"found": False, "error": f"Error fetching {agent_type} for {ticker}: {str(e)}"})


@mcp.tool()
def db_get_final_report(ticker: str, run_id: str = "") -> str:
    """
    Fetch a stored final report for a ticker. With `run_id`, that specific run's
    report; otherwise the most recent one. Returns JSON with run_id, verdict,
    markdown_report, created_at and age_hours, or {"found": false}.

    Backs the refinement loop, which critiques an ALREADY-PRODUCED report rather than
    generating one. Unlike db_find_reusable_report there is no analysis_key match and
    no age limit: the caller has named the report it wants to refine, and refusing to
    load a 30-hour-old one would just mean paying for a full pipeline re-run first.
    """
    try:
        ticker = ticker.strip().upper()
        run_id = (run_id or "").strip()
        conn = get_db_connection()
        cur = conn.cursor()
        if run_id:
            cur.execute('''
                SELECT run_id::text, verdict, markdown_report, created_at,
                       EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0
                FROM final_reports
                WHERE ticker = %s AND run_id = %s
                ORDER BY created_at DESC LIMIT 1
            ''', (ticker, run_id))
        else:
            cur.execute('''
                SELECT run_id::text, verdict, markdown_report, created_at,
                       EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0
                FROM final_reports
                WHERE ticker = %s
                ORDER BY created_at DESC LIMIT 1
            ''', (ticker,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return json.dumps({"found": False})
        return json.dumps({
            "found": True,
            "ticker": ticker,
            "run_id": row[0],
            "verdict": row[1],
            "markdown_report": row[2],
            "created_at": row[3].isoformat() if row[3] else None,
            "age_hours": round(float(row[4]), 1) if row[4] is not None else None,
        })
    except Exception as e:
        return json.dumps({"found": False, "error": f"Error fetching final report for {ticker}: {str(e)}"})


@mcp.tool()
def db_store_critic_findings(ticker: str, source_run_id: str, refine_run_id: str,
                             iteration: int, findings_json: str) -> str:
    """
    Store one round of critic findings as long-term memory (critic_memory).

    `findings_json` is a JSON array of objects with severity / type / title /
    finding. Rows land as status 'OPEN' and are settled later by
    db_resolve_critic_findings once the refinement session ends, so an interrupted
    session leaves its findings visibly unresolved rather than silently closed.
    """
    try:
        findings = json.loads(findings_json) if findings_json else []
        if not findings:
            return f"No critic findings to store for {ticker} (round {iteration})"
        conn = get_db_connection()
        cur = conn.cursor()
        for f in findings:
            cur.execute('''
                INSERT INTO critic_memory
                    (ticker, source_run_id, refine_run_id, iteration, severity,
                     finding_type, title, finding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                ticker.strip().upper(),
                (source_run_id or "").strip() or None,
                (refine_run_id or "").strip() or None,
                int(iteration),
                (f.get("severity") or "UNKNOWN")[:16],
                (f.get("type") or "")[:80] or None,
                f.get("title"),
                f.get("finding") or "",
            ))
        conn.commit()
        cur.close()
        conn.close()
        return f"Stored {len(findings)} critic finding(s) for {ticker} (round {iteration})"
    except Exception as e:
        logger.error(f"Error storing critic findings for {ticker}: {e}")
        return f"Error storing critic findings: {str(e)}"


@mcp.tool()
def db_record_analyst_response(refine_run_id: str, ticker: str, iteration: int,
                               response: str) -> str:
    """
    Attach the analyst's point-by-point reply to the findings of one review round.

    Stored against the findings it answers so a later session can see not just what
    was objected to but how it ended — a finding the analyst REBUTTED and the critic
    accepted must not come back, and without the reply there is nothing on record to
    say it was ever answered.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            UPDATE critic_memory SET analyst_response = %s
            WHERE refine_run_id = %s AND ticker = %s AND iteration = %s
        ''', (response, (refine_run_id or "").strip() or None,
              ticker.strip().upper(), int(iteration)))
        n = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return f"Recorded analyst response against {n} finding(s) for {ticker} (round {iteration})"
    except Exception as e:
        logger.error(f"Error recording analyst response for {ticker}: {e}")
        return f"Error recording analyst response: {str(e)}"


@mcp.tool()
def db_resolve_critic_findings(refine_run_id: str, ticker: str, status: str) -> str:
    """
    Settle every still-OPEN finding from one refinement session.

    'RESOLVED' when the critic agreed the final report, 'UNRESOLVED' when the session
    ended on the budget or round ceiling with objections standing. The distinction is
    what the next session reads: an UNRESOLVED finding is a live objection to raise
    again, a RESOLVED one is settled and must not be.
    """
    try:
        normalized = (status or "").strip().upper()
        if normalized not in ("RESOLVED", "UNRESOLVED"):
            normalized = "UNRESOLVED"
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            UPDATE critic_memory SET status = %s
            WHERE refine_run_id = %s AND ticker = %s AND status = 'OPEN'
        ''', (normalized, (refine_run_id or "").strip() or None, ticker.strip().upper()))
        n = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return f"Marked {n} finding(s) {normalized} for {ticker}"
    except Exception as e:
        logger.error(f"Error resolving critic findings for {ticker}: {e}")
        return f"Error resolving critic findings: {str(e)}"


@mcp.tool()
def db_get_critic_memory(ticker: str, limit: int = 25, max_age_days: int = 365) -> str:
    """
    Load a ticker's stored critic findings, newest first, as a JSON array.

    Retrieved by exact ticker and recency rather than by vector similarity, and
    deliberately so: the retrieval key here is known exactly (this company's own
    review history), semantic search would return approximate neighbours where an
    exact answer exists, and every embedded row would add an embedding call to a loop
    whose whole design constraint is cost. `db_search_historical_reports` remains the
    right tool for the fuzzy cross-company question.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            SELECT created_at, iteration, severity, finding_type, title, finding,
                   analyst_response, status, refine_run_id::text, source_run_id::text
            FROM critic_memory
            WHERE ticker = %s AND created_at > NOW() - (%s || ' days')::interval
            ORDER BY created_at DESC
            LIMIT %s
        ''', (ticker.strip().upper(), str(int(max_age_days)), int(limit)))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return json.dumps([
            {
                "created_at": r[0].isoformat() if r[0] else None,
                "iteration": r[1],
                "severity": r[2],
                "finding_type": r[3],
                "title": r[4],
                "finding": r[5],
                "analyst_response": r[6],
                "status": r[7],
                "refine_run_id": r[8],
                "source_run_id": r[9],
            }
            for r in rows
        ])
    except Exception as e:
        logger.error(f"Error loading critic memory for {ticker}: {e}")
        return json.dumps([])


if __name__ == "__main__":
    mcp.run()
