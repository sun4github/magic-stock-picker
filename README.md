# magic-stock-picker

A multi-agent stock research pipeline built on the Google Agent Development Kit (ADK).
It combines Joel Greenblatt's **Magic Formula** (a quantitative value + quality
screen) with an LLM research pipeline that argues **both sides** of the investment
case — a Bear Agent and a Bull Agent — before a neutral Analyst Agent weighs them
and issues a verdict, followed by a Sale Advisor that (assuming you own the stock)
names the specific business events that would break the thesis.

## What it does

```mermaid
flowchart LR
    A["Phase A — Screener<br/>Scans the FMP universe, applies<br/>Greenblatt's eligibility gates, and<br/>ranks survivors by the Magic Formula"]
    B["Phase B — Decomposer Analysis<br/>Bear and Bull agents argue the case;<br/>a neutral Analyst Agent weighs both<br/>and issues Buy / Watch / Avoid"]
    C["Phase C — Sale Advisory<br/>Assumes the stock is already owned;<br/>names 3 measurable events that would<br/>break the original investment thesis"]
    A --> B --> C
```

**Phase A — Screener.** Scans the FMP stock universe, applies Greenblatt's
step-by-step eligibility gates — no financials, utilities, funds/REITs or foreign
(ADR) issuers; **Return on Assets ≥ 25%**; **P/E ≥ 5**; nothing that announced
earnings in the **last 7 days** — then ranks the survivors by the Magic Formula
itself (Return on Capital × Earnings Yield), producing a ranked candidate list
(also written to a CSV for reuse).

Note that Return on **Assets** and Return on **Capital** are different measures and
both are used deliberately: ROA divides operating profit by *all* assets and only
decides who enters the list; ROC divides by *capital employed* and decides the
order. See §2.H of `src/specs/agent_architecture.md`.

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

**Phase C — Sale Advisory.** After the analyst report is produced, a **Sale
Advisor Agent** (`src/sale-advisor-instructions.md`) runs per ticker. It *ignores*
the verdict and *assumes the stock is already owned*, then — using the analyst's
bull/bear thesis plus fresh FMP news + Tavily research — names the **three specific,
measurable business events** (not price movements) that would signal the original
investment case is broken and justify selling.

**Persistence.** Every run writes to PostgreSQL (with `pgvector` embeddings for
semantic search): the pipeline run's token/search usage and estimated cost, each
agent's raw output (SEC/metrics stored without embeddings; bear/bull/sale case and
the final report embedded for search), and the final verdict. Reports are also
saved locally to `src/reports/<TICKER>_Final_Report_<Buy|Watch|Avoid>.md` and
`src/reports/<TICKER>_Sale_Advisory.md`.

See [`src/specs/agent_architecture.md`](src/specs/agent_architecture.md) and
[`src/specs/workflow.feature`](src/specs/workflow.feature) for the full design spec
(with diagrams), and [`docs/`](docs/README.md) for a developer-facing code
walkthrough with file/line references into `main.py`, `mcp_server.py`, the
screener, and the web viewer.

This project follows **Spec-Driven Development**: the architecture and behavior
specs in [`src/specs/`](src/specs/) were written before the corresponding code and
used to generate the initial implementation, rather than written after the fact to
document it. They remain the source of truth as the system evolves — kept current
alongside the code, with any point where the shipped implementation diverges from
the original spec called out explicitly in the spec itself rather than left to go
stale silently.

## The book and the formula, briefly

This project automates the strategy from Joel Greenblatt's *The Little Book That
Beats the Market* (2005). Greenblatt — a hedge fund manager and Columbia Business
School professor — argued that you don't need complex models to beat the market:
you just need to systematically buy **good businesses at cheap prices**, and let a
formula (not your emotions) decide which stocks qualify.

The **Magic Formula** ranks every stock in the universe on two measures, then
combines the two ranks:

- **Return on Capital (ROC)** = EBIT ÷ (Net Working Capital + Net Fixed Assets) —
  how efficiently the business turns the capital it employs into profit. This is
  the "good business" half of the formula.
- **Earnings Yield (EY)** = EBIT ÷ Enterprise Value — how much operating profit
  you get for the total price of the business (market value of equity, plus debt,
  minus cash). This is the "cheap price" half.

A stock ranked 5th on ROC and 12th on EY across the universe gets a combined rank
of 17; sort every stock by that combined score, and the names at the top are
simultaneously good *and* cheap. Greenblatt's own backtests showed this simple,
mechanical combination beating the vast majority of professional fund managers
over long periods — largely because it forces you to buy unpopular, temporarily
out-of-favor companies instead of the popular, expensive ones everyone already
wants.

This project doesn't stop at the ranking. **Phase A** reproduces Greenblatt's
formula and eligibility rules exactly (see above); **Phases B and C** add an LLM
research layer that argues both sides of each candidate and flags the specific
events that would prove the thesis wrong — so you have more to go on than the
ranking alone before deciding whether to act on a name.

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
   cap, universe size, excluded sectors and industries), the Greenblatt
   eligibility gates (`min_roa`, `roa_basis`, `min_pe`,
   `exclude_recent_earnings_days`, `exclude_adr` — set any to `null` to disable
   it), LLM/Tavily pricing used for cost estimates, and rate limits.

   The 25% ROA hurdle is severe by design: on a sampled run it eliminated ~95% of
   the universe on its own. That is Greenblatt's intent, but if it ever leaves
   fewer than 30 survivors the screener prints a warning, because Phase B expects
   a top 30 — lower `min_roa` or raise `universe_limit` if you see it.

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

**Screen only (Phase A alone)** — refresh the rankings CSV and stop. No agents, no
database writes, nothing billed. FMP is subscription-metered, so this costs time
only.
```bash
python main.py --screen-only
```

**Skip the screener, reuse the last rankings CSV** — analyze the top N from a
previous screener run's output, without re-running Phase A.
```bash
python main.py --from-csv
python main.py --from-csv path/to/other_rankings.csv   # explicit CSV path
```

`--screen-only` and `--from-csv` are exact inverses, so **screening and analysis can
run on different cadences**: refresh the rankings as often as you like for free, and
pay for Phase B/C only in the periods you actually intend to act on the list. The two
together do the same work as a bare `python main.py`.

**Control how deep to analyze** — `--top-n` overrides `top_n_candidates` for one run.
Depth is the main cost lever, at roughly $0.37/ticker.
```bash
python main.py --from-csv --top-n 12
```
Because reports are reused within `reuse.max_age_hours` (copying stored embeddings
rather than regenerating them), **re-running with a larger `--top-n` bills only the
new tickers** — the already-analyzed ones come back for ~$0. So you can start shallow
and deepen if a run doesn't surface enough Buy verdicts, without paying twice.

**Drop Phase C** — run the bear/bull/analyst graph without the sale advisor, saving
roughly a quarter of the per-ticker cost. No `SALE_CASE` is written, so
`--sell-check` will have no conditions from such a run to test. Combines with any
Phase B mode.
```bash
python main.py --from-csv --skip-sale-advisor
```
Reports produced with and without Phase C are fingerprinted separately, so a cheap
Phase-C-less run is never reused to satisfy a later full one.

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

**Sell-condition check** — for a stock you already own, test whether the
thesis-breaking sale conditions from a prior analysis are now met. It loads a
stored `SALE_CASE`, pulls current FMP fundamentals + live news/web research, marks
each condition **MET / NOT MET / UNCLEAR** with evidence, and advises **SELL** (if
any condition is clearly met) or **HOLD**.
```bash
python main.py --sell-check CROX                    # against CROX's LATEST sale conditions
python main.py --sell-check CROX "Crocs Inc"        # optional explicit company name
python main.py --sell-check CROX --run <RUN_ID>     # against a SPECIFIC run's conditions
```
This is a lightweight flow: it reads the prior conditions and writes
`src/reports/<TICKER>_Sell_Check.md`, but does **not** create a new pipeline run
(so it never appears in the web UI's run lists). It requires a prior run to have
produced a `SALE_CASE` for the ticker.

> **Which conditions get checked?** Sale conditions are exit criteria tied to a
> *specific* investment thesis. By default the check uses the ticker's *latest*
> stored `SALE_CASE`, which is only correct if you haven't re-analyzed since buying.
> **If you bought under an earlier run, pin it with `--run <RUN_ID>`** — the run you
> purchased under — so you evaluate the conditions you actually committed to rather
> than a fresh `SALE_CASE` anchored to a thesis you never acted on. The `RUN_ID` is
> shown in the run logs. See [`src/specs/agent_architecture.md`](src/specs/agent_architecture.md) §8.D.

## Running the book's strategy on a 2-month cycle

Greenblatt's method is a **basket** strategy: it works across many positions held for
about a year, not on any single pick. His stated approach is to buy 5–7 top-ranked
names every 2–3 months, building to 20–30 positions over 9–12 months, then roll each
holding at the one-year mark. The schedule below applies that to this tool.

> Nothing here is investment advice. It is the book's method expressed as a run
> schedule; position sizing, and every buy and sell decision, is yours.

### Why 2 months, and not monthly

The ranking has two halves that move at very different speeds:

- **ROC rank** comes from filings, so it changes **once a quarter**. Between filings
  this half of the score is frozen.
- **EY rank** is EBIT ÷ enterprise value, and EV moves with the share price, so it
  drifts **daily**.

Run monthly and two runs in three show you a list whose quality half has not moved —
you would be picking from essentially one quarter's ordering three times over, which
concentrates you into whatever ranked highest at that single filing date. Two months
is the shortest interval where each list has had a fresh filing wave at least partly
flow through.

### When in the calendar

Time runs to the **back half** of each quarter's filing cycle. Filings cluster in the
4–6 weeks after each quarter end, and the earnings-blackout gate drops anything that
reported in the last 7 days — so running at the peak costs you candidates. For example,
in a sample run timed at the peak of Q2 filing season, this gate alone removed **28% of
survivors** (16 of 58); a mid-quarter run sees far less falloff.

A workable schedule — six runs, each clear of the filing peak:

| Run | Timing | Why |
| :--- | :--- | :--- |
| 1 | late **February** | Q4/annual filings absorbed |
| 2 | late **April** | after the Q1 wave |
| 3 | late **June** | quiet stretch before Q2 reporting |
| 4 | late **August** | after the Q2 wave |
| 5 | late **October** | after the Q3 wave |
| 6 | late **December** | quiet stretch before Q4 reporting |

### The 36-stock math

**6 names per run × 6 runs = 36 positions** by the end of year one. Greenblatt's own
figure is 5–7 per run and 20–30 in total; 6 per run reaches the higher end of that
range and matches the once-a-year roll — each purchase hits its one-year mark in the
same slot two years running, so the cycle self-perpetuates:

```
Year 1   build:  Feb +6  Apr +6  Jun +6  Aug +6  Oct +6  Dec +6   -> 36 held
Year 2+  roll :  each run, the batch bought 12 months earlier reaches one year,
                 and a new batch of 6 replaces it                 -> ~36 steady state
```

Analyzing 30 candidates per run gives room to find 6 Buy verdicts. Buy rates on past
runs of this pipeline have ranged widely (7%–50%, ~31% average), so 30 is a deliberate
cushion rather than a precise fit — at 31% it yields ~9 Buys.

### What to run, each time

```bash
cd src
python main.py
```

That is Phase A + B + C: roughly 55 minutes and about **$11** (6 runs ≈ $66/year).
Then read the results:

```bash
cd ../webapp
python app.py     # http://localhost:8000
```

Open **By pipeline run** → newest run. Buy verdicts are grouped first and sorted by
Magic Formula rank, so your candidates are the top rows. Work down them, read each
final report, and take 6.

Split the run if you would rather see the shortlist before spending anything:

```bash
python main.py --screen-only     # free, ~25 min — refreshes the rankings CSV
python main.py --from-csv        # ~$11, ~30 min — analyzes the top 30
```

Between buying runs, `python main.py --screen-only` costs nothing and keeps the
rankings current if you want to watch how names move.

### Rolling positions after year one

Each run, the batch bought 12 months earlier comes due. For any holding you are
weighing, `--sell-check` tests the sell conditions that were written **at the time you
bought** — pin the run you purchased under:

```bash
python main.py --sell-check TICKER --run <RUN_ID>
```

Using the latest `SALE_CASE` instead would test a thesis you never acted on. This tool
tracks no positions and no purchase dates; keep those wherever you already do.

## Web UI (report viewer)

A separate, read-only Flask web app in [`webapp/`](webapp/) lets you browse the
stored reports: pick a ticker, choose a run (by date), and view the Bear / Bull /
Sale Advisory / Final reports with the Buy/Watch/Avoid recommendation and a markdown download.
It's designed to run on a Raspberry Pi and connects to the same database via its
own `.env`. See [`webapp/README.md`](webapp/README.md) for full details.

### Quick start

```bash
cd webapp
pip install -r requirements.txt
cp .env.example .env      # set DATABASE_URL, and PORT if desired
python app.py             # http://<host>:8000
```

### Choosing the port

The app reads `PORT` from `webapp/.env` (default `8000`). Set it there:

```env
DATABASE_URL=postgresql://user:pass@host:5432/dbname
PORT=8080
```

It binds `0.0.0.0`, so it's reachable from other devices at
`http://<raspberry-pi-ip>:<PORT>`.

### Run as a permanent background job on a Raspberry Pi (systemd)

To keep the viewer running across reboots and crashes, install it as a `systemd`
service. Create `/etc/systemd/system/report-viewer.service` (adjust the path,
user, and port):

```ini
[Unit]
Description=Magic Stock Picker Report Viewer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/magic-stock-picker/webapp
Environment=PORT=8080
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`Environment=PORT=8080` sets the port for the service (it overrides the `.env`
default; keep them in sync or rely on just one). Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now report-viewer      # start now + on every boot
sudo systemctl status report-viewer            # check it's running
journalctl -u report-viewer -f                 # follow logs
```

The viewer is now permanently available at `http://<raspberry-pi-ip>:8080`. It
auto-restarts on failure and starts automatically after a reboot. (For heavier
use, install `gunicorn` and set `ExecStart=/usr/bin/gunicorn -b 0.0.0.0:8080 app:app`.)

## Notes

- FMP's legacy `v3`/`v4` endpoints were retired; this project uses the
  `stable/*` endpoints throughout.
- Gemini calls use exponential backoff on HTTP 429s (rate limits), retrying up
  to 5 times.
- Estimated LLM and Tavily costs are logged per ticker and per run, and
  persisted to `pipeline_runs` — query it to track actual spend over time.
