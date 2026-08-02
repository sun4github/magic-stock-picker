"""
Magic Stock Picker — Report Viewer
A small Flask web app (designed to run on a Raspberry Pi) that reads the pipeline's
PostgreSQL database. It supports two ways to browse the same stored reports:

  1. By ticker  — pick a ticker, pick one of its runs, read the Bear / Bull /
     Final reports (rendered markdown) with a download option.
  2. By pipeline run — pick a run (newest first), see every ticker analyzed in
     that run with its Buy / Watch / Avoid recommendation, drill into any
     ticker's reports, and download the whole run's decisions as CSV.

Reads DATABASE_URL from a local .env file (see .env.example). Read-only.
"""
import csv
import io
import os
import re

import markdown
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request

load_dotenv()  # loads webapp/.env

DATABASE_URL = os.getenv("DATABASE_URL")
MD_EXTENSIONS = ["extra", "sane_lists"]  # tables, fenced code, nice lists

app = Flask(__name__)


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and set your database URL."
        )
    return psycopg2.connect(DATABASE_URL)


def render_md(text: str) -> str:
    """Render stored markdown to HTML. Content is the pipeline's own trusted output."""
    if not text:
        return "<p class='muted'><em>Not available for this run.</em></p>"
    return markdown.markdown(text, extensions=MD_EXTENSIONS)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tickers")
def api_tickers():
    """All tickers that have at least one run, alphabetical. Optional ?q= substring
    filter (the UI uses a full alphabetical datalist; q supports 3-char search)."""
    q = (request.args.get("q") or "").strip().upper()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if q:
        cur.execute(
            """SELECT ticker, MAX(company_name) AS company_name
               FROM ticker_runs WHERE ticker ILIKE %s
               GROUP BY ticker ORDER BY ticker""",
            (f"%{q}%",),
        )
    else:
        cur.execute(
            """SELECT ticker, MAX(company_name) AS company_name
               FROM ticker_runs GROUP BY ticker ORDER BY ticker"""
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/runs")
def api_runs():
    """Runs for a ticker, newest first.

    Joins pipeline_runs so a critic refinement is identifiable in the picker itself.
    Without that, a refinement is indistinguishable from an ordinary run until you
    open it — and since it is the NEWEST run for the ticker it is what the picker
    selects by default, so the distinction has to be visible before the click."""
    ticker = (request.args.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify([])
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT tr.run_id::text AS run_id, tr.run_date, tr.verdict, tr.company_name,
                  pr.refines_run_id::text AS refines_run_id,
                  CASE
                      WHEN pr.refines_run_id IS NULL THEN NULL
                      WHEN UPPER(pr.status) = 'COMPLETED' THEN 'agreed'
                      ELSE 'not_agreed'
                  END AS critic_status
           FROM ticker_runs tr
           LEFT JOIN pipeline_runs pr ON pr.run_id = tr.run_id
           WHERE tr.ticker = %s ORDER BY tr.run_date DESC""",
        (ticker,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["run_date"] = d["run_date"].isoformat() if d["run_date"] else None
        out.append(d)
    return jsonify(out)


@app.route("/api/pipeline-runs")
def api_pipeline_runs():
    """Multi-ticker pipeline runs, newest first. One row per run with its date and
    a verdict breakdown, so the run picker can show 'N tickers' at a glance.

    Single-ticker runs are on-demand one-offs (not value-discovery screens), so
    they are excluded here (HAVING COUNT(*) > 1) per specs/webapp.feature."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT run_id::text AS run_id,
                  MAX(run_date) AS run_date,
                  COUNT(*) AS ticker_count,
                  COUNT(*) FILTER (WHERE UPPER(verdict) = 'BUY')   AS buy_count,
                  COUNT(*) FILTER (WHERE UPPER(verdict) = 'WATCH') AS watch_count,
                  COUNT(*) FILTER (WHERE UPPER(verdict) = 'AVOID') AS avoid_count
           FROM ticker_runs
           GROUP BY run_id
           HAVING COUNT(*) > 1
           ORDER BY MAX(run_date) DESC"""
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["run_date"] = d["run_date"].isoformat() if d["run_date"] else None
        out.append(d)
    return jsonify(out)


# Reading order for a run's decisions: the actionable names first, the rejects last.
# Anything outside the Buy/Watch/Avoid vocabulary (legacy HOLD/SELL rows, or a NULL
# verdict from an interrupted run) sorts after all three rather than silently landing
# among the Buys.
_VERDICT_ORDER_SQL = """
    CASE UPPER(COALESCE(verdict, ''))
        WHEN 'BUY'   THEN 1
        WHEN 'WATCH' THEN 2
        WHEN 'AVOID' THEN 3
        ELSE 4
    END
"""


def _fetch_run_tickers(run_id: str):
    """Return every ticker in one pipeline run, ordered for reading, with its verdict.

    Buy first, then Watch, then Avoid; within each group by Magic Formula rank
    (1 = best). Runs recorded before the rank was stored have NULL magic_rank — those
    sort last within their group and fall back to alphabetical, so an older run still
    reads sensibly instead of coming back in arbitrary order."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        f"""SELECT ticker, company_name, verdict, run_date, magic_rank
            FROM ticker_runs WHERE run_id = %s
            ORDER BY {_VERDICT_ORDER_SQL}, magic_rank ASC NULLS LAST, ticker""",
        (run_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.route("/api/pipeline-run")
def api_pipeline_run():
    """Tickers analyzed in a single pipeline run, alphabetical, each with its
    Buy/Watch/Avoid recommendation (drives the per-run decisions table)."""
    run_id = request.args.get("run_id")
    if not run_id:
        return jsonify([])
    rows = _fetch_run_tickers(run_id)
    out = []
    for r in rows:
        d = dict(r)
        d["run_date"] = d["run_date"].isoformat() if d["run_date"] else None
        out.append(d)
    return jsonify(out)


@app.route("/download-run")
def download_run():
    """Download every ticker + recommendation for one pipeline run as CSV."""
    run_id = request.args.get("run_id")
    if not run_id:
        abort(400)
    rows = _fetch_run_tickers(run_id)
    if not rows:
        abort(404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    # Rows come out of _fetch_run_tickers already in Buy/Watch/Avoid then rank order,
    # so the download reads the same way the on-screen list does.
    writer.writerow(["Ticker", "Company", "Recommendation", "MagicFormulaRank"])
    for r in rows:
        writer.writerow(
            [r["ticker"], r["company_name"] or "", (r["verdict"] or "").upper(),
             r["magic_rank"] if r["magic_rank"] is not None else ""]
        )
    run_date = rows[0]["run_date"]
    stamp = run_date.strftime("%Y%m%d") if run_date else "run"
    fname = f"pipeline_run_{stamp}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


_CASE_TYPES = ("BEAR_CASE", "BULL_CASE", "SALE_CASE")

_HEADING_RE = re.compile(r"^(#{1,5})(?=\s)", re.MULTILINE)


def _demote_headings(markdown_text: str, levels: int = 1) -> str:
    """Push every heading down `levels` so an inlined document nests under its own
    container heading instead of tying with it. Mirrors refine.py's version, which
    does the same job when the critic's review is inlined into a stored report."""
    return _HEADING_RE.sub(
        lambda m: "#" * min(len(m.group(1)) + levels, 6), markdown_text or ""
    )


def _refinement_of(cur, run_id: str):
    """(source_run_id, critic_status) for a run, or (None, None) if it is not a
    refinement.

    `pipeline_runs.refines_run_id` being non-NULL IS the test — the critic loop is
    the only thing that sets it, so there is no separate flag that could disagree.
    The terminal status carries the outcome: the loop finalizes COMPLETED only when
    the critic agreed, and NOT_AGREED / BUDGET_EXCEEDED when it did not."""
    cur.execute(
        "SELECT refines_run_id::text, status FROM pipeline_runs WHERE run_id = %s",
        (run_id,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None, None
    return row[0], ("agreed" if (row[1] or "").upper() == "COMPLETED" else "not_agreed")


def _borrowed_note(source_run_id: str) -> str:
    """Header stamped onto a case borrowed from the run under review.

    The refinement never re-ran the bear/bull research — it reviewed a report built
    from it — so presenting these tabs unlabelled would imply work this run did not
    do, and would date the research to the refinement rather than to the analysis."""
    return (
        f"> **From the reviewed run** `{source_run_id}`. The critic review did not "
        f"re-run this research; it examined the report built from it.\n\n"
    )


def _fetch_reports(run_id: str, ticker: str):
    """Return every report body for one run/ticker, plus its refinement standing.

    Returns a dict rather than the old 5-tuple: there are now six bodies and two
    pieces of run metadata, and a positional tuple that long is a bug waiting to
    happen at each call site."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT agent_type, raw_content FROM agent_outputs
           WHERE run_id = %s AND ticker = %s AND agent_type = ANY(%s)""",
        (run_id, ticker, list(_CASE_TYPES)),
    )
    parts = {t: c for t, c in cur.fetchall()}

    # Every critic round, oldest first, so the tab reads as the argument progressed.
    # The stored final report carries only the LAST review, and only when the critic
    # never agreed, so this tab is the only place the full exchange is visible.
    cur.execute(
        """SELECT COALESCE(metadata->>'iteration', '?'), raw_content
           FROM agent_outputs
           WHERE run_id = %s AND ticker = %s AND agent_type = 'CRITIC_REVIEW'
           ORDER BY created_at ASC""",
        (run_id, ticker),
    )
    rounds = cur.fetchall()
    # Each review is a standalone document whose own sections are '##'. Stacked under
    # a '## Review round N' heading unchanged they would tie with their container, so
    # the tab's outline would read as one flat run of sections with no indication of
    # where one round ends and the next begins. Demote by one so they nest.
    critic = "\n\n---\n\n".join(
        f"## Review round {n}\n\n{_demote_headings(body)}" for n, body in rounds
    )

    source_run_id, critic_status = _refinement_of(cur, run_id)
    # A refinement run holds only its own critic reviews, so its Bull/Bear/Sale tabs
    # would be empty. Borrow them from the run it reviewed rather than copying them
    # at refine time — a copy would duplicate ~26KB of text and two 768-dim vectors
    # per refinement to say nothing new.
    if source_run_id and not all(parts.get(t) for t in _CASE_TYPES):
        cur.execute(
            """SELECT agent_type, raw_content FROM agent_outputs
               WHERE run_id = %s AND ticker = %s AND agent_type = ANY(%s)""",
            (source_run_id, ticker, list(_CASE_TYPES)),
        )
        for t, content in cur.fetchall():
            if not parts.get(t) and content:
                parts[t] = _borrowed_note(source_run_id) + content

    cur.execute(
        "SELECT markdown_report, verdict FROM final_reports WHERE run_id = %s AND ticker = %s",
        (run_id, ticker),
    )
    fr = cur.fetchone()
    cur.close()
    conn.close()
    return {
        "bear": parts.get("BEAR_CASE", ""),
        "bull": parts.get("BULL_CASE", ""),
        "sale": parts.get("SALE_CASE", ""),
        "critic": critic,
        "final": fr[0] if fr else "",
        "verdict": fr[1] if fr else "",
        "source_run_id": source_run_id,
        "critic_status": critic_status,
        "critic_rounds": len(rounds),
    }


@app.route("/api/report")
def api_report():
    run_id = request.args.get("run_id")
    ticker = (request.args.get("ticker") or "").strip().upper()
    if not run_id or not ticker:
        abort(400)
    r = _fetch_reports(run_id, ticker)
    payload = {
        "ticker": ticker,
        "verdict": r["verdict"],
        # Whether this run was a critic refinement, and how it ended. None for an
        # ordinary analysis run, which is what the UI keys its badge off.
        "critic_status": r["critic_status"],
        "critic_rounds": r["critic_rounds"],
        "source_run_id": r["source_run_id"],
    }
    for kind in ("bear", "bull", "sale", "critic", "final"):
        payload[f"{kind}_html"] = render_md(r[kind])
        payload[f"has_{kind}"] = bool(r[kind])
    return jsonify(payload)


@app.route("/download")
def download():
    """Download a report's raw markdown to the viewing device."""
    run_id = request.args.get("run_id")
    ticker = (request.args.get("ticker") or "").strip().upper()
    kind = (request.args.get("kind") or "final").lower()  # bear|bull|sale|critic|final
    if not run_id or not ticker:
        abort(400)
    r = _fetch_reports(run_id, ticker)
    text = r.get(kind) or r["final"]
    if not text:
        abort(404)
    verdict = r["verdict"]
    label = {
        "bear": "Bear_Case",
        "bull": "Bull_Case",
        "sale": "Sale_Advisory",
        "critic": "Critic_Review",
        "final": f"Final_Report_{(verdict or 'NA').title()}",
    }.get(kind, "Report")
    fname = f"{ticker}_{label}.md"
    return Response(
        text,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


if __name__ == "__main__":
    # host=0.0.0.0 so the app is reachable from other devices on the Pi's network.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
