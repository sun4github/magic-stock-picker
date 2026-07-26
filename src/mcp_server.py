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
    calculate_company_metrics,
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
        metrics = calculate_company_metrics(ticker, live_cap, api_key)
        if not metrics:
            return json.dumps({"error": f"Could not compute ROC/Earnings Yield for {ticker}"})
        return json.dumps({
            "Symbol": ticker,
            "CompanyName": metrics.get("CompanyName", ticker),
            "ROC_Pct": f"{round(metrics['ROC'] * 100, 2)}%",
            "EY_Pct": f"{round(metrics['EarningsYield'] * 100, 2)}%",
            "Final_Rank": None,
            "MagicFormula_Score": None,
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
            "key_metrics_3y": key_metrics_3y,
            "ratios_5y": ratios_5y,
            "pe_5y_average": pe_5y_average,
            "ratings_snapshot": ratings_snapshot,
            "price_target_consensus": price_target,
            "grades_consensus": grades,
            "competitors": peers,
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error fetching FMP metrics for {ticker}: {str(e)}"

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
                UNIQUE (ticker, run_id)
            )
        ''')
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

def get_embedding(text: str) -> List[float]:
    try:
        import google.generativeai as genai
        google_api_key = os.getenv("GOOGLE_API_KEY")
        if google_api_key:
            genai.configure(api_key=google_api_key)
            result = genai.embed_content(
                model="models/text-embedding-004",
                content=text[:10000]
            )
            return result['embedding']
    except Exception:
        pass
    # Fallback dummy embedding if API key missing or error
    return [0.0] * 768

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
def db_store_final_report(run_id: str, ticker: str, verdict: str, markdown_report: str) -> str:
    """
    Stores final report in PostgreSQL database with vector embeddings.
    The verdict is normalized to the uppercase BUY/WATCH/AVOID values allowed by
    the final_reports CHECK constraint (WATCH is the neutral default).
    """
    try:
        normalized = (verdict or "").strip().upper()
        if normalized not in ("BUY", "WATCH", "AVOID"):
            normalized = "WATCH"
        embedding = get_embedding(markdown_report)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO final_reports (run_id, ticker, verdict, markdown_report, embedding)
            VALUES (%s, %s, %s, %s, %s)
        ''', (run_id, ticker, normalized, markdown_report, embedding))
        conn.commit()
        cur.close()
        conn.close()
        return f"Successfully stored final report for {ticker} with verdict {normalized}"
    except Exception as e:
        logger.error(f"Error storing final report for {ticker}: {e}")
        return f"Error storing final report: {str(e)}"

@mcp.tool()
def db_store_ticker_run(run_id: str, ticker: str, company_name: str, verdict: str) -> str:
    """
    Records a per-ticker index row in ticker_runs (ticker -> run + verdict + date).
    This drives the web UI's ticker picker and run list. Idempotent per (ticker, run_id).
    """
    try:
        normalized = (verdict or "").strip().upper()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO ticker_runs (ticker, run_id, company_name, verdict)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (ticker, run_id)
            DO UPDATE SET company_name = EXCLUDED.company_name, verdict = EXCLUDED.verdict
        ''', (ticker, run_id, company_name, normalized))
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
