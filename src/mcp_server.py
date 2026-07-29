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
        roic = metrics.get("ROIC_InclGoodwill")
        intang_share = metrics.get("IntangiblesShareOfAssets")
        roa = metrics.get("ROA")
        roa_ni = metrics.get("ROA_NetIncome")
        pe = metrics.get("PE")
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
            "NetIncome": metrics.get("NetIncome"),
            "EBIT_Basis": metrics.get("EBIT_Basis"),
            "EBIT": metrics.get("EBIT"),
            "CapitalEmployed": metrics.get("CapitalEmployed"),
            "EnterpriseValue": metrics.get("EnterpriseValue"),
            "LiveMarketCap": live_cap,
            "Final_Rank": None,
            "MagicFormula_Score": None,
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
def fmp_metrics_extractor(ticker: str) -> str:
    """
    Fetches 3-year metric trends, 5-year P/E history/average, analyst consensus
    targets, and competitor data from Financial Modeling Prep.

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
        peers = _get("stock-peers", symbol=ticker)                     # replaces legacy /v4/stock_peers

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
            "competitors": peers,
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
        ]:
            cur.execute(f"ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS {col_def}")

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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ticker_runs_ticker ON ticker_runs(ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ticker_runs_date ON ticker_runs(run_date DESC)")

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
# report). The orchestrator drains this counter after each ticker rather than
# threading a usage object through every db_* tool signature.
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
def db_create_pipeline_run(run_id: str, tickers: List[str]) -> str:
    """
    Creates the parent pipeline_runs row for a batch execution. This MUST be
    called before storing any agent_outputs/final_reports, because those tables
    have a foreign key onto pipeline_runs(run_id).
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO pipeline_runs (run_id, status, top_30_tickers)
            VALUES (%s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
        ''', (run_id, "RUNNING", list(tickers)))
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
    """
    if not analysis_key:
        return json.dumps({"found": False, "reason": "no analysis key"})
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            SELECT run_id, verdict, markdown_report, created_at,
                   EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0 AS age_hours
              FROM final_reports
             WHERE ticker = %s AND analysis_key = %s
               AND created_at > NOW() - (%s || ' hours')::interval
             ORDER BY created_at DESC
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
                        magic_rank: Optional[int] = None) -> str:
    """
    Records a per-ticker index row in ticker_runs (ticker -> run + verdict + date).
    This drives the web UI's ticker picker and run list. Idempotent per (ticker, run_id).

    `magic_rank` is the ticker's Final_Rank in the screen that selected it (1 = best),
    used by the UI to order a run's decisions within each Buy/Watch/Avoid group. Pass
    None for on-demand single-ticker runs, which never went through a ranking.
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
        cur.execute('''
            INSERT INTO ticker_runs (ticker, run_id, company_name, verdict, magic_rank)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (ticker, run_id)
            DO UPDATE SET company_name = EXCLUDED.company_name,
                          verdict = EXCLUDED.verdict,
                          magic_rank = EXCLUDED.magic_rank
        ''', (ticker, run_id, company_name, normalized, rank))
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


if __name__ == "__main__":
    mcp.run()
