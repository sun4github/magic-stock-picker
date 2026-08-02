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