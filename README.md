# magic-stock-picker

A multi-agent stock research pipeline built on the Google Agent Development Kit (ADK).
It combines Joel Greenblatt's **Magic Formula** (a quantitative value + quality
screen) with an LLM research pipeline that argues **both sides** of the investment
case — a Bear Agent and a Bull Agent — before a neutral Analyst Agent weighs them
and issues a verdict.

## What it does

**Phase A — Screener.** Scans the FMP stock universe and ranks companies by the
Magic Formula (Return on Capital × Earnings Yield), producing a ranked candidate
list (also written to a CSV for reuse).

**Phase B — Balanced Decomposer Analysis.** For each candidate (or a single
on-demand ticker):
1. **Direct data gathering** (no LLM, 100% fidelity, $0 marginal cost):
   - SEC 10-K extraction (`edgar-tools`) — business/segment data, >10% customer
     concentration.
   - FMP quantitative metrics — 3-year trends, 5-year P/E average, competitor
     metrics, analyst consensus.
   - Magic Formula ROC / Earnings Yield (from the screen, or computed on the fly
     for on-demand tickers).
2. **Bear Agent** — builds the skeptical case (`src/research-instructions.md`),
   using FMP news + Tavily web search for bear-case research.
3. **Bull Agent** — builds the bull case (`src/bullish-research-instructions.md`),
   runs *after* the bear agent so it can directly refute its points, also using
   FMP news + Tavily.
4. **Analyst Agent (neutral judge)** — carries no skeptical or promotional bias.
   Weighs the bull case against the bear case and writes a combined Markdown
   report with `## Bull Case`, `## Bear Case`, and `## Final Verdict` sections.

**Verdict vocabulary** is written for an investor who does **not** currently own
the stock: **Buy** (worth initiating), **Watch** (not compelling now / watchlist),
or **Avoid** (actively unattractive).

**Persistence.** Every run writes to PostgreSQL (with `pgvector` embeddings for
semantic search): the pipeline run's token/search usage and estimated cost, each
agent's raw output (SEC/metrics stored without embeddings; bear/bull case and the
final report embedded for search), and the final verdict. Reports are also saved
locally to `src/reports/<TICKER>_Final_Report_<Buy|Watch|Avoid>.md`.

See [`src/specs/agent_architecture.md`](src/specs/agent_architecture.md) and
[`src/specs/workflow.feature`](src/specs/workflow.feature) for the full design spec.

## Requirements

- **Python 3.10+**
- **PostgreSQL** with the [`pgvector`](https://github.com/pgvector/pgvector)
  extension enabled (`CREATE EXTENSION vector;`)
- API keys / accounts:
  - **Google AI (Gemini)** — `GOOGLE_API_KEY`. A paid/upgraded key is strongly
    recommended; the free tier's daily quota (20 requests/day) is exhausted in a
    single run.
  - **Financial Modeling Prep (FMP)** — `FMP_API_KEY`. A **Starter** plan or
    higher (some endpoints used here, like `stable/key-metrics`, `ratios`,
    `market-capitalization`, are not on the free tier).
  - **Tavily** — `TAVILY_API_KEY`. Pay-as-you-go recommended for production use
    (`advanced` search depth = 2 credits/search; each ticker uses ~2
    searches/run between the Bear and Bull agents).
  - **SEC EDGAR** — no key required, but SEC requires a descriptive User-Agent
    string identifying you (`SEC_USER_AGENT`).

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create the database schema**
   ```bash
   psql "$DATABASE_URL" -f src/sql-schema.sql
   ```
   (The app also idempotently creates/migrates tables on startup, so this step
   is optional but recommended for a clean first run.)

3. **Configure environment variables.** Copy `.env.example` to `.env` in the
   project root and fill in your values:
   ```bash
   cp .env.example .env
   ```
   ```env
   FMP_API_KEY=...
   TAVILY_API_KEY=...
   DATABASE_URL=postgresql://user:password@host:5432/dbname
   SEC_USER_AGENT="Your Name your.email@example.com"
   GOOGLE_API_KEY=...
   ```

4. **Review `src/specs/config.yaml`** for tunable parameters: how many top
   candidates to analyze (`top_n_candidates`), screener filters (minimum market
   cap, universe size, excluded sectors), LLM/Tavily pricing used for cost
   estimates, and rate limits.

## Running

All commands are run from the `src/` directory:

```bash
cd src
```

**Full pipeline** — run the screener (Phase A), then analyze the top N candidates
(Phase B). This is the slowest mode since it scans the full FMP universe.
```bash
python main.py
```

**Skip the screener, reuse the last rankings CSV** — analyze the top N from a
previous screener run's output, without re-running Phase A.
```bash
python main.py --from-csv
python main.py --from-csv path/to/other_rankings.csv   # explicit CSV path
```

**On-demand single ticker** — bypasses the screener entirely. Magic Formula
ROC/Earnings Yield are computed for just that ticker so the Bull Agent still has
a value/quality signal to argue from.
```bash
python main.py BKNG
python main.py BKNG "Booking Holdings"   # optional explicit company name
```

Each run creates a `pipeline_runs` row (token usage, search usage, and
estimated cost), logs progress to `src/logs/`, and writes reports to
`src/reports/`.

## Notes

- FMP's legacy `v3`/`v4` endpoints were retired; this project uses the
  `stable/*` endpoints throughout.
- Gemini calls use exponential backoff on HTTP 429s (rate limits), retrying up
  to 5 times.
- Estimated LLM and Tavily costs are logged per ticker and per run, and
  persisted to `pipeline_runs` — query it to track actual spend over time.
