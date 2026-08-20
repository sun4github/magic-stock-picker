-- Enable the pgvector extension for semantic search
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. Pipeline Runs (Tracks batch executions)
CREATE TABLE pipeline_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,     -- Set when the run finishes
    -- RUNNING -> COMPLETED, or BUDGET_EXCEEDED when a spend ceiling stopped the run
    -- early (see `budget:` in specs/config.yaml). Everything analyzed before the
    -- stop is kept, so a short run must stay distinguishable from a finished one.
    status VARCHAR(50) NOT NULL DEFAULT 'RUNNING',
    top_30_tickers TEXT[],                     -- Array of tickers identified in Phase A
    -- Aggregated LLM/search usage & estimated cost for the run
    model_requests INTEGER,
    input_tokens BIGINT,
    output_tokens BIGINT,
    total_tokens BIGINT,
    search_requests INTEGER,           -- web-search (Tavily) calls
    llm_cost_usd NUMERIC(12, 6),
    search_cost_usd NUMERIC(12, 6),
    total_cost_usd NUMERIC(12, 6),
    -- Set only by the critic refinement loop (refine.py): the run whose report this
    -- run reviewed. NULL for every ordinary pipeline run, which is also the test for
    -- "is this a refinement" — there is no separate flag that could fall out of sync
    -- with it. Paired with `status`, it tells the web UI both that a run is a
    -- refinement and how it ended (COMPLETED = the critic agreed; NOT_AGREED or
    -- BUDGET_EXCEEDED = it did not).
    --
    -- Not a foreign key: a refinement outliving the run it reviewed is a fact worth
    -- keeping, and cascading it away would delete a report someone may hold.
    refines_run_id UUID
);

-- 2. Agent Raw Outputs (Stores output from SEC, Quant, & Search agents)
CREATE TABLE agent_outputs (
    output_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    ticker VARCHAR(10) NOT NULL,
    agent_type VARCHAR(50) NOT NULL, -- 'SEC_DATA', 'QUANT_METRICS', 'BEAR_CASE', 'BULL_CASE', 'SALE_CASE'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    raw_content TEXT NOT NULL,
    metadata JSONB, -- Stores structured JSON like customer concentration %, P/E, etc.
    -- gemini-embedding-001 pinned to 768 dims via output_dimensionality (it defaults
    -- to 3072). text-embedding-004 is retired and 404s on the current API.
    embedding vector(768)
);

-- 3. Final Synthesized Reports
CREATE TABLE final_reports (
    report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    ticker VARCHAR(10) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- Verdict vocabulary is Buy/Watch/Avoid (written for a non-owner deciding whether
    -- to initiate). Legacy BUY/SELL/HOLD kept valid for historical rows.
    verdict VARCHAR(10) CHECK (verdict IN ('BUY', 'WATCH', 'AVOID', 'HOLD', 'SELL')),
    markdown_report TEXT NOT NULL,
    embedding vector(768),
    -- Fingerprint of the inputs this report was produced from:
    --   sha256(ticker | balance-sheet date | prompt version)
    -- A later run matching the same fingerprint reuses this report instead of
    -- paying to regenerate an identical analysis (see `reuse:` in config.yaml and
    -- db_find_reusable_report). Nullable: rows written before this column existed
    -- simply do not participate in reuse.
    analysis_key TEXT
);

-- 4. Ticker Runs (per-ticker index of pipeline runs; drives the web UI)
CREATE TABLE ticker_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker VARCHAR(10) NOT NULL,
    run_id UUID NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    company_name TEXT,
    verdict VARCHAR(10),
    run_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- Final_Rank from the Magic Formula screen that selected this ticker (1 = best
    -- combined ROC + Earnings Yield). Stored so the UI can order a run's decisions
    -- by conviction without re-reading the screener CSV, which is overwritten every
    -- run and so cannot answer "how was this ticker ranked back then".
    -- NULL for on-demand single-ticker runs, which never went through a ranking.
    magic_rank INTEGER,
    UNIQUE (ticker, run_id)  -- one index row per (ticker, run)
);

-- 5. Critic Memory (the independent critic's findings; long-term, cross-run)
--
-- Written by the refinement loop (refine.py), read back into BOTH the critic's and
-- the analyst's prompts on every round. It is what stops a bounded feedback loop
-- from spending its budget relitigating a point that was settled two rounds — or
-- two sessions — ago.
--
-- Deliberately has NO foreign key onto pipeline_runs, unlike every other table
-- here. The others are per-run artefacts and should cascade away with their run;
-- this is memory. If a finding disappeared when the run it was learned from was
-- deleted, the same fallacy would return on the next refinement and be paid for
-- again. The run ids below are provenance, not constraints.
CREATE TABLE critic_memory (
    memory_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker VARCHAR(10) NOT NULL,
    source_run_id UUID,               -- the run whose report was critiqued
    refine_run_id UUID,               -- the refinement session that produced this
    iteration INTEGER NOT NULL DEFAULT 1,   -- review round within that session
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    severity VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN',  -- BLOCKING | MATERIAL | MINOR | UNKNOWN
    finding_type VARCHAR(80),         -- the fallacy-checklist item, e.g. 'priced in asserted'
    title TEXT,
    finding TEXT NOT NULL,            -- the finding block as the critic wrote it
    analyst_response TEXT,            -- the analyst's FIXED/REBUTTED reply, if any
    -- OPEN while the session runs; settled to RESOLVED (critic agreed the final
    -- report) or UNRESOLVED (budget/round ceiling hit with objections standing)
    -- when it ends. A session killed mid-flight leaves OPEN rows, which read
    -- correctly as "never settled" rather than as agreement.
    status VARCHAR(16) NOT NULL DEFAULT 'OPEN'
);

-- ===========================================================================
-- THE VERDICT LEDGER (tables 6-9)
--
-- Everything above records what a run PRODUCED. The four tables below record
-- what a verdict CLAIMED and what subsequently happened to the stock, so the
-- system can eventually be graded against realised outcomes instead of against
-- its own confidence.
--
-- Like critic_memory, none of these carry a foreign key onto pipeline_runs.
-- They are memory, not per-run artefacts: a verdict whose run was deleted is
-- still a verdict that was made, and losing it would silently shorten the very
-- track record these tables exist to accumulate. The run ids are provenance.
--
-- Nothing here is written by an LLM. Every column is either copied from the
-- screener DataFrame, snapshotted from a price quote, or (for verdict_basis)
-- parsed out of the analyst's structured output. Grading is arithmetic.
-- ===========================================================================

-- 6. Run Universe (every candidate that cleared the screen, with its numbers)
--
-- The screener's own output is not currently recoverable after the fact:
-- magic_formula_rankings_live.csv is overwritten on every run, and
-- pipeline_runs.top_30_tickers holds bare symbols with no price, rank or
-- ratio attached. So "how was this ticker ranked, and at what price, on the
-- day we screened it" is unanswerable for every run made so far.
--
-- This is a straight dump of the ranking DataFrame at run time — no API calls,
-- no cost. It is also the baseline for the question the ledger exists to
-- settle: how did the FULL screened basket do, versus only the names the
-- agents approved? Answering that requires a point-in-time price for the
-- rejected names too, which is why this table covers the whole universe and
-- not just the tickers that reached a verdict.
CREATE TABLE run_universe (
    universe_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID,                       -- provenance only; see the note above
    ticker VARCHAR(10) NOT NULL,
    company_name TEXT,
    screened_on DATE NOT NULL,
    magic_rank INTEGER,                -- Final_Rank (1 = best combined ROC + EY)
    -- Point-in-time screen figures, copied from the DataFrame columns of the
    -- same name. Stored as they were computed that day: a later re-screen of
    -- the same ticker produces different numbers and must not overwrite these.
    price NUMERIC(14, 4),              -- Live_Price
    market_cap NUMERIC(20, 2),         -- LiveMarketCap
    roc_pct NUMERIC(12, 4),            -- ROC_Pct
    earnings_yield_pct NUMERIC(12, 4), -- EY_Pct
    pe_ratio NUMERIC(12, 4),           -- PE_Ratio
    peg NUMERIC(12, 4),                -- PEG (absent from runs before PEG shipped)
    eps_growth_pct NUMERIC(12, 4),     -- EPSGrowth_Pct
    ebit_basis VARCHAR(16),            -- TTM | ANNUAL — which EBIT the rank used
    balance_sheet_date DATE,
    -- FALSE when the run stopped (budget ceiling, interrupt) before reaching
    -- this ticker. Distinguishes "the agents passed on it" from "the agents
    -- never saw it", which are opposite facts when measuring the filter.
    analyzed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, ticker)
);

-- 7. Verdict Log (one row per verdict actually reached)
--
-- One row per FRESH verdict. The reuse path (db_copy_ticker_outputs) copies a
-- prior report into a new run without re-reasoning, and must not write here:
-- that would enter the same judgement into the track record twice and let a
-- single heavily-rerun ticker dominate every base rate computed from it.
CREATE TABLE verdict_log (
    verdict_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID,                       -- provenance only; see the note above
    ticker VARCHAR(10) NOT NULL,
    company_name TEXT,
    verdict_date DATE NOT NULL,
    verdict VARCHAR(10) NOT NULL CHECK (verdict IN ('BUY', 'WATCH', 'AVOID')),
    -- INDEPENDENT = the analyst's own call from bear vs bull.
    -- POST_CRITIC  = the same ticker's verdict after the refinement loop revised it.
    -- MEMORY       = reserved for a future verdict informed by this ledger.
    -- Both of the first two are logged when a refinement runs, which makes
    -- "does the critic change outcomes, or only cost money" a measurable
    -- question rather than an assumption.
    verdict_stage VARCHAR(16) NOT NULL DEFAULT 'INDEPENDENT'
        CHECK (verdict_stage IN ('INDEPENDENT', 'POST_CRITIC', 'MEMORY')),
    -- Who made this call. PIPELINE is a verdict this system reasoned its way
    -- to; anything else is a purchase made on an outside recommendation,
    -- recorded here so it can be measured on the same arithmetic.
    --
    -- This is what makes the ledger answer a question it otherwise could not:
    -- most of the money in this household follows external picks, and without
    -- a row for them there is nothing to compare the pipeline against except
    -- the index. External rows carry NULL prompt_version and git_sha (no
    -- prompt produced them) and usually no verdict_basis rows (no factors were
    -- articulated) — both are expected, not missing data.
    --
    -- EVERY query that computes a base rate for this system MUST filter on
    -- source = 'PIPELINE'. An external pick counted as a pipeline verdict
    -- credits or blames the screen for a decision it never made, which is the
    -- one error that invalidates the whole scoreboard. See ledger-queries.sql.
    source VARCHAR(32) NOT NULL DEFAULT 'PIPELINE',
    -- Set only when the position behind this row was SOLD. Their presence means
    -- the result is final: the snapshot job stops marking the row to market, and
    -- the last verdict_outcome row is the one priced at the exit.
    --
    -- Without these, a sold position could only be measured to today's price —
    -- a return nobody earned — so closed picks had to be excluded from the
    -- scoreboard altogether. That is not a neutral omission: the picks that go
    -- wrong are the ones most likely to have been sold, so leaving them out
    -- makes every average read better than the record actually was.
    --
    -- Kept here rather than read from the position tracker so this repo stays
    -- able to run without one (see ledger-queries.sql Q5).
    exit_date DATE,
    exit_price NUMERIC(14, 4),
    -- Point-in-time snapshot. Duplicated from run_universe rather than joined
    -- to it because an on-demand single-ticker run has no universe row at all,
    -- and because a verdict must stay gradeable if its run is ever deleted.
    price NUMERIC(14, 4),
    market_cap NUMERIC(20, 2),
    shares_outstanding NUMERIC(20, 2), -- cap and price diverge under buybacks
    sector TEXT,
    industry TEXT,
    magic_rank INTEGER,
    -- The benchmark baseline, captured the same day. Without it, computing an
    -- excess return years later means re-fetching historical index levels and
    -- hoping the provider still serves that date. One extra quote, stored once.
    benchmark_symbol VARCHAR(10),
    benchmark_price NUMERIC(14, 4),
    -- Provenance. prompt_version is main.py:_prompt_version() — a hash of every
    -- agent instruction plus the shared fragments. Without it, the first prompt
    -- edit silently mixes two different systems into one track record and
    -- "we got worse" becomes indistinguishable from "we changed the question".
    -- git_sha makes that hash readable: the hash says two versions differ, the
    -- commit says how.
    prompt_version VARCHAR(16),
    git_sha VARCHAR(40),
    analysis_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 8. Verdict Basis (the factors behind a verdict, one row each)
--
-- The reusable intelligence is here, not in the verdict label. "We called it a
-- Watch" generalises to nothing; "we cited customer concentration and it went
-- on to matter" generalises across sectors and across companies.
--
-- factor_key is drawn from the controlled vocabulary in specs/config.yaml
-- (`verdict_ledger.factor_taxonomy`). Free-text keys were the alternative and
-- would have made the table useless for its purpose: "customer concentration",
-- "client concentration risk" and "top-10 customer exposure" are one factor
-- written three ways, and as three distinct keys they never accumulate into a
-- base rate. Genuinely new factors go in under the OTHER_* prefix and get
-- reviewed for promotion rather than being silently coined.
CREATE TABLE verdict_basis (
    basis_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    verdict_id UUID NOT NULL REFERENCES verdict_log(verdict_id) ON DELETE CASCADE,
    factor_key TEXT NOT NULL,
    stance VARCHAR(8) NOT NULL CHECK (stance IN ('BULL', 'BEAR')),
    materiality VARCHAR(8) CHECK (materiality IN ('HIGH', 'MEDIUM', 'LOW')),
    observation TEXT NOT NULL,        -- ~2 sentences, in the analyst's own words
    embedding vector(768),            -- for the long tail the taxonomy misses
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 9. Verdict Outcome (the scoreboard)
--
-- A weekly snapshot per open verdict, NOT four fixed rows at 30/90/180/365.
-- Fixed horizons were the first design and are fragile in exactly the way that
-- matters: a cron run missed on the day a horizon falls loses that datapoint
-- permanently, and over a year a missed run is a certainty rather than a risk.
-- Weekly capture costs one batched quote call, survives outages, and lets the
-- horizon marks be derived (`age_days`) instead of captured.
--
-- Also snapshots tickers that were screened but never bought, which is what
-- makes the basket-versus-selective comparison possible without owning the
-- names that were passed over.
CREATE TABLE verdict_outcome (
    outcome_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Exactly one of these is set. A verdict row grades a judgement; a universe
    -- row grades a screened name that never reached one, which is the control
    -- group. Kept in one table so a single weekly job writes both.
    verdict_id UUID REFERENCES verdict_log(verdict_id) ON DELETE CASCADE,
    universe_id UUID REFERENCES run_universe(universe_id) ON DELETE CASCADE,
    ticker VARCHAR(10) NOT NULL,
    as_of DATE NOT NULL,
    age_days INTEGER NOT NULL,         -- days since verdict_date / screened_on
    price NUMERIC(14, 4),
    benchmark_symbol VARCHAR(10),
    benchmark_price NUMERIC(14, 4),
    -- Both computed at write time from the baseline stored on the parent row,
    -- so a query never has to re-derive them and a later change to the
    -- benchmark choice cannot retroactively rewrite history.
    total_return_pct NUMERIC(12, 4),
    excess_return_pct NUMERIC(12, 4),  -- total_return_pct - benchmark return
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CHECK ((verdict_id IS NOT NULL) <> (universe_id IS NOT NULL))
);

-- 10. Buy-Check Sweep State (what the weekly sweep saw last time)
--
-- Not part of the ledger — this is operational state for
-- cron/run_buy_check_sweep.py, which asks weekly whether any open buy case's
-- triggers have fired.
--
-- It exists for two jobs the sweep cannot do without memory of its last run:
--
--   * "has the company reported since we last looked", which is what escalates
--     a ticker whose price has not moved but whose event triggers now have
--     fresh numbers to be tested against;
--   * not sending the same alert every week. A trigger that fires and STAYS
--     fired would otherwise notify on every sweep until the reader stops
--     opening the notification, which costs more than the alert is worth.
CREATE TABLE buy_check_sweep_state (
    ticker VARCHAR(10) PRIMARY KEY,
    last_swept_on DATE,
    last_price NUMERIC(14, 4),
    -- The price trigger parsed out of the stored buy case, kept so a sweep can
    -- report what it compared against without re-parsing, and so a buy case
    -- rewritten at a new price is visible as a change rather than silently
    -- shifting the bar.
    trigger_price NUMERIC(14, 4),
    -- Latest earnings date already seen. A `recent` report newer than this is
    -- what "they have reported since we last looked" means.
    last_earnings_seen DATE,
    -- BUY | WAIT | NULL — the agent's last answer, or NULL if the pre-filter
    -- has never escalated this ticker.
    last_recommendation VARCHAR(8),
    last_alerted_on DATE,
    -- Consecutive sweeps where the buy case's price trigger could not be read as
    -- a dollar amount. Reset to 0 the moment one parses.
    --
    -- A trigger written as "below 8.0x forward earnings" is a perfectly good
    -- trigger that arithmetic cannot evaluate, so the sweep escalates it to the
    -- model rather than skipping it — correct, but it means that name never
    -- becomes free to check and quietly costs ~$0.08 every week for as long as
    -- the document stands. The document cannot fix itself; regenerating the buy
    -- case against a live quote is a human action. This counter is what turns a
    -- recurring silent charge into one prompt to take it.
    unreadable_streak INTEGER NOT NULL DEFAULT 0,
    -- Set when the streak alert was sent, so it is sent once per episode rather
    -- than every week thereafter.
    unreadable_alerted_on DATE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for fast lookup and vector similarity search
CREATE INDEX idx_agent_outputs_ticker ON agent_outputs(ticker);
CREATE INDEX idx_final_reports_ticker ON final_reports(ticker);
CREATE INDEX idx_agent_outputs_embedding ON agent_outputs USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_final_reports_embedding ON final_reports USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_ticker_runs_ticker ON ticker_runs(ticker);
CREATE INDEX idx_ticker_runs_date ON ticker_runs(run_date DESC);
-- Backs the duplicate-run skip: looked up as (ticker, analysis_key) before any
-- billed work starts, so it sits on the hot path of every ticker.
CREATE INDEX idx_final_reports_analysis_key ON final_reports(ticker, analysis_key);
-- Refinements are a small minority of runs, so the index is partial: it answers
-- "which run refined this one" and "list the refinements" without carrying a NULL
-- entry for every ordinary pipeline run.
CREATE INDEX idx_pipeline_runs_refines ON pipeline_runs(refines_run_id)
    WHERE refines_run_id IS NOT NULL;
-- Critic memory is always read by (ticker, recency) — see db_get_critic_memory.
CREATE INDEX idx_critic_memory_ticker ON critic_memory(ticker, created_at DESC);
CREATE INDEX idx_critic_memory_refine_run ON critic_memory(refine_run_id);

-- Verdict ledger. The weekly snapshot job's hot path is "which rows still need
-- a snapshot", which is a scan by date over rows that have no recent outcome —
-- hence the date indexes on both parents rather than on ticker.
CREATE INDEX idx_run_universe_run ON run_universe(run_id);
CREATE INDEX idx_run_universe_ticker_date ON run_universe(ticker, screened_on DESC);
CREATE INDEX idx_verdict_log_ticker_date ON verdict_log(ticker, verdict_date DESC);
CREATE INDEX idx_verdict_log_date ON verdict_log(verdict_date DESC);
-- Base rates are computed per (verdict, prompt version): mixing versions is the
-- one comparison the ledger must never make silently.
CREATE INDEX idx_verdict_log_verdict ON verdict_log(verdict, prompt_version);
-- The head-to-head read path: "every external pick, oldest first".
CREATE INDEX idx_verdict_log_source ON verdict_log(source, verdict_date DESC);
-- What makes the external-pick sync safe to re-run. A purchase is identified by
-- (source, ticker, purchase date), so a second sync conflicts instead of
-- entering the same buy twice and letting one position count double in every
-- average. Partial, because the pipeline legitimately reaches several verdicts
-- for one ticker on one day (INDEPENDENT and POST_CRITIC) and must not be
-- constrained by this.
CREATE UNIQUE INDEX idx_verdict_log_external_dedup
    ON verdict_log(source, ticker, verdict_date) WHERE source <> 'PIPELINE';
-- The factor-level base rate — "every time we cited X, what happened" — is the
-- ledger's primary read, and it starts from the factor rather than the ticker.
CREATE INDEX idx_verdict_basis_factor ON verdict_basis(factor_key, stance);
CREATE INDEX idx_verdict_basis_verdict ON verdict_basis(verdict_id);
CREATE INDEX idx_verdict_basis_embedding ON verdict_basis USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_verdict_outcome_verdict ON verdict_outcome(verdict_id, as_of DESC);
CREATE INDEX idx_verdict_outcome_universe ON verdict_outcome(universe_id, as_of DESC);
-- Guards against a double-run of the weekly job writing two snapshots for the
-- same subject on the same day, which would double-count that observation.
CREATE UNIQUE INDEX idx_verdict_outcome_v_dedup ON verdict_outcome(verdict_id, as_of)
    WHERE verdict_id IS NOT NULL;
CREATE UNIQUE INDEX idx_verdict_outcome_u_dedup ON verdict_outcome(universe_id, as_of)
    WHERE universe_id IS NOT NULL;