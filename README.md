# magic-stock-picker

A multi-agent stock research pipeline built on the Google Agent Development Kit (ADK).
It combines Joel Greenblatt's **Magic Formula** with Peter Lynch's **PEG ratio**
(a quantitative value + quality + growth screen) and an LLM research pipeline that
argues **both sides** of the investment case — a Bear Agent and a Bull Agent — before
a neutral Analyst Agent weighs them
and issues a verdict, followed by a Sale Advisor that (assuming you own the stock)
names the specific business events that would break the thesis.

An optional fourth phase applies the **Producer–Critic pattern** (also called
**reflection**): an independent Critic Agent — its own instructions, its own
research tools, and no sight of how the report was written — audits the finished
report for fallacies in the Analyst's *judgement* and hands findings back for
revision, looping until both agree or a spend ceiling is reached.

## What it does

```mermaid
flowchart LR
    A["Phase A — Screener<br/>Scans the FMP universe, applies<br/>Greenblatt's eligibility gates, and<br/>ranks survivors on ROC, Earnings Yield<br/>and Lynch's PEG ratio"]
    B["Phase B — Decomposer Analysis<br/>Bear and Bull agents argue the case;<br/>a neutral Analyst Agent weighs both<br/>and issues Buy / Watch / Avoid"]
    C["Phase C — Sale Advisory<br/>Assumes the stock is already owned;<br/>names 3 measurable events that would<br/>break the original investment thesis"]
    W["Phase E — Buy Case<br/>ONLY when the verdict is Watch.<br/>Assumes you do NOT own it; derives the<br/>buy price range and the events that<br/>would make it a Buy"]
    D["Phase D — Critic Review (opt-in)<br/>Producer–Critic / reflection loop<br/>An INDEPENDENT Critic Agent re-verifies<br/>the report with its own web + FMP tools<br/>and hunts fallacies in the Analyst's judgement"]
    E["Analyst revises<br/>under the same rules<br/>as Phase B"]
    F["Refined report — agreed by both,<br/>or stamped 'the critic has NOT agreed',<br/>with the open objections attached"]
    A --> B --> C
    C -->|"verdict == Watch"| W
    C -.->|"opt-in, run per ticker:<br/>python refine.py TICKER"| D
    D -->|"blocking / material findings"| E
    E -->|"re-review"| D
    D ==>|"agreed, or budget spent"| F
```

Phases A–C are one pipeline run, and so is Phase E — but only for the candidates
whose verdict comes back **Watch**. **Phase D is deliberately not part of it** — it is
a separate command you point at a report you're about to act on, because it costs
several times what producing that report cost.

(The letters run in the order the phases were added, not in pipeline order: D is the
opt-in critic loop, E runs inside the pipeline right after C.)

Four smaller commands are left off the diagram because they add no stage — they act
on a run that already exists: **`sale_advisory.py`** regenerates one report's Phase C
advisory and **`buy_case.py`** does the same for a Watch report's Phase E buy case
(when either is missing, stale, or you want it re-anchored to today's figures and
price); **`main.py --sell-check`** tests a stored advisory's triggers to advise Sell
or Hold, and **`main.py --buy-check`** tests a stored buy case's triggers to advise
Buy or Wait. All four are covered under [Running](#running).

**Phase A — Screener.** Scans the FMP stock universe, applies Greenblatt's
step-by-step eligibility gates — no financials, utilities, funds/REITs or foreign
(ADR) issuers; **Return on Assets ≥ 25%**; **P/E ≥ 5**; nothing that announced
earnings in the **last 7 days** — plus one gate that is *not* Greenblatt's:
**earnings per share must actually be growing, off a base period that was genuinely
profitable, at a PEG ratio of 1.5 or lower**. It then ranks the survivors on three
measures — Return on Capital, Earnings Yield, and 1/PEG — producing a ranked
candidate list (also written to a CSV for reuse).

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
   - Magic Formula ROC / Earnings Yield and the PEG growth test (from the screen,
     or computed on the fly for on-demand tickers).
2. **Bear Agent** — builds the skeptical case (`src/prompts/bear.md`),
   using FMP news + Tavily web search for bear-case research.
3. **Bull Agent** — builds the bull case (`src/prompts/bull.md`),
   runs *after* the bear agent so it can directly refute its points, also using
   FMP news + Tavily.
4. **Analyst Agent (neutral judge)** — carries no skeptical or promotional bias.
   Weighs the bull case against the bear case and writes a combined Markdown
   report with `## Bull Case`, `## Bear Case`, and `## Final Verdict` sections.

**Verdict vocabulary** is written for an investor who does **not** currently own
the stock: **Buy** (worth initiating), **Watch** (not compelling now / watchlist),
or **Avoid** (actively unattractive).

**Every report opens with the price.** A one-row table above everything else: what a
share cost when the report was written, the timestamp, the 52-week range, how far off
its high it is, the 50- and 200-day averages, and the market value the ratios below
are all computed from. It is stamped rather than presented as current — a stored
report is a point-in-time document, and knowing what a stock cost when a `Watch` was
written is most of the context for judging whether that verdict still holds. The same
single quote is given to the agents as a verified figure, so the prose can't wander
between two different prices, and to the buy case, so its price triggers are measured
against exactly the number printed at the top.

**Phase C — Sale Advisory.** After the analyst report is produced, a **Sale
Advisor Agent** (`src/prompts/sale-advisor.md`) runs per ticker. It *ignores*
the verdict and *assumes the stock is already owned*, then — using the analyst's
bull/bear thesis plus fresh FMP news + Tavily research — names the **three specific,
measurable business events** (not price movements) that would signal the original
investment case is broken and justify selling.

**Phase E — Buy Case (Watch verdicts only).** A `Watch` is the most common verdict
and the least actionable one: it tells you to wait without saying what for. So when —
and only when — the verdict is `Watch`, a **Buy Case Agent**
(`src/prompts/buy-case.md`) finishes the sentence. It is the mirror of Phase C:
where the sale advisory assumes you own the stock and names what would break the
thesis, the buy case assumes you **don't** and names what would make it worth owning.

It is also the only role in the system that looks **forward**. It reads analyst
consensus revenue and earnings estimates for future fiscal years, the **forward P/E
those imply at today's price — always quoted with its basis** (the price, the
fiscal-year end, the consensus EPS, and how many analysts stand behind it), the
analyst price targets and who moved them, pending deals and SEC merger filings, and
the earnings dates of the companies whose results actually drive this one's revenue
(a chip designer's customers are the cloud operators buying the chips, so their
capital-spending guidance is a leading indicator for the supplier). Those feeds are
deliberately kept away from the bear/bull/analyst chain, which weighs results that
have actually happened and may not rest a `Buy` on a projection.

What it produces is a short document ending in **buy triggers** — a derived price
range showing its arithmetic, plus three to five observable events, each with its
threshold, the current value beside it, and the report or announcement it will show
up in. `python main.py --buy-check TICKER` re-tests all of them against current data
later and answers **Buy** or **Wait**. No buy case is written for a `Buy` (the entry
condition is now) or an `Avoid` (writing entry conditions for a company the analysis
argued against is exactly the wrong document to hand someone).

**Phase D — Critic Review (opt-in): the Producer–Critic / reflection pattern.**
Phases B and C are adversarial — bear versus bull — but all four roles read the
*same* pre-fetched evidence and descend from the same prompt lineage, so nothing
stands outside them. The reconciliation gate catches wrong **figures**; nothing
catches wrong **reasoning** — a verdict that doesn't follow from the report's own
body, a "Buy" resting entirely on a rumoured deal, a "priced in" claim with nothing
behind it. This class of check only exists in Phase D, and Phase D is opt-in — a
report that nobody runs `refine.py` against has had its numbers checked but its
reasoning never examined by anything.

So `python refine.py TICKER` (see [below](#independent-critic-review-refinepy))
adds a **Critic Agent** as the independent half of a producer–critic pair. It has
its own instruction file, its own FMP + Tavily tools to verify claims against the
world, and **no sight of the analyst's prompt** — a critic told how the report was
*supposed* to be written grades against that spec instead of against reality. It
grades each finding **BLOCKING / MATERIAL / MINOR**; anything blocking or material
sends the report back to the analyst — same rules, same five sections — which
either fixes it or rebuts it with a reason. That reflection loop repeats until the
critic agrees or the budget runs out, and **the refined report says on its face
which of the two happened.** Findings persist in a `critic_memory` table across
rounds *and across sessions*, so the loop never spends a paid round relitigating a
point already settled.

**Persistence.** Every run writes to PostgreSQL (with `pgvector` embeddings for
semantic search): the pipeline run's token/search usage and estimated cost, each
agent's raw output (SEC/metrics stored without embeddings; bear/bull/sale/buy case and
the final report embedded for search), and the final verdict. Reports are also
saved locally to `src/reports/<TICKER>_Final_Report_<Buy|Watch|Avoid>.md`,
`src/reports/<TICKER>_Sale_Advisory.md`, and — for a Watch —
`src/reports/<TICKER>_Buy_Case.md`. A Phase D review is stored as its **own**
run — the report it reviewed is never overwritten — writing
`src/reports/<TICKER>_Refined_Report_<Verdict>.md` and
`src/reports/<TICKER>_Critic_Review.md`.

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
combines the two ranks (this project adds a third — see below):

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

### Peter Lynch's PEG ratio — the third measure

Greenblatt's two ratios ask whether a company is cheap and whether it is a good
business. Neither asks whether it is **growing**, and that gap is the classic value
trap.

The earnings yield is a **snapshot**: this year's profit set against today's price. It
has no memory and no sense of direction. Consider two companies both showing a 10%
yield:

| | Profit | Price | Yield |
| :--- | ---: | ---: | ---: |
| **A** — steady | $10, and has been for years | $100 | 10% |
| **B** — shrinking | $10, down from $25 three years ago | $100, down from $250 | 10% |

**The screen cannot tell them apart.** But buy B for $100 and next year it earns $7,
the year after $5 — the return on the money you actually paid falls to 7%, then 5%.
You bought a melting asset at what looked like a fair price.

Note what is *not* being claimed: falling profits do not raise the yield — as your
arithmetic would expect, a drop from $10 to $6 on a $100 price takes the yield from
10% down to 6%. The trap is that **the price falls too, and usually faster**, because
the market prices in the decline it can see coming. That keeps the ratio looking
healthy while the business underneath deteriorates.

That is not hypothetical here. On the screen run immediately before this was added,
the **top-ranked company** showed a 20% earnings yield and a 1,673% return on capital.
Its operating profit had fallen 19% over three years — but its market value had fallen
**68%**, so the yield went *up*, from 13% to 33%. Its earnings per share had gone from
$3.21 to $0.15, through a $945M write-off. The screen saw only the ratio between price
and profit, and called it the best company on the list.

So this project adds the **PEG ratio** from Peter Lynch's *One Up on Wall Street*:

```
PEG = price-to-earnings ratio ÷ earnings growth rate, in percentage points
```

A P/E of 20 on 20% growth is a PEG of exactly 1.0 — Lynch's fair-value line. Below
1.0 you are getting the growth cheaply; above it you are paying up front for growth
that has not happened yet. A company must be growing at all and have a PEG of **1.5 or
lower** to enter the list, and its **1/PEG rank is added to the two Magic Formula
ranks** to order it. So a stock ranked 5th on ROC, 12th on earnings yield and 9th on
1/PEG scores 26.

#### How the growth rate is measured

Not by comparing one year to one year three years earlier — that is decided entirely
by which two years the calendar happens to pick, and it steps straight over everything
in between. Instead:

```
growth = ( total EPS over the last 3 years  ÷  total EPS over the 3 years before that ) ^ (1/3) − 1
```

Adding up whole periods means a loss year is counted at its real negative value
instead of being skipped. On one live example — a company the two-point method scored
at *+349% a year* — the last three years actually **total −$0.57 per share**. It made
a loss overall, so it has no growth rate and is excluded. The old method would have
ranked it first.

#### The three thresholds, and why they are where they are

Each was set from a 189-company cross-sector study rather than picked by feel. The
study, including its caveats, is in §10.I of `src/specs/agent_architecture.md`.

**`max_peg: 1.5` — how expensive, relative to growth, is too expensive.** It started
at 1.2 and the data said that was wrong: the companies sitting between 1.2 and 1.5
were JNJ, ISRG, CMG, MCK, KDP and similar — steady growth, no loss years, and none of
them there because of a capped growth rate. The genuinely speculative names were
already getting in *below* 1.2. So 1.2 was excluding quality, not risk.

**`min_base_net_margin: 3%` — the starting period must have been a real one.** A
compound growth rate is only as meaningful as what it starts from. One company's base
year earned $852K on $195M of sales — a 0.4% margin, profitable on paper and
breakeven in reality — which turned an ordinary recovery into a three-digit growth
rate. A dollar floor on earnings per share cannot express this (10 cents is nothing
for a $500 stock and a lot for a $2 stock), so the test is **net profit margin**,
which means the same thing at any price or in any sector. A typical company here
scores about 10%; NVDA's base year was 16%, LLY's 22%.

**`max_growth_rate_for_peg: 60%` — never divide by a growth rate above 60.** Growth
does not persist. Checking what companies actually delivered *after* a fast stretch:
of those growing over 50% a year, **73% went on to negative growth over the next three
years**, with a median of −20%. The fastest growers were the worst subsequent
performers, and the group was dominated by cyclicals at a peak. Meanwhile only 5% of
companies ever sustain more than 63%. So paying for growth above ~60% is paying for
something the data says almost never arrives.

Two consequences worth knowing. Capping can only ever *raise* a PEG, so it can never
flatter a company into the list — and it puts a hard ceiling on P/E for the whole
screen of `max_peg × cap × 100` = **90**, however fast a company is growing. Review
those two settings together, never one alone.

This project doesn't stop at the ranking. **Phase A** reproduces Greenblatt's
formula and eligibility rules (see above) and adds Lynch's growth test; **Phases B
and C** add an LLM research layer that argues both sides of each candidate and
flags the specific events that would prove the thesis wrong — so you have more to
go on than the ranking alone before deciding whether to act on a name.

### Re-checking and adjusting the growth cap

The three PEG thresholds were set from **one sample of 189 companies taken on
2026-07-31** — the largest ~21 companies in each of the 9 sectors the screen allows,
with 8 years of annual figures each. That sample is a snapshot: it reflects where the
market was that week, it covers US large and mid caps only, and its headline finding
(that fast growth reverses) rests on just 11 companies, because few companies ever
grow that fast. **The direction is solid. The exact numbers are not gospel.**

So re-derive them rather than adjusting by feel. There is a tool for it, in
[`tools/`](tools/) rather than `src/` because it is not part of the pipeline — nothing
imports it, it computes no PEG and screens no company:

```bash
python tools/analyze_growth_persistence.py
```

Run it from the repository root. It takes about 10 minutes, makes no LLM calls and
writes nothing — FMP is billed by subscription, so it costs time only. Use
`--per-sector 40` for a larger sample.

**When to run it:** before changing `max_growth_rate_for_peg`, `max_peg` or
`min_base_net_margin`; and roughly once a year regardless, so a threshold tuned to one
market doesn't quietly persist into a different one.

**What it prints, and how to read it.** Three tables, one per threshold:

1. **Growth persistence** — for every company with enough history it compares the
   growth you would have *known about* three years ago against what the company
   *actually delivered* since. Both windows are historical, so nothing is being
   forecast. If the fastest-growing bucket still shows most companies going on to
   shrink, the cap is doing real work; if fast growth has started to persist, the cap
   is costing you candidates.
2. **Distribution of realised growth** — what companies genuinely achieve. **Set
   `max_growth_rate_for_peg` near the 95th percentile.** The logic: above that line you
   are dividing by a rate almost nobody actually delivers, so you'd be paying for
   growth that doesn't arrive. The script prints the percentile and compares it to
   your configured value.
3. **Base-window net margin** — set `min_base_net_margin` *below* the 25th percentile,
   so it catches only genuinely breakeven starting periods rather than ordinary ones.

**One trap when adjusting.** `max_peg` and `max_growth_rate_for_peg` are
mathematically linked. A company passes when `PEG ≤ max_peg`, and PEG is
`P/E ÷ growth` where growth can never exceed the cap. So the best possible PEG is
`P/E ÷ cap`, which means:

```
hard P/E ceiling for the whole screen = max_peg × max_growth_rate_for_peg × 100
                                      = 1.5 × 0.60 × 100 = 90
```

**No company with a P/E above 90 can enter the screen, however fast it grows.** Raise
`max_peg` to 2.0 and you have also silently raised that ceiling to 120; drop the cap to
40% and you have lowered it to 60. Neither knob moves alone — decide what P/E ceiling
you want, then pick the pair. The script prints your current ceiling as a reminder.

**After any change**, re-run the screen alone before paying for analysis:

```bash
python src/main.py --screen-only
```

That refreshes the rankings CSV and prints the gate breakdown, so you can see exactly
which cut moved and how many candidates survived — for free. Only then run
`python src/main.py --from-csv`, which is the billed part.

## Dependencies

Everything in this section is required to run **Phases A–C and E** (the screener,
the Bear/Bull/Analyst pipeline, the Sale Advisory, and the Buy Case) — the core
loop `main.py` runs. Phase D, the opt-in Critic/Reflection loop, adds one more
requirement of its own; see the second table below.

### Core (every run)

| Dependency | What it's for | Notes |
| :--- | :--- | :--- |
| **Python 3.10+** | Runtime | |
| **PostgreSQL** + [`pgvector`](https://github.com/pgvector/pgvector) | Stores runs, reports, and embeddings | `CREATE EXTENSION vector;` |
| **Google AI (Gemini)** — `GOOGLE_API_KEY` | Powers the Bear, Bull, Analyst, and Sale Advisor LLM agents | A paid/upgraded key is strongly recommended; the free tier's daily quota (20 requests/day) is exhausted in a single run |
| **Financial Modeling Prep (FMP)** — `FMP_API_KEY` | Screener universe, fundamentals, analyst consensus, news | Requires a **Starter** plan or higher — some endpoints used here (`stable/key-metrics`, `ratios`, `market-capitalization`) aren't on the free tier |
| **Tavily** — `TAVILY_API_KEY` | Web search for the Bear/Bull/Sale Advisor agents | Pay-as-you-go recommended for production use (`advanced` search depth = 2 credits/search; each ticker uses ~2 searches/run between the Bear and Bull agents) |
| **SEC EDGAR** — `SEC_USER_AGENT` | 10-K/10-Q filings | No key, but SEC requires a descriptive User-Agent string identifying you |
| **[Ollama](https://ollama.com)**, running **locally**, with `nomic-embed-text` pulled (`ollama pull nomic-embed-text`) | Generates the embeddings stored on every `agent_outputs`/`final_reports` row, for semantic search | No API key, nothing leaves your machine — this is a small (274MB) model that runs fine on CPU, no GPU needed. Override the endpoint with `OLLAMA_EMBED_API_BASE` if Ollama isn't on the default `http://localhost:11434` |

### Additional, only if you use the Critic/Reflection loop (Phase D, `refine.py`)

The critic is configured (`agents.critic_model` in `specs/config.yaml`) to run on
`glm-5.2:cloud` — a *different* model lineage from the Gemini analyst, deliberately,
so the review doesn't share the analyst's blind spots (see
[09-critic-and-refinement-loop.md](docs/09-critic-and-refinement-loop.md)). That
model is routed through the same local Ollama install as embeddings, but reaching
it needs one of:

- **An Ollama Cloud subscription** — set `OLLAMA_API_BASE=https://ollama.com` and
  `OLLAMA_API_KEY=...` in `.env`, and Ollama proxies the call to Ollama's hosted
  `glm-5.2:cloud`/`kimi-k3:cloud`. No local compute needed beyond running Ollama
  itself.
- **A powerful local GPU** — repoint `agents.critic_model` at an equivalent model
  pulled and served entirely locally. `glm-5.2` and `kimi-k3` are large models;
  running them at usable speed without the cloud tier means real GPU VRAM, not the
  same CPU-only setup that's enough for `nomic-embed-text`.

This is a separate concern from the embeddings' `OLLAMA_EMBED_API_BASE` — one env
var can point at the cloud for the critic while the other stays on `localhost` for
embeddings, and vice versa. Skip this whole section if you never run `refine.py`;
Phases A–C and E don't touch the critic model at all.

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
   # OLLAMA_EMBED_API_BASE=http://localhost:11434   # only if not on the default port
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
pay for Phase B/C/E only in the periods you actually intend to act on the list. The
two together do the same work as a bare `python main.py`.

**Control how deep to analyze** — `--top-n` overrides `top_n_candidates` for one run.
Depth is the main cost lever, at roughly $0.37/ticker — plus about $0.18 more for each
ticker whose verdict comes back `Watch` and therefore gets a buy case.
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

**Drop Phase E** — skip the buy case even for a `Watch` verdict. Nothing is written,
so `--buy-check` will have no triggers from such a run to test. Fingerprinted
separately for the same reason as above.
```bash
python main.py --from-csv --skip-buy-case
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

**Buy-condition check** — the mirror image, for a stock you do **not** own: test
whether the buy case's price range and triggers are now met. It loads a stored
`BUY_CASE`, fetches **today's price** plus current fundamentals and live news/web
research, marks each trigger **MET / NOT MET / UNCLEAR** with evidence, reports what
has moved since (consensus estimates, analyst targets, any bid), and advises **BUY**
or **WAIT**.
```bash
python main.py --buy-check MRVL                    # against MRVL's latest buy case
python main.py --buy-check MRVL "Marvell"          # optional explicit company name
python main.py --buy-check MRVL --run <RUN_ID>     # against a SPECIFIC run's buy case
```
Also lightweight: it writes `src/reports/<TICKER>_Buy_Check.md` and creates no
pipeline run. It requires a prior run to have produced a `BUY_CASE` — which means a
prior run must have reached a `Watch` verdict on that ticker.

> **Note the opposite default from `--sell-check`, and why.** A sell-check should be
> anchored to the run you *bought* under, because sale conditions are the exit
> criteria of a specific entry thesis. A buy-check is a decision you are making
> **today**, so the newest buy case is the right one by default. `--run` is there for
> when you want to test what a *particular* past case would have said — "would the
> case I passed on in March have fired by now?". If a newer report has since moved
> the ticker off `Watch`, the command says so and runs the check anyway.

**Recommendation lines at a glance:**

| Command | Assumes you | Reads | Answers |
| :--- | :--- | :--- | :--- |
| `--sell-check` | own it | stored `SALE_CASE` | `SELL` / `HOLD` |
| `--buy-check` | don't own it | stored `BUY_CASE` | `BUY` / `WAIT` |

### Independent critic review (`refine.py`)

A **separate command**, not part of the pipeline. It takes a final report the
pipeline has already produced and puts it in front of an **independent critic
agent** — its own instructions, its own news and web-search tools, and no sight of
how the report was written — which hunts for fallacies in the analyst's *judgement*:
a verdict that does not follow from the report's own body, a "Buy" resting on a
rumoured deal, a "priced in" claim with nothing behind it, a sell trigger that
ignores the company's seasonality. The analyst then revises against those findings,
and the critic reviews again, until **either the critic agrees or the budget runs
out**.

```bash
python refine.py CROX                       # refine CROX's latest report
python refine.py CROX --run <RUN_ID>        # refine a SPECIFIC run's report
python refine.py CROX --max-budget 3.00     # raise the ceiling for this run
python refine.py CROX --max-rounds 2        # cap the rounds instead
```

The refined report says on its face which of those two happened. If the critic
agreed, it says so and states plainly that agreement means neither agent could find
a fault in the argument — not that the call is right. Agreement doesn't require the
critic to have found *nothing*: a MINOR point (worth fixing, but not serious enough
to withhold agreement) does not trigger another paid round, so it is never silently
dropped — it's **named in the report itself**, along with the required fix that was
never applied, so you're reading a caveat rather than a correction already made.
**If they never agreed, the report is stamped "the independent critic has NOT agreed
this report"**, with the count of objections still standing, why the review stopped,
and the critic's full final review reproduced underneath. Read that before acting on
the verdict.

> **First live runs (2026-08-01):** HRB and LLY agreed in round 1 (~$0.10–$0.12
> each) with one MINOR finding apiece. FISV — an older report predating a prompt fix
> for exactly this class of bug — took one revision: the critic caught the report
> citing the Magic Formula return on capital (136.73%) alone on a company where
> goodwill is 59.1% of assets, where the goodwill-inclusive figure is a very
> different 9.4%. The analyst fixed it and three other findings in one pass; round 2
> came back clean. Total cost $0.2556, verdict unchanged (Watch). See
> [`docs/09-critic-and-refinement-loop.md`](docs/09-critic-and-refinement-loop.md#measured-behaviour-first-three-live-runs-2026-08-01-gemini-36-flash)
> for the full findings from each run.

Why it is a separate command: it costs several times what producing the report cost
in the first place, and takes as long. Run it on the handful of names you are
actually about to buy. The ceiling defaults to `refinement.max_budget_usd` in
`config.yaml` ($2.45) and is enforced *between* rounds. Committing to a revision
commits to four things, priced as one: the revision, **the review that must follow
it** — so the report you are handed has always been checked as it stands — the
**sale advisory that revision invalidates**, since an advisory written against the
pre-review report can carry sell triggers anchored to a figure the critic corrected,
and the **buy case**, on the same logic. The buy case is reserved for every session
even though only a refined report ending on `Watch` will spend it: whether it is
needed depends on the verdict of a report that does not exist yet, so it cannot be
reserved conditionally. The rolling daily budget still applies on top.

The refinement gets its **own** run id and its own cost row; the report it reviewed
is left untouched, and the refined one is written to
`src/reports/<TICKER>_Refined_Report_<Verdict>.md` alongside
`src/reports/<TICKER>_Critic_Review.md`. It also shows up in the
[web UI](#web-ui-report-viewer) as its own run, marked with a critic-standing chip
and a dedicated Critic Review tab — see below.

> **A review is not confused by an old price.** The critic works from figures
> recomputed *today*, while the report it is reading was written days or weeks ago —
> so the stock has usually moved since. It is told which figures are allowed to have
> moved (share price, market value, and anything derived from them) and which are not
> (debt, cash, equity, assets, profit — those come off the balance sheet and don't
> change until the next filing). A price that has merely moved is never reported as a
> mistake, and the analyst is told not to quietly rewrite old prices to today's. If
> the move is big enough to actually weaken the argument, *that* gets raised — as a
> point about the reasoning, not as a wrong number.

> **The critic remembers.** Every finding is stored in a `critic_memory` table and
> replayed into both agents on later rounds *and later sessions for the same
> company*, along with how it was settled. That is what stops the loop spending a
> paid round relitigating a point the analyst already conceded, or reintroducing a
> correction it made last week. See
> [`docs/09-critic-and-refinement-loop.md`](docs/09-critic-and-refinement-loop.md).

### Generate or repair a sale advisory (`sale_advisory.py`)

The pipeline writes a sale advisory once, as the last step of the run that produced
the report. A report can outlive its advisory, though — the critic revised the report
but the budget could not cover re-deriving the advisory, `--skip-sale-advisor` was
used, the advisor produced nothing, or the advisory is simply old and its thresholds
are anchored to figures that have moved. This regenerates one for **any** stored
report, without re-running the pipeline:

```bash
python sale_advisory.py CROX                    # from CROX's latest report
python sale_advisory.py CROX --run <RUN_ID>     # from a SPECIFIC run's report
python sale_advisory.py CROX "Crocs Inc"        # optional explicit company name
```

Costs ~$0.08-0.10 (one agent call) against the rolling daily budget, versus ~$0.40 to
re-run the whole pipeline — which would also rewrite the report you already reviewed.
It works against a pipeline run or a critic-review run alike, and the new advisory
says on its face that it was generated separately and is anchored to the figures
current at generation time.

The advisory is stored **on the run you name**, so `--sell-check TICKER --run <that
id>` picks it up and the web viewer shows it on that run's Sale Advisory tab. The
*cost*, however, gets its own row — adding it to a finished run's totals would
rewrite the record of what that run cost.

> **After a critic review, prefer this over re-running `refine.py`.** Re-running the
> review would not repair a stale advisory: the critic agrees immediately (the report
> is already corrected), so no revision runs, and the advisory gets carried forward
> again — this time without its staleness warning.

### Generate or repair a buy case (`buy_case.py`)

The same tool for the other side. The pipeline writes a buy case once, for a `Watch`
verdict, as the last step of that ticker's run — and a buy case ages faster than
anything else here, because its first trigger is a price range measured against a
quote that moves every session and derived from consensus estimates that are revised
continuously. Refreshing one is ordinary watchlist maintenance, not a repair:

```bash
python buy_case.py MRVL                     # from MRVL's latest report
python buy_case.py MRVL --run <RUN_ID>      # from a SPECIFIC run's report
python buy_case.py MRVL "Marvell"           # optional explicit company name
```

Costs ~$0.12–0.18 against the rolling daily budget. The same split as
`sale_advisory.py` applies: the buy case is stored **on the run you name**, so
`--buy-check --run <that id>` picks it up and the viewer shows it on that run's Buy
Case tab, while the *cost* gets its own row so the named run's totals are not
rewritten.

**It refuses on a report whose verdict is not `Watch`**, and there is no override
flag. A `Buy` needs no entry conditions — the entry condition is now — and an `Avoid`
should not be handed any. If you think the verdict is wrong, the answer is
`python refine.py TICKER`: let the critic move it on the evidence, rather than writing
entry conditions underneath a verdict that says no.

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

That is Phase A + B + C, plus Phase E for the Watch verdicts: roughly 55 minutes and
about **$11–13** (6 runs ≈ $66–78/year). Then read the results:

```bash
cd ../webapp
python app.py     # http://localhost:8000
```

Open **By pipeline run** → newest run. Buy verdicts are grouped first and sorted by
Magic Formula rank, so your candidates are the top rows — each with the share price it
was analysed at, so you can see at a glance how far the market has moved since. Work
down them, read each final report, and take 6.

**Then do something with the Watch verdicts**, which are usually the largest group.
Each has a **Buy Case** tab holding the price range and the events that would make it
a Buy. Skim those and note the ones whose triggers look close. You can test any of them
on demand with `python main.py --buy-check TICKER` (a couple of cents each) — but the
weekly sweep now does this for you, so the watchlist checks itself rather than waiting
for you to remember. See [What runs between your runs](#what-runs-between-your-runs).

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

The buy side of the same rhythm is largely automatic now: the weekly sweep tests every
open buy case and alerts you when one reads Buy, so a fired trigger reaches you in the
week it fires rather than at your next run. A trigger that has fired is a candidate you
already did the work on. If a buy case is more than a run or two old, regenerate it
first (`python buy_case.py TICKER`) so its price range is anchored to today's market
rather than to the one it was written in — the sweep tests the triggers as written and
will happily keep measuring against a stale price.

## What runs between your runs

Two optional scheduled jobs run on their own between analysis runs. They are easy to
confuse, and the quickest way to keep them apart is that **neither one knows what you
own** — this tool tracks no positions. One watches the entry conditions it wrote for
`Watch` verdicts; the other just records prices.

| | Weekly buy-check sweep | Ledger snapshot |
| :--- | :--- | :--- |
| Asks | "has a buy trigger fired?" | "how did past calls turn out?" |
| Looks at | stored buy cases still on `Watch` | every verdict, and the whole screened list |
| Uses a model | yes (ADK + Gemini) | **no** — arithmetic only |
| Needs | the full agent stack | `requests`, `psycopg2`, `pyyaml`, `dotenv` |
| Suggested schedule | weekly, after the close | weekly, after the close |
| Notifies | yes, when something fires | never |
| Costs | ~$0.08 per escalated ticker | nothing |

Because the snapshot job needs none of the agent stack, the two can run on different
machines — the sweep wherever you run the pipeline, the snapshot on any always-on box
that can reach the database.

### The weekly buy-check sweep

Every `Watch` verdict carries a buy case: a price trigger plus two to four event
triggers that would make it a Buy. Before this existed, those were only checked when
you remembered to. Most never were — and a watchlist nobody revisits is just a list of
tickers.

The sweep tests all of them, every Saturday after the close. **What keeps it cheap is
that it does not ask the model about most of them.** A free arithmetic pass runs first,
using the price the buy case itself wrote down:

| Condition | Escalates? | Why |
| :--- | :--- | :--- |
| Price at or below the buy case's price trigger | yes | it has fired |
| Price within 5% of it | yes | about a week's movement — it could fire before the next sweep |
| Company reported since the last sweep | yes | most event triggers are written against reported figures |
| Trigger price could not be read | yes | $0.08 beats silently dropping a name |
| None of the above | no | costs nothing |

Only the escalated ones reach the model, so a sweep over five open buy cases reads
something like:

```
[AAA] skip     — price $66.05 is 29.1% above the $51.16 trigger
[BBB] skip     — price $566.49 is 31.5% above the $430.75 trigger
[CCC] skip     — price $222.18 is 56.7% above the $141.80 trigger
[DDD] ESCALATE — reported on 2026-08-05, after the buy case was last tested
[EEE] ESCALATE — reported on 2026-08-06, after the buy case was last tested
```

Three cost nothing. Note that both escalations there are **earnings-driven, not
price-driven** — a price-only filter would have missed them, and those are exactly the
weeks an event trigger's answer changes. Five names checked in full would be ~$0.40;
that week costs $0.15, and a week where nothing moves and nobody reports costs zero.

**What you get.** When a check comes back `Buy`, a Pushover push and an email arrive
naming the price, the trigger it cleared, and why the sweep looked at that ticker at
all. Only on `Buy`, and not twice for the same standing signal — a trigger that fires
and stays fired would otherwise alert every week until you stopped opening the
notification.

**What it writes.** Three places, and the difference matters:

- `reports/TICKER_Buy_Check.md` — the full check: every trigger marked MET / NOT MET /
  UNCLEAR with evidence and dates. **Overwritten by the next check.**
- **The database**, as a `BUY_CHECK` record against the run whose buy case it tested.
  This is the durable one — it accumulates, so you can see when a trigger first fired
  and what the reasoning was. The viewer shows them under a **Buy Checks** tab, newest
  first.
- `logs/buy_check_sweep.log` — every decision, including the free skips.

On Windows, `tools\setup_weekly_jobs.cmd` registers it with Task Scheduler; on Linux
or macOS it is an ordinary weekly crontab entry. Preview it any time without spending
anything:

```bash
python src/cron/run_buy_check_sweep.py --dry-run
```

That runs the pre-filter and reports who *would* be checked. No model calls, no alerts,
and it changes nothing.

### The ledger snapshot

In one sentence: **once a week it writes down what every stock the system has ever had
an opinion on is worth today.** It is a price recorder, and silent by design — a price
being recorded is not news, so it never notifies.

**It has no idea what you own** — this tool tracks no positions at all. What it reads
is two tables that your *analysis runs* fill:

| Table | Holds | Filled by |
| :--- | :--- | :--- |
| `verdict_log` | every Buy / Watch / Avoid the analysis reached | an analysis run |
| `run_universe` | every name that cleared the screen | an analysis run |

Each week it prices everything in both that is still inside its tracking window, notes
where the benchmark stood, works out the return and the excess return, and appends one
row per subject to a third table:

| Table | Holds | Filled by |
| :--- | :--- | :--- |
| `verdict_outcome` | one dated price per subject per week, with return and excess return | this job |

**`verdict_outcome` is the scoreboard** — it is what every query in the next section
reads. The two tables above are what the system *thought*; this one is what actually
happened next.

It appends rather than overwrites, so each subject accumulates a weekly price history
rather than a single latest value. That is deliberate: a name up 40% at six months and
flat at twelve is a different story from one that climbed steadily, and a single
end-point reading cannot tell them apart. It also means a missed week costs nothing —
the next run simply carries on.

**It deliberately prices the names that were rejected, too.** That reads backwards at
first, and it is the whole point: a record of only the names the analysis liked has
nothing to compare them against. The ones it passed over are the control group, and
without them the next section's first question cannot be asked at all.

Say a run screens 12 names and the analysis calls 5 of them Buy. `run_universe` gets 12
rows, `verdict_log` gets 12 rows, and each week after that the job prices all 12 plus
the benchmark and writes 24 rows into `verdict_outcome` — the same companies tracked
twice, once as a screened name and once as a verdict. A year later that answers: *did
the 5 it liked beat the 12 it screened?* — which needs the 7 it rejected just as much as
the 5 it did not.

Until the first analysis run after you install it, both tables are empty and the job
correctly reports `No ledger subjects due for a snapshot. No API calls made.` and exits.
**That is a pass, not a failure.**

## Reading the track record

The system records every verdict it reaches, the factors behind it, and the whole
screened list it came from — then prices them week after week. That is the **verdict
ledger** ([docs/12](docs/12-verdict-ledger.md)).

Ten questions are written out in [`src/ledger-queries.sql`](src/ledger-queries.sql), so
they get asked the same way every time:

```bash
psql "$DATABASE_URL" -f src/ledger-queries.sql
```

| | Answers |
| :--- | :--- |
| **Q1** | Did filtering the screen beat *not* filtering it? |
| **Q2** | Do Buy / Watch / Avoid actually return different things? |
| **Q3** | **Which factors do we read well, and which badly?** |
| **Q4** | Did the critic improve outcomes, or only cost money? |
| **Q5** | How did the positions you actually bought do? |
| **Q6** | What is in the ledger right now? |
| **Q7** | This pipeline's picks vs the ones you chose elsewhere vs the market |
| **Q8** | The same, weighted by what you actually invested |
| **Q9** | Names *both* streams landed on — and what deliberating cost |
| **Q10** | **Every dollar in, against the same dollars in the index** |

Q1–Q4 grade this system and all filter on `source = 'PIPELINE'`. Q7–Q9 compare the two
streams. **Q10 is the one to run if you only run one** — see below.

### How to read the columns

A row from Q3 might look like:

```
factor_key: customer_concentration   stance: BEAR
n: 9      avg_vs_market: -14.2%      pct_read_correctly: 78%
```

- **`n`** — how many times. The trust dial. At `n=3` ignore the row; at `n=30` start
  listening. Every query reports its own.
- **`stance`** — which way we argued. `BEAR` = a reason not to buy, `BULL` = a reason
  to. You need it because "were we right" flips with the side taken: BEAR plus a stock
  that fell is a hit; BULL plus the same stock is a miss.
- **`avg_vs_market`** — **not** "did the stock go up". It is the return *minus* the
  benchmark's return over the same window. A stock up 21% while the market rose 30%
  scores **−9%**: the analysis lost you money even though the price rose. Raw returns
  would call you a genius in every rising market, until suddenly they didn't.
- **the `%` columns** — hit rate: how *often* we were right, not how *much*. Read it
  alongside the average, because one enormous winner can carry a pile of losers. Two
  strategies both averaging +10% are completely different animals if one is right 70%
  of the time and the other 20%.

### Picks this system did not make

Most people who run a screen do not buy only what it says. If the larger share of your
money follows an outside recommendation, this pipeline can only ever be compared against
the index — which flatters or damns both streams together depending on what the market
happened to do.

`verdict_log.source` fixes that. `PIPELINE` is a verdict this system reasoned its way to;
anything else is a purchase made elsewhere, recorded so it is measured on the same
arithmetic. [`src/sync_external_picks.py`](src/sync_external_picks.py) mirrors them in
from a position tracker (see Q5), stamping each one with the benchmark's close on the day
of the purchase.

An externally-sourced row is a `BUY` dated to the **purchase**, priced at the **fill**.
That is a deliberate choice about which question gets answered: it grades your money,
including whatever was lost between reading an idea and acting on it. `prompt_version`,
`git_sha` and factor rows are all absent, because no prompt produced these and no factors
were articulated.

**The one rule that is not optional:** every query grading *this system* must filter
`source = 'PIPELINE'`. Without it the screen collects credit and blame for picks it never
made — which does not add noise, it silently answers a different question.

#### Finished picks, and why they must be graded to their exit

A position you sold has to be measured to the **exit**, not to today's price. Measured to
today it reports a return nobody earned, which is why `verdict_log` carries
`exit_date`/`exit_price`: set, they mean the result is final, the last outcome row is the
one priced at the sale, and the weekly snapshot stops marking it.

This is not a detail. Before it existed, closed positions had to be left out of the
scoreboard altogether — and that is not a neutral omission, because **the picks that go
wrong are the ones most likely to have been sold**. A record built only from what you
still hold reads far better than the truth. Expect the number to fall, possibly by half,
the first time exits are included. That drop is the measurement working.

The same reasoning applies to anything you cannot recover: if a broker's history does not
reach back far enough to price some old purchases, those picks are absent, and they are
absent non-randomly. Say so next to the result rather than quietly reporting the rest.

#### Q10, if you only run one

Q1–Q9 all average **picks**. Q10 follows **dollars**: for every purchase it takes the
money actually spent, on the day it was spent, and grows it by what the benchmark did
over that same holding period. Sum both sides and the difference is what the picking was
worth, in currency.

```
stream       state          n   invested     worth    if_spy   vs_spy_dollars
EVERYTHING   ALL           89     68,906   103,075    84,555          +18,520
External     sold          53     36,595    40,747    41,790           -1,043
External     still held    32     30,512    60,472    40,878          +19,594
```

Read the `sold` and `still held` split before the total. Sold rows are settled and cannot
change; open rows are marked at today's price and still can. A strong total resting
entirely on the open rows is a position, not a result.

It is also a verdict on **decisions, not a portfolio valuation** — sold positions
contribute their exit proceeds, and that money was presumably reinvested into something
else that also appears in the table.

### Which question to care about

Two of them earn attention for opposite reasons, and it is worth being clear that they
are not competing:

- **Q3 is the one you will use most.** It is actionable — a factor cited often and read
  wrongly is a specific, fixable flaw in the analyst prompt — and it reaches a usable
  sample first, because it aggregates across every ticker rather than waiting for
  similar companies to recur.
- **Q1 is the one that matters most.** It does not tell you what to fix; it tells you
  whether the analysis layer is worth running at all.

Q3 improves the tool. Q1 decides whether the tool should exist. Expect Q3 to say
something useful long before Q1 does.

#### Why Q1 is the existential one

Greenblatt's method is a basket: its edge comes from holding many screened names,
*including* uncomfortable ones. Every verdict this tool produces is a filter applied on
top of a strategy that did not ask to be filtered — and a bear/bull/analyst chain is
structurally best at building an articulate case against exactly the cheapest names.

Q1 settles that empirically. It compares the **whole screened list** against **the names
the agents approved**, using prices the ledger captured for both. If "approved" does not
beat "all", the analysis layer is subtracting value, and the honest response is to buy
the whole screen.

Nothing else in this repo can answer that, and it costs nothing extra to collect — the
screener already computes every figure.

### When to start believing any of it

Not for a while. These need roughly **30 subjects with six months or more elapsed**
before the numbers mean anything — so at a few runs a year, the first honest read is
somewhere north of a year after you start recording.

That is not a flaw, it is what "track record" means. It is also why the recording ships
before anything that consumes it: the analysis on top can be built at any time, but the
months of data underneath it cannot be recovered afterwards. Every query reports its own
`n` so you can see how far off that point you are.

Q5 is the exception, and it needs something this tool cannot do for you. It tracks no
positions, so if you want to grade the trades you actually made rather than the opinions
the system formed, record the pipeline `run_id` alongside each purchase in whatever you
already use. That id is the only link from a trade back to the analysis that justified
it, and it cannot be reconstructed later.

## Web UI (report viewer)


A separate, read-only Flask web app in [`webapp/`](webapp/) lets you browse the
stored reports: pick a ticker, choose a run (by date), and view the Bear / Bull /
Sale Advisory / Buy Case / Final reports with the Buy/Watch/Avoid recommendation and a
markdown download. The **Buy Case** tab appears only where there is one — that is, on a
`Watch` verdict.
It's designed to run on a Raspberry Pi and connects to the same database via its
own `.env`. See [`webapp/README.md`](webapp/README.md) for full details.

A run produced by `refine.py` is marked in the run picker (✓/⚠), carries a critic
standing chip next to its verdict badge, and gets a **Critic Review** tab holding
every round of the exchange — the Bear/Bull/Sale tabs on that run are borrowed from
the report it reviewed rather than duplicated. The Buy Case tab is deliberately *not*
borrowed: a review can move the verdict off `Watch`, and a buy case must never appear
on a run whose verdict is `Buy` or `Avoid`.

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
