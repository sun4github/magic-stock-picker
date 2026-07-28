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
    total_cost_usd NUMERIC(12, 6)
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
    UNIQUE (ticker, run_id)  -- one index row per (ticker, run)
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