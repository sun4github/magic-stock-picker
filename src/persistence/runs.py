"""Pipeline runs: creating them, tracking status, and the reuse path.

A pipeline_runs row is the parent every agent output and final report hangs off by
foreign key, so it is written before anything else in a run. `db_find_reusable_report`
and `db_copy_ticker_outputs` are the pair that make a repeat run cheap — they are
what the analysis-key fingerprint exists to make safe.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import json
from typing import List

from mcp_app import logger, mcp

from .db import get_db_connection
from .embeddings import get_embedding


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
        # BUY_CHECK is excluded on purpose. The other rows are ARTEFACTS of the
        # analysis being reused, and the new run needs its own copies or the web
        # UI lists the ticker and then fails to load anything for it. A buy check
        # is not an artefact of the analysis — it is a dated observation of
        # whether that analysis's triggers had fired on some later day, and a run
        # accumulates as many as it has been checked. Copying them would clone
        # the entire check history under a new run id every time a report is
        # reused, inflating every count taken over it and inventing checks that
        # never happened.
        cur.execute('''
            INSERT INTO agent_outputs (run_id, ticker, agent_type, raw_content, metadata, embedding)
            SELECT %s, ticker, agent_type, raw_content, metadata, embedding
              FROM agent_outputs
             WHERE run_id = %s AND ticker = %s AND agent_type <> 'BUY_CHECK'
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
