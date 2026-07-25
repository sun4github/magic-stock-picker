# ADK Agent System Technical Architecture & Implementation Guide

This specification guides the Coding Agent in implementing a multi-agent investment workflow using **Google ADK (Agent Development Kit)**, **Model Context Protocol (MCP)** tools, and **Python**.

---

## 1. System Overview & Sequence Workflow

The application supports two execution modes (see Section 8 for details):

- **Full pipeline run** — Phase A screener feeds the Top N candidates into Phase B.
- **On-demand single-ticker run** — Phase B runs directly on one user-supplied
  ticker, bypassing Phase A. It is still recorded as its own `pipeline_runs`
  entry (containing just that one ticker) for full traceability.

The application must execute a strict 4-stage sequential workflow managed by a primary **Google ADK Workflow/Orchestrator Agent**:

1. **Phase A: Magic Formula Screening**
   - **Agent:** `MagicFormulaScreenerAgent`
   - **Action:** Triggers the MCP Tool wrapping `magic_formulae_screener.py`.
   - **Output:** Identifies and ranks valid candidates, extracting exactly the **Top 30 ranked companies**.
2. **Phase B: Balanced Decomposer Analysis** (per ticker)

   **Data gathering — direct tool calls (no LLM):**
   - **SEC 10-K** (`fetch_sec_10k_data`, `edgar-tools`): Item 1 business/segment data and **>10% customer concentration notes**.
   - **FMP metrics** (`fmp_metrics_extractor`): 3-year trailing metrics, 5-year P/E average, competitor metrics, analyst consensus targets.
   - **Magic Formula value/quality signal** (`screen_context`): ROC + Earnings Yield + rank. For on-demand single tickers (where the screener didn't run), `compute_ticker_magic_metrics` computes ROC/EY on the fly.

   These are gathered once and seeded into session state (`sec_data`, `metrics_data`, `screen_context`) for the three reasoning agents below.

   **Reasoning — a `SequentialAgent` of two advocates and a neutral judge:**
   - **Agent 1: Bear Agent** — instruction = `research-instructions.md` (skeptical). Uses `fmp_stock_news` + `web_search_tool` (Tavily, bear queries) plus the SEC/metrics data to build the **bear case** → `state['bear_data']`.
   - **Agent 2: Bull Agent** — instruction = `bullish-research-instructions.md`. Runs **after** the bear agent (its Section 4 directly refutes the bear case). Uses `fmp_stock_news` + `web_search_tool` (Tavily, bull queries) plus SEC/metrics/`screen_context` to build the **bull case** → `state['bull_data']`.
   - **Agent 3: Analyst Agent (Neutral Judge)** — carries **no** skeptical prompt. Weighs `bear_data` vs `bull_data` (plus `screen_context`) and emits a combined Markdown report with `## Bull Case`, `## Bear Case`, and `## Final Verdict` sections.
        - **Verdict:** exactly one of `Buy` / `Watch` / `Avoid`, written for an investor who does **not** currently own the stock (Buy = worth initiating; Watch = not compelling now / watchlist; Avoid = actively unattractive), with a one-paragraph justification of how the two cases net out.
        - **Balance:** skepticism lives in the Bear Agent, balanced by an equal Bull Agent; the judge itself is neutral. This removes the structural bear bias.
        - **Output:** written to `reports/{Ticker}_Skeptical_Analysis.md`; `bear_data`/`bull_data` are also persisted separately as `agent_outputs` (BEAR_CASE / BULL_CASE) for drill-down.

---

## 2. MCP Tools Setup Guide for Coding Agent

The coding agent **must wrap underlying Python scripts into standard MCP (Model Context Protocol) tool wrappers** so Google ADK agents can invoke them natively.

### A. Screener MCP Tool (`magic_formulae_screener.py`)
- Create an MCP server/tool wrapper `run_magic_formula_screener` around `magic_formula_starter_screener.py`.
- The tool must execute the script, read the resulting `magic_formula_rankings_live.csv`, extract the Top 30 ranked companies, and return them in JSON format to the `MagicFormulaScreenerAgent`.

### B. SEC EDGAR MCP Tool (`sec_edgar_extractor`)
- Create a Python script utilizing the `edgar-tools` or `sec-edgar-downloader` library.
- Instruct the coding agent to configure the tool to require a valid `User-Agent` header (`User-Agent: Name admin@domain.com`) as mandated by SEC regulations.
- Wrap this script into an MCP tool `fetch_sec_10k_data(ticker)` that extracts:
  - Business Item 1 / Segment profitability tables.
  - Notes on concentration of credit risk / customer concentration (>10% revenue).

### C. Search Tools — FMP News vs. Tavily (when to use each)

The research agents (Bear and Bull) are each given the same **two** search tools,
with a clear division of labor (they query them with opposing framing — bear vs. bull):

1. **`fmp_stock_news(ticker)`** — recent, ticker-tagged **factual** financial news
   from FMP (`/stable/news/stock`). Use for HARD developments: earnings, guidance
   changes, product launches, M&A, analyst upgrades/downgrades, lawsuits — the
   concrete catalysts tied to the company. It is a financial news feed (precise on
   the ticker) and, being on the FMP Starter plan, adds **no marginal cost**.

2. **`web_search_tool(query)`** — a modular **Tavily** open-web adapter for
   **qualitative** research: bear-case theses, valuation-drop risks,
   competitive/regulatory threats, and short-seller/analyst criticism that go
   beyond factual headlines. Configured via the `TAVILY_API_KEY` environment
   variable (`search_depth: advanced`, `time_range: year`).

Rule of thumb: **FMP answers "what happened to this company?"; Tavily answers
"why might this be a bad investment?"** The agent calls both and combines them.

- Design the `web_search_tool` interface to allow swapping backend adapters (e.g.,
  Google Custom Search, Serper, Exa) without modifying agent instruction code.
  Brave Search was the original adapter and has been removed in favor of Tavily.

---

## 3. Logger Specification

The system must log all execution steps, tool invocations, and agent handoffs:

1. **Dual Handlers:**
   - **StreamHandler:** Output formatted, color-coded logs directly to `stdout`/console.
   - **FileHandler:** Write logs to a dedicated directory (`logs/`).
2. **File Isolation per Run:**
   - Every system execution must instantiate a unique log file named with a timestamp: `logs/run_YYYYMMDD_HHMMSS.log`.
3. **Structured Context:**
   - Log tool calls, response times, candidate counts, and sub-agent delegation events.

---

## 4. Google ADK Implementation Template

The coding agent should construct the main program using `google-adk` as follows:

```python
import os
import logging
from datetime import datetime
from google.adk import Agent, Workflow
# Import custom MCP tool wrappers here...

# --- 1. LOGGER SETUP ---
os.makedirs("logs", exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = f"logs/run_{timestamp}.log"

logger = logging.getLogger("InvestmentAgentPipeline")
logger.setLevel(logging.INFO)

c_handler = logging.StreamHandler()
f_handler = logging.FileHandler(log_filename)

formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
c_handler.setFormatter(formatter)
f_handler.setFormatter(formatter)

logger.addHandler(c_handler)
logger.addHandler(f_handler)

logger.info(f"Initialized run logging to console and '{log_filename}'")

# --- 2. AGENT DEFINITIONS ---
screener_agent = Agent(
    name="screener_agent",
    model="gemini-2.5-flash",
    instruction="Execute the magic formula screener tool and output the top 30 ranked companies.",
    tools=[run_magic_formula_screener_mcp_tool]
)

sec_agent = Agent(
    name="sec_agent",
    model="gemini-2.5-flash",
    instruction="Extract Item 1 segment data and >10% customer concentration notes from SEC 10-Ks.",
    tools=[sec_edgar_mcp_tool]
)

metrics_agent = Agent(
    name="metrics_agent",
    model="gemini-2.5-flash",
    instruction="Fetch 3-year metric trends, 5-year P/E averages, and competitor data.",
    tools=[fmp_metrics_mcp_tool]
)

search_agent = Agent(
    name="search_agent",
    model="gemini-2.5-flash",
    instruction="Execute web search for news within the last year, bear cases, and catalysts.",
    tools=[fmp_stock_news_mcp_tool, web_search_mcp_tool] # FMP news + Tavily web search
)
```


## 5. PostgreSQL Persistence & Vector MCP Tools

The system stores all intermediate outputs and final reports in a PostgreSQL database using `pgvector`. The authoritative schema is [`sql-schema.sql`](../sql-schema.sql); `initialize_database()` mirrors it and applies idempotent migrations on startup.

### A. Tables
1. `pipeline_runs` — one row per run: status, tickers, and aggregated token/search usage + estimated cost.
2. `agent_outputs` — raw per-step outputs, keyed by `(run_id, ticker, agent_type)`; `agent_type` ∈ `SEC_DATA`, `QUANT_METRICS`, `BEAR_CASE`, `BULL_CASE`. Optional `embedding vector(768)`.
3. `final_reports` — the neutral analyst's combined report + `verdict` (`BUY`/`WATCH`/`AVOID`; legacy `HOLD`/`SELL` still valid) + embedding.
4. `ticker_runs` — a lean per-ticker index of runs (`ticker`, `run_id`, `company_name`, `verdict`, `run_date`), **built to drive the web UI**. No report text is duplicated here; the UI joins back to `agent_outputs`/`final_reports` on `run_id`. `initialize_database()` backfills it from existing `final_reports`.

### B. Database MCP Tools
1. `db_store_agent_output(run_id, ticker, agent_type, raw_content, metadata_json, embed=True)`:
   - Inserts into `agent_outputs`. When `embed=True`, generates a `text-embedding-004` vector on `raw_content`. **SEC/metrics are stored with `embed=False`** (raw provenance, low search value); **bear/bull cases are embedded** for semantic search.
2. `db_store_final_report(run_id, ticker, verdict, markdown_report)`:
   - Normalizes the verdict to `BUY`/`WATCH`/`AVOID` (default `WATCH`), embeds the report, and inserts into `final_reports`.
3. `db_store_ticker_run(run_id, ticker, company_name, verdict)`:
   - Upserts the `ticker_runs` index row (idempotent per `(ticker, run_id)`).
4. `db_search_historical_reports(query_text, ticker="", limit=5)`:
   - Embeds `query_text` and runs a cosine similarity search (`<=>`) against embedded rows.
5. Run lifecycle: `db_create_pipeline_run` (parent row, created first for FK integrity) and `db_finalize_pipeline_run` (terminal status + usage/cost).

### C. Per-Ticker Persistence Flow (`analyze_ticker`)
- SEC 10-K and FMP metrics are gathered by **direct tool calls** and stored via `db_store_agent_output(..., embed=False)` — 100% fidelity, no LLM in the loop.
- The bear and bull agents' outputs are stored (embedded) as `BEAR_CASE` / `BULL_CASE`.
- After the neutral analyst produces the report and a `Buy`/`Watch`/`Avoid` verdict, call `db_store_final_report`, then `db_store_ticker_run` to index it for the UI, and write the report file to `reports/{Ticker}_Final_Report_{Verdict}.md`.


## 6. API Rate Limiting & Throttling Rules

The coding agent **must implement explicit rate-limiting middleware or throttle delays** for all HTTP requests to prevent API blocks or 429/403 errors:

1. **FMP Starter License Rules:**
   - **Limit:** Maximum 300 requests per minute (5 requests/second).
   - **Implementation:** Implement a mandatory pause (`time.sleep(0.20)` or an `asyncio` rate limiter) between sequential FMP API requests.
   - **Batching:** Use `/stable/batch-quote` with up to 50 symbols per call to minimize total request counts.

2. **SEC EDGAR API Rules:**
   - **Limit:** Strict maximum of 10 requests per second.
   - **Header Requirement:** Every request MUST include the `User-Agent` header configured in `config.yaml` (Format: `Sample Company Name AdminContact@<sample company domain>.com`).
   - **Implementation:** Enforce a delay of at least `0.12 seconds` between EDGAR requests. Handle `429 Too Many Requests` responses with exponential backoff.

3. **Tavily Search API Rules:**
   - **Implementation:** Enforce a short delay (~`0.5 second`) between search queries. Tavily bills per credit (advanced search = 2 credits); the free "Researcher" tier includes 1,000 credits/month.


## 7. Environment Variables & Parameterization

The coding agent must strictly separate sensitive credentials and runtime parameters from the Python source code.

### A. Environment Variables (.env)
1. **Implementation:** The project must utilize the `python-dotenv` package to load environment variables into `os.environ`. 
2. **Secrets Management:** The following sensitive keys MUST be read exclusively from a `.env` file located at the project root:
   - `FMP_API_KEY`
   - `TAVILY_API_KEY`
   - `DATABASE_URL`
   - `SEC_USER_AGENT`
3. **No Hardcoding:** At no point should API keys or user agents be hardcoded into the Python scripts, MCP tools, or agent definitions.
4. **Template:** Generate a `.env.example` file populated with the required keys (but empty values) so the user knows what to fill out. Ensure `.env` is added to the `.gitignore` file.

### B. Configuration Parameterization (config.yaml)
1. **Dynamic Logic:** The MCP tool wrapping `magic_formula_starter_screener.py` must be modified to read its algorithmic variables (e.g., `MIN_MARKET_CAP`, `EXCLUDED_SECTORS`) directly from `specs/config.yaml`.
2. **Agent Limits:** The orchestrator agent must dynamically read `top_n_candidates` (e.g., 30) from `config.yaml` to determine how many companies to process in Phase B.

## 8. ORCHESTRATOR & WORKFLOW

Phase B distinguishes **data-relay** steps from **reasoning** steps:

- **Data relays — direct tool calls (no LLM).** SEC 10-K extraction
  (`fetch_sec_10k_data`) and FMP metrics (`fmp_metrics_extractor`) are
  deterministic tools, so `analyze_ticker` calls them **directly** and seeds
  their raw output into session state as `sec_data` / `metrics_data`. This gives
  100% data fidelity (no LLM summarization) and costs zero tokens.
- **Reasoning — an ADK `SequentialAgent` graph of three roles:** `bear_agent`
  (`research-instructions.md`, → `bear_data`) → `bull_agent`
  (`bullish-research-instructions.md`, → `bull_data`; runs after bear so §4 can
  refute it) → `analyst_agent` (neutral judge → `final_report`). Both advocates
  read `{sec_data}` / `{metrics_data}` / `{screen_context}`; the bull agent also
  reads `{bear_data}`; the judge reads `{bear_data}` / `{bull_data}`. `ticker`,
  `company_name`, `sec_data`, `metrics_data`, and `screen_context` are seeded
  into session state before the graph runs.

**Model tiers:** all three reasoning agents run on the thinking
`gemini-flash-latest` (they do genuine research/weighing). SEC/metrics are direct
tool calls, so no lite/data-gathering agents remain.

The per-ticker logic is factored into `analyze_ticker(run_id, ticker,
company_name, screen_context)`, which does the direct tool calls, runs the graph,
and persists SEC/metrics/bear/bull outputs plus the final report. All execution
modes call it, so persistence and verdict logic are identical across modes.

### A. Execution Modes

1. **Full pipeline run** (`run_orchestrator`)
   - Runs Phase A (screener → writes the rankings CSV), then Phase B over the Top N.
2. **CSV run — skip Phase A** (`run_from_csv`)
   - Phase A (the full FMP universe scan) is slow. This mode **reuses the
     rankings CSV from a previous run**, picks the Top N, and runs Phase B — no
     screener re-run. Accepts an optional CSV path (defaults to the screener's
     `magic_formula_rankings_live.csv`).
3. **On-demand single-ticker run** (`run_single_ticker`)
   - Skips Phase A entirely for a single user-supplied ticker.

All three create a `pipeline_runs` row and produce records that are structurally
identical and fully queryable by `run_id`.

### B. Invocation

```
python main.py                    # full pipeline (Phase A screener + Phase B)
python main.py --from-csv         # skip Phase A; Phase B on top N from the rankings CSV
python main.py --from-csv PATH    # same, from a specific CSV file
python main.py TICKER             # on-demand Phase B for one ticker
python main.py TICKER "Company"   # on-demand with an explicit company name
```

`run_from_csv(path)` and `run_single_ticker(ticker, company_name)` are also
directly importable for driving these modes from a service endpoint or notebook.

### C. Ordering Constraint

The parent `pipeline_runs` row MUST be created before any `agent_outputs` or
`final_reports` inserts, because those tables carry a foreign key onto
`pipeline_runs(run_id)`. The pipeline status is set to `COMPLETED` after the
run finishes.


## 9. WEB UI (Report Viewer)

A separate, **read-only** web application (`webapp/`) lets a user browse stored
reports. It is decoupled from the agent — it only reads the shared PostgreSQL
database — and is designed to run on a Raspberry Pi. It offers **two browse
modes** over the same data (see `specs/webapp.feature`):

1. **By ticker** — drill into one company's history across runs.
2. **By pipeline run** — see every ticker's decision from a single run at once.

### A. Stack & Deployment
- **Flask** (Python) — lightweight, reuses the `psycopg2` stack, no build step.
  Markdown is rendered to HTML **server-side** (`python-markdown`), so the page
  works offline with no external CDN/JS dependency.
- Reads `DATABASE_URL` from its own `webapp/.env`; binds `0.0.0.0:$PORT`
  (default 8000) so it is reachable across the LAN.
- Runs as a permanent background job on the Pi via `systemd`
  (see `webapp/README.md` and the root `README.md`).

### B. Data Flow
The UI is driven by the `ticker_runs` index table (which carries both the
per-ticker and per-run views — no report text is duplicated there); report
bodies are fetched on demand from `agent_outputs` (`BEAR_CASE` / `BULL_CASE`)
and `final_reports`:

**By ticker**
- `GET /api/tickers` → distinct tickers (alphabetical; optional `?q=` substring).
- `GET /api/runs?ticker=` → that ticker's runs, newest first, with `run_date` + `verdict`.

**By pipeline run**
- `GET /api/pipeline-runs` → **multi-ticker** runs, newest first, each with its
  `run_date`, `ticker_count`, and a Buy/Watch/Avoid breakdown (`GROUP BY run_id`
  over `ticker_runs`, `HAVING COUNT(*) > 1`). Single-ticker on-demand one-offs
  are excluded — this view is for value-discovery screens.
- `GET /api/pipeline-run?run_id=` → all tickers in that run, alphabetical, each
  with `company_name` + `verdict`.
- `GET /download-run?run_id=` → the run's tickers + recommendations as a
  `.csv` attachment (`Ticker,Company,Recommendation`).

**Shared (report drill-down + download)**
- `GET /api/report?ticker=&run_id=` → server-rendered HTML for the bear, bull, and
  final reports plus the verdict.
- `GET /download?ticker=&run_id=&kind=bear|bull|final` → the raw markdown as a
  `.md` attachment for the viewing device.

### C. User Flows
- **By ticker:** pick a ticker (alphabetical dropdown / 3-letter type-ahead) →
  pick a run (sorted by date, date shown) → view Bear / Bull / Final reports as
  rendered markdown with the Buy/Watch/Avoid recommendation badge → optionally
  download any report.
- **By pipeline run:** pick a run (sorted by date, newest first, ticker count
  shown) → see every analyzed ticker A–Z with its Buy/Watch/Avoid badge and a
  "View reports" link into that ticker's reports **for that run** → optionally
  download the whole run's decisions as CSV. The report drill-down reuses the
  shared viewer (a "Back to run" control returns to the decisions list).