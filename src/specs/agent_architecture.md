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
2. **Phase B: Skeptical Decomposer Analysis** (Loop over Top 30)
   - **Agent 1: SEC Filings Data Agent**
     - Uses Python `edgar-tools` wrapped in an MCP tool to parse SEC 10-K filings for product/business segment revenue breakdowns and explicit **>10% customer concentration notes**.
   - **Agent 2: Quantitative Metrics Agent**
     - Uses an MCP tool calling FMP endpoints to extract 3-year trailing metrics, 5-year historical P/E averages, competitor metrics, and analyst consensus targets.
   - **Agent 3: Search & Bear Case Agent**
     - Uses **two complementary sources**: `fmp_stock_news` (FMP, ticker-tagged factual financial news and catalysts) and a modular `web_search_tool` (Tavily, qualitative open-web bear-case and risk research) to find news within the last year, valuation drop catalysts, and bear-case risks. See Section 2C for when each is used.
   - **Agent 4: Analysis & Composition Agent (The Synthesizer)**
        - **Role:** This is the final agent in the Phase B loop. It does **not** fetch data. It acts as the reasoning engine.
        - **Inputs:** 
        1. The raw JSON/text outputs from the `SEC_Filings_Data_Agent`, `Quantitative_Metrics_Agent`, and `Search_And_Bear_Case_Agent`.
        2. The exact text of the user's `research-instructions.md` prompt.
        - **Execution Logic:**
        - Instruct the coding agent to load the contents of `research-instructions.md` as the core `system_instruction` for this ADK Agent.
        - The agent must be instructed to strictly synthesize the provided context without hallucinating external data.
        - **Crucial Addition:** Instruct the agent to append a final section titled `## Final Verdict` that explicitly outputs a `Buy`, `Watch`, or `Avoid` verdict — written for an investor who does **not** currently own the stock (Buy = worth initiating; Watch = not compelling now / watchlist; Avoid = actively unattractive). The verdict must weigh the **bull** case (the Magic Formula value + quality signal that surfaced the stock, passed to the agent as `screen_context`) against the **bear** case from the skeptical research, and state a one-paragraph justification of how they net out. The skeptical research body stays skeptical; only the verdict is balanced.
        - **Output:** Write the final synthesized response to `reports/{Ticker}_Skeptical_Analysis.md`.

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

The Search & Bear Case Agent is given **two** tools with a clear division of labor:

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

The system must store all intermediate outputs and final reports in a PostgreSQL database using `pgvector`.

### A. Database MCP Tools to Build
1. `db_store_agent_output`:
   - **Inputs:** `run_id`, `ticker`, `agent_type`, `raw_content`, `metadata_json`.
   - **Logic:** Generates a vector embedding using `text-embedding-004` on `raw_content`, then inserts the record into `agent_outputs`.
2. `db_store_final_report`:
   - **Inputs:** `run_id`, `ticker`, `verdict`, `markdown_report`.
   - **Logic:** Generates a vector embedding on the report summary, then inserts the record into `final_reports`.
3. `db_search_historical_reports`:
   - **Inputs:** `query_text`, `ticker` (optional), `limit` (default 5).
   - **Logic:** Converts `query_text` to an embedding and runs a cosine similarity vector search (`<=>`) against `final_reports` or `agent_outputs`.

### B. Sub-Agent Execution Flow
- **After Sub-Agent Execution:** As each sub-agent (`SEC_Filings_Data_Agent`, `Quantitative_Metrics_Agent`, `Search_And_Bear_Case_Agent`) completes its task, the orchestrator calls `db_store_agent_output` to persist the result.
- **In-Memory Pass:** The Orchestrator retains the full raw text in memory and passes all 3 text blocks directly to the `Analysis_And_Composition_Agent` to ensure **100% data fidelity**.
- **After Report Synthesis:** Once the `Analysis_And_Composition_Agent` completes the report and assigns a `BUY/SELL/HOLD` verdict, call `db_store_final_report`.


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
- **Reasoning — an ADK `SequentialAgent` graph.** Only the steps that require
  reasoning are LLM agents: `search_agent` (combines FMP news + Tavily, writing
  `output_key='search_data'`) then `analysis_agent` (synthesizes the report,
  reading `{sec_data}` / `{metrics_data}` / `{search_data}` via instruction
  templating). `ticker`, `company_name`, `sec_data`, and `metrics_data` are
  seeded into session state before the graph runs.

**Model tiers:** data-gathering historically used a lite/non-thinking model, but
since SEC/metrics are now direct calls, the two remaining agents run on the
thinking `gemini-flash-latest` (`search_agent` genuinely combines sources;
`analysis_agent` reasons over everything).

The per-ticker logic is factored into `analyze_ticker(run_id, ticker,
company_name)`, which does the direct tool calls, runs the graph, and persists
the SEC/metrics/search outputs plus the final report. All execution modes call
it, so persistence and verdict logic are identical across modes.

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