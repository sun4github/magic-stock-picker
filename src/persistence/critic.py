"""The critic's findings, the analyst's responses, and the memory between runs.

Kept apart from reports.py because these rows are about the refinement LOOP rather
than its product: what was challenged, what was answered, and what a later run
should remember having already argued about.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import json

from mcp_app import logger, mcp

from .db import get_db_connection


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
