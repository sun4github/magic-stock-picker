"""The verdict ledger: what each verdict claimed, for grading later.

Write-only from the pipeline's point of view — nothing here is read back into a
prompt and no agent has any of it in its `tools=[...]`. Reading and scoring happen
in cron/run_outcome_snapshot.py, which deliberately does not import this package;
see the note there.

Every failure below is logged and swallowed. A ledger write must never be able to
fail a run: the analysis is the product, and losing one row of bookkeeping is not
worth discarding a report that was paid for.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import json

from mcp_app import logger, mcp

from .db import get_db_connection


# ===========================================================================
# THE VERDICT LEDGER
#
# Write-only from the pipeline's point of view: nothing here is read back into
# a prompt, and no agent has any of these in its `tools=[...]`. The ledger
# records what each verdict claimed and what the stock did next, so the system
# can eventually be graded on realised outcomes instead of on its own
# confidence. Reading and scoring happen in cron/run_outcome_snapshot.py, which
# deliberately does not import this module — see the note there.
#
# Every failure below is logged and swallowed. A ledger write must never be
# able to fail a run: the analysis is the product, and losing one row of
# bookkeeping is not worth discarding a report that was paid for.
# ===========================================================================

def _num(value):
    """Coerce a screener/DataFrame cell to a float, or None.

    The ranking CSV carries `Live_Price` as a display string ("$44.03"), pandas
    NaN for any metric that could not be computed, and percentage columns as
    plain floats. All three reach here, and a NaN inserted into a NUMERIC column
    becomes the literal 'NaN' rather than NULL, which then poisons every average
    computed over that column."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").replace("%", "").strip()
        if not value:
            return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


@mcp.tool()
def db_log_run_universe(run_id: str, rows_json: str) -> str:
    """
    Record every candidate that cleared the screen, with its point-in-time figures.

    `rows_json` is a JSON array of the ranking DataFrame's rows — the column
    names are the CSV's own (Final_Rank, Symbol, Live_Price, LiveMarketCap,
    ROC_Pct, EY_Pct, PE_Ratio, PEG, EPSGrowth_Pct, EBIT_Basis, BalanceSheetDate).

    Costs nothing: the screener has already computed all of it. Without this the
    figures are unrecoverable after the fact — magic_formula_rankings_live.csv is
    overwritten on the next run, and pipeline_runs.top_30_tickers holds bare
    symbols. It is also the control group for the only question the ledger can
    answer within a year: did filtering the screen beat not filtering it?

    ON CONFLICT DO NOTHING so a re-run of the same screen is idempotent rather
    than a duplicate-key failure.
    """
    try:
        rows = json.loads(rows_json) if rows_json else []
        if not rows:
            return "No universe rows to log."
        conn = get_db_connection()
        cur = conn.cursor()
        written = 0
        for r in rows:
            ticker = (r.get("Symbol") or "").strip().upper()
            if not ticker:
                continue
            rank = _num(r.get("Final_Rank"))
            cur.execute('''
                INSERT INTO run_universe (
                    run_id, ticker, company_name, screened_on, magic_rank, price,
                    market_cap, roc_pct, earnings_yield_pct, pe_ratio, peg,
                    eps_growth_pct, ebit_basis, balance_sheet_date
                ) VALUES (%s, %s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s)
                ON CONFLICT (run_id, ticker) DO NOTHING
            ''', (
                run_id or None, ticker, r.get("CompanyName"),
                int(rank) if rank is not None else None,
                _num(r.get("Live_Price")), _num(r.get("LiveMarketCap")),
                _num(r.get("ROC_Pct")), _num(r.get("EY_Pct")),
                _num(r.get("PE_Ratio")), _num(r.get("PEG")),
                _num(r.get("EPSGrowth_Pct")), r.get("EBIT_Basis"),
                r.get("BalanceSheetDate") or None,
            ))
            written += cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return f"Logged {written} universe row(s) for run {run_id}."
    except Exception as e:
        logger.error(f"Error logging run universe for {run_id}: {e}")
        return f"Error logging run universe: {str(e)}"


@mcp.tool()
def db_log_verdict(run_id: str, ticker: str, verdict: str, basis_json: str,
                   company_name: str = "", verdict_stage: str = "INDEPENDENT",
                   price: float = 0.0, market_cap: float = 0.0,
                   shares_outstanding: float = 0.0, sector: str = "",
                   industry: str = "", magic_rank: int = 0,
                   benchmark_symbol: str = "", benchmark_price: float = 0.0,
                   prompt_version: str = "", git_sha: str = "",
                   analysis_key: str = "") -> str:
    """
    Record one FRESH verdict plus the factors behind it. Returns the verdict_id.

    Only ever called on the fresh-analysis path. The reuse path
    (db_copy_ticker_outputs) must not reach here: copying a prior report into a
    new run re-states an existing judgement rather than making a new one, and
    logging it again would enter the same observation into the track record
    twice. One heavily re-run ticker would then dominate every base rate
    computed from the ledger — FISV alone has 15 stored reports.

    `basis_json` is a JSON array of {factor_key, stance, materiality,
    observation}. factor_key should come from the controlled vocabulary at
    specs/config.yaml `verdict_ledger.factor_taxonomy`; anything else is stored
    as given (prefixed OTHER_ by the caller) for periodic review rather than
    rejected, because losing an observation is worse than an untidy key.

    Observations are NOT embedded here. The column exists and Phase 1 does not
    use it — base rates aggregate on factor_key, not on vector similarity — and
    embedding ~6 factors per ticker would roughly double the run's embedding
    spend for a lookup nothing performs yet. The text is stored, so backfilling
    embeddings later is a batch job over rows that already exist.
    """
    try:
        ticker = (ticker or "").strip().upper()
        verdict = (verdict or "").strip().upper()
        if verdict not in ("BUY", "WATCH", "AVOID"):
            # Legacy HOLD/SELL and anything unparsed stay out of the ledger
            # rather than being coerced into a bucket they do not belong in —
            # a wrong label is worse for a track record than a missing row.
            logger.warning(f"[{ticker}] Verdict '{verdict}' is not ledger-eligible; not logged.")
            return ""

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO verdict_log (
                run_id, ticker, company_name, verdict_date, verdict, verdict_stage,
                price, market_cap, shares_outstanding, sector, industry, magic_rank,
                benchmark_symbol, benchmark_price, prompt_version, git_sha, analysis_key
            ) VALUES (%s, %s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s)
            RETURNING verdict_id
        ''', (
            run_id or None, ticker, company_name or None, verdict,
            (verdict_stage or "INDEPENDENT").strip().upper(),
            _num(price), _num(market_cap), _num(shares_outstanding),
            sector or None, industry or None,
            int(magic_rank) if magic_rank else None,
            (benchmark_symbol or "").strip().upper() or None, _num(benchmark_price),
            prompt_version or None, git_sha or None, analysis_key or None,
        ))
        verdict_id = cur.fetchone()[0]

        basis = json.loads(basis_json) if basis_json else []
        stored = 0
        for f in basis:
            key = (f.get("factor_key") or "").strip()
            observation = (f.get("observation") or "").strip()
            if not key or not observation:
                continue
            stance = (f.get("stance") or "").strip().upper()
            if stance not in ("BULL", "BEAR"):
                continue
            materiality = (f.get("materiality") or "").strip().upper() or None
            if materiality not in ("HIGH", "MEDIUM", "LOW", None):
                materiality = None
            cur.execute('''
                INSERT INTO verdict_basis (verdict_id, factor_key, stance,
                                           materiality, observation)
                VALUES (%s, %s, %s, %s, %s)
            ''', (verdict_id, key, stance, materiality, observation))
            stored += 1

        # The universe row and the verdict are written by different phases, so
        # the flag is set here rather than at insert time: until the agents have
        # actually reached a ticker, "analyzed" is not yet true. It distinguishes
        # "the agents passed on it" from "the run died before getting to it",
        # which are opposite facts when measuring what the filter did.
        if run_id:
            cur.execute("UPDATE run_universe SET analyzed = TRUE "
                        "WHERE run_id = %s AND ticker = %s", (run_id, ticker))

        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"[{ticker}] Ledger: logged {verdict} ({verdict_stage}) "
                    f"with {stored} factor(s).")
        return str(verdict_id)
    except Exception as e:
        logger.error(f"Error logging verdict for {ticker}: {e}")
        return ""
