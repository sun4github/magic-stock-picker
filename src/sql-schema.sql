-- Enable the pgvector extension for semantic search
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. Pipeline Runs (Tracks batch executions)
CREATE TABLE pipeline_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,     -- Set when the run finishes
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
    agent_type VARCHAR(50) NOT NULL, -- 'SEC_DATA', 'QUANT_METRICS', 'SEARCH_BEAR'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    raw_content TEXT NOT NULL,
    metadata JSONB, -- Stores structured JSON like customer concentration %, P/E, etc.
    embedding vector(768) -- Vector embedding using text-embedding-004
);

-- 3. Final Synthesized Reports
CREATE TABLE final_reports (
    report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    ticker VARCHAR(10) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    verdict VARCHAR(10) CHECK (verdict IN ('BUY', 'SELL', 'HOLD')),
    markdown_report TEXT NOT NULL,
    embedding vector(768)
);

-- Indexes for fast lookup and vector similarity search
CREATE INDEX idx_agent_outputs_ticker ON agent_outputs(ticker);
CREATE INDEX idx_final_reports_ticker ON final_reports(ticker);
CREATE INDEX idx_agent_outputs_embedding ON agent_outputs USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_final_reports_embedding ON final_reports USING hnsw (embedding vector_cosine_ops);