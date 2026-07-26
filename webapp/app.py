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
    """Runs for a ticker, newest first."""
    ticker = (request.args.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify([])
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT run_id::text AS run_id, run_date, verdict, company_name
           FROM ticker_runs WHERE ticker = %s ORDER BY run_date DESC""",
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


def _fetch_run_tickers(run_id: str):
    """Return every ticker in one pipeline run, alphabetical, with its verdict."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT ticker, company_name, verdict, run_date
           FROM ticker_runs WHERE run_id = %s ORDER BY ticker""",
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
    writer.writerow(["Ticker", "Company", "Recommendation"])
    for r in rows:
        writer.writerow(
            [r["ticker"], r["company_name"] or "", (r["verdict"] or "").upper()]
        )
    run_date = rows[0]["run_date"]
    stamp = run_date.strftime("%Y%m%d") if run_date else "run"
    fname = f"pipeline_run_{stamp}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _fetch_reports(run_id: str, ticker: str):
    """Return (bear_md, bull_md, sale_md, final_md, verdict) for one run/ticker."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT agent_type, raw_content FROM agent_outputs
           WHERE run_id = %s AND ticker = %s
             AND agent_type IN ('BEAR_CASE', 'BULL_CASE', 'SALE_CASE')""",
        (run_id, ticker),
    )
    parts = {t: c for t, c in cur.fetchall()}
    cur.execute(
        "SELECT markdown_report, verdict FROM final_reports WHERE run_id = %s AND ticker = %s",
        (run_id, ticker),
    )
    fr = cur.fetchone()
    cur.close()
    conn.close()
    final_md = fr[0] if fr else ""
    verdict = fr[1] if fr else ""
    return (
        parts.get("BEAR_CASE", ""),
        parts.get("BULL_CASE", ""),
        parts.get("SALE_CASE", ""),
        final_md,
        verdict,
    )


@app.route("/api/report")
def api_report():
    run_id = request.args.get("run_id")
    ticker = (request.args.get("ticker") or "").strip().upper()
    if not run_id or not ticker:
        abort(400)
    bear, bull, sale, final, verdict = _fetch_reports(run_id, ticker)
    return jsonify(
        {
            "ticker": ticker,
            "verdict": verdict,
            "bear_html": render_md(bear),
            "bull_html": render_md(bull),
            "sale_html": render_md(sale),
            "final_html": render_md(final),
            "has_bear": bool(bear),
            "has_bull": bool(bull),
            "has_sale": bool(sale),
            "has_final": bool(final),
        }
    )


@app.route("/download")
def download():
    """Download a report's raw markdown to the viewing device."""
    run_id = request.args.get("run_id")
    ticker = (request.args.get("ticker") or "").strip().upper()
    kind = (request.args.get("kind") or "final").lower()  # bear|bull|sale|final
    if not run_id or not ticker:
        abort(400)
    bear, bull, sale, final, verdict = _fetch_reports(run_id, ticker)
    text = {"bear": bear, "bull": bull, "sale": sale, "final": final}.get(kind, final)
    if not text:
        abort(404)
    label = {
        "bear": "Bear_Case",
        "bull": "Bull_Case",
        "sale": "Sale_Advisory",
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
