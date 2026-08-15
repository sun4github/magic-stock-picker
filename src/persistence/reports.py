"""Storing and retrieving the documents a run produces.

Final reports, per-ticker verdicts, and the getters the follow-up phases read them
back with — the sale case, the buy case, a named agent's output. Semantic search
over the stored corpus lives here too, since it reads the same rows.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import json
from typing import Optional

from mcp_app import logger, mcp

from .db import get_db_connection
from .embeddings import get_embedding


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
