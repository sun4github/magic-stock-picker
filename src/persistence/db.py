"""The connection, and the schema bootstrap.

Everything else in this package goes through `get_db_connection`. The schema DDL
mirrors sql-schema.sql, which stays the authoritative version.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import os

import psycopg2

from mcp_app import logger


# ==========================================
# DATABASE INITIALIZATION & TOOLS
# ==========================================
# Schema bootstrap runs ONCE per process, on the first connection that actually
# needs it — not at import. It used to run at module scope (see the note where
# `initialize_database` is defined), which meant every importer paid for a remote
# Postgres round trip plus ~350 lines of DDL before executing a single statement:
# two minutes on a cold import, charged to every test file, every cron script and
# every `--help`. Nothing is lost by deferring it, because the schema only has to
# exist before the first read or write, and that is exactly where this now fires.
_SCHEMA_READY = {"done": False}


def get_db_connection(ensure_schema: bool = True):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL not set")
    # Flag is set BEFORE the call, not after: `initialize_database` opens its own
    # connection, and without this it would recurse into itself forever. Setting it
    # first also means a failed bootstrap is not retried on every subsequent
    # connection — matching the old import-time behaviour, where a failure was
    # logged once and the process carried on.
    if ensure_schema and not _SCHEMA_READY["done"]:
        _SCHEMA_READY["done"] = True
        initialize_database()
    return psycopg2.connect(db_url)


def initialize_database():
    """Ensure the schema exists. This mirrors sql-schema.sql exactly (the
    authoritative schema): a pipeline_runs parent table referenced by
    agent_outputs and final_reports via FK, a `metadata` (not metadata_json)
    JSONB column, and a verdict CHECK constrained to BUY/SELL/HOLD.
    Uses IF NOT EXISTS so it is a safe no-op when tables already exist.

    Called lazily by `get_db_connection` on the first connection of a process;
    `ensure_schema=False` on its own connection breaks the recursion."""
    try:
        conn = get_db_connection(ensure_schema=False)
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

        # ------------------------------------------------------------------
        # The verdict ledger. See sql-schema.sql (tables 6-9) for the full
        # rationale; the short version is that everything above records what a
        # run PRODUCED, while these record what a verdict CLAIMED and what the
        # stock did afterwards, so the system can be graded on realised
        # outcomes rather than on its own confidence.
        #
        # Like critic_memory, none of these carry an FK onto pipeline_runs:
        # they are memory, and a verdict whose run was deleted is still a
        # verdict that was made.
        # ------------------------------------------------------------------
        cur.execute('''
            CREATE TABLE IF NOT EXISTS run_universe (
                universe_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                run_id UUID,
                ticker VARCHAR(10) NOT NULL,
                company_name TEXT,
                screened_on DATE NOT NULL,
                magic_rank INTEGER,
                price NUMERIC(14, 4),
                market_cap NUMERIC(20, 2),
                roc_pct NUMERIC(12, 4),
                earnings_yield_pct NUMERIC(12, 4),
                pe_ratio NUMERIC(12, 4),
                peg NUMERIC(12, 4),
                eps_growth_pct NUMERIC(12, 4),
                ebit_basis VARCHAR(16),
                balance_sheet_date DATE,
                analyzed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (run_id, ticker)
            )
        ''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_run_universe_run ON run_universe(run_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_run_universe_ticker_date "
                    "ON run_universe(ticker, screened_on DESC)")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS verdict_log (
                verdict_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                run_id UUID,
                ticker VARCHAR(10) NOT NULL,
                company_name TEXT,
                verdict_date DATE NOT NULL,
                verdict VARCHAR(10) NOT NULL
                    CHECK (verdict IN ('BUY', 'WATCH', 'AVOID')),
                verdict_stage VARCHAR(16) NOT NULL DEFAULT 'INDEPENDENT'
                    CHECK (verdict_stage IN ('INDEPENDENT', 'POST_CRITIC', 'MEMORY')),
                price NUMERIC(14, 4),
                market_cap NUMERIC(20, 2),
                shares_outstanding NUMERIC(20, 2),
                sector TEXT,
                industry TEXT,
                magic_rank INTEGER,
                benchmark_symbol VARCHAR(10),
                benchmark_price NUMERIC(14, 4),
                prompt_version VARCHAR(16),
                git_sha VARCHAR(40),
                analysis_key TEXT,
                source VARCHAR(32) NOT NULL DEFAULT 'PIPELINE',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Idempotently add `source` to a verdict_log created before it existed.
        # PIPELINE is the right default for every pre-existing row: until this
        # column shipped, the pipeline was the only thing that wrote here.
        cur.execute("ALTER TABLE verdict_log ADD COLUMN IF NOT EXISTS "
                    "source VARCHAR(32) NOT NULL DEFAULT 'PIPELINE'")
        # Set only for a row whose position was sold. NULL means still held, so
        # the snapshot job keeps marking it to market; non-NULL means the result
        # is final and the last outcome row is the one priced at the exit.
        cur.execute("ALTER TABLE verdict_log ADD COLUMN IF NOT EXISTS exit_date DATE")
        cur.execute("ALTER TABLE verdict_log ADD COLUMN IF NOT EXISTS "
                    "exit_price NUMERIC(14, 4)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_log_ticker_date "
                    "ON verdict_log(ticker, verdict_date DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_log_date "
                    "ON verdict_log(verdict_date DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_log_verdict "
                    "ON verdict_log(verdict, prompt_version)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_log_source "
                    "ON verdict_log(source, verdict_date DESC)")
        # Makes sync_external_picks.py safe to re-run: one purchase is one
        # (source, ticker, date), so a repeat sync conflicts instead of
        # double-entering it. Partial, because the pipeline legitimately logs
        # both an INDEPENDENT and a POST_CRITIC verdict for one ticker on one
        # day and must not be constrained by this.
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_verdict_log_external_dedup "
                    "ON verdict_log(source, ticker, verdict_date) WHERE source <> 'PIPELINE'")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS verdict_basis (
                basis_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                verdict_id UUID NOT NULL
                    REFERENCES verdict_log(verdict_id) ON DELETE CASCADE,
                factor_key TEXT NOT NULL,
                stance VARCHAR(8) NOT NULL CHECK (stance IN ('BULL', 'BEAR')),
                materiality VARCHAR(8) CHECK (materiality IN ('HIGH', 'MEDIUM', 'LOW')),
                observation TEXT NOT NULL,
                embedding vector(768),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_basis_factor "
                    "ON verdict_basis(factor_key, stance)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_basis_verdict "
                    "ON verdict_basis(verdict_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_basis_embedding "
                    "ON verdict_basis USING hnsw (embedding vector_cosine_ops)")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS verdict_outcome (
                outcome_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                verdict_id UUID REFERENCES verdict_log(verdict_id) ON DELETE CASCADE,
                universe_id UUID REFERENCES run_universe(universe_id) ON DELETE CASCADE,
                ticker VARCHAR(10) NOT NULL,
                as_of DATE NOT NULL,
                age_days INTEGER NOT NULL,
                price NUMERIC(14, 4),
                benchmark_symbol VARCHAR(10),
                benchmark_price NUMERIC(14, 4),
                total_return_pct NUMERIC(12, 4),
                excess_return_pct NUMERIC(12, 4),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                CHECK ((verdict_id IS NOT NULL) <> (universe_id IS NOT NULL))
            )
        ''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_outcome_verdict "
                    "ON verdict_outcome(verdict_id, as_of DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_verdict_outcome_universe "
                    "ON verdict_outcome(universe_id, as_of DESC)")
        # Partial unique indexes rather than a table constraint, because exactly
        # one of the two parent ids is set on any row. They are what makes the
        # snapshot job safe to run twice in a day: the second run conflicts
        # instead of double-counting the observation.
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_verdict_outcome_v_dedup "
                    "ON verdict_outcome(verdict_id, as_of) WHERE verdict_id IS NOT NULL")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_verdict_outcome_u_dedup "
                    "ON verdict_outcome(universe_id, as_of) WHERE universe_id IS NOT NULL")

        # Operational state for the weekly buy-check sweep. Not ledger data —
        # it exists so the sweep can tell "they have reported since we last
        # looked" from "nothing has changed", and so a trigger that fires and
        # stays fired does not re-alert every week. See sql-schema.sql table 10.
        cur.execute('''
            CREATE TABLE IF NOT EXISTS buy_check_sweep_state (
                ticker VARCHAR(10) PRIMARY KEY,
                last_swept_on DATE,
                last_price NUMERIC(14, 4),
                trigger_price NUMERIC(14, 4),
                last_earnings_seen DATE,
                last_recommendation VARCHAR(8),
                last_alerted_on DATE,
                unreadable_streak INTEGER NOT NULL DEFAULT 0,
                unreadable_alerted_on DATE,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Idempotently add the streak columns to a table created before they
        # existed, matching how pipeline_runs gained its usage columns.
        for col_def in ["unreadable_streak INTEGER NOT NULL DEFAULT 0",
                        "unreadable_alerted_on DATE"]:
            cur.execute(f"ALTER TABLE buy_check_sweep_state ADD COLUMN IF NOT EXISTS {col_def}")

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
