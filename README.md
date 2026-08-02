# magic-stock-picker

A multi-agent stock research pipeline built on the Google Agent Development Kit (ADK).
It combines Joel Greenblatt's **Magic Formula** with Peter Lynch's **PEG ratio**
(a quantitative value + quality + growth screen) and an LLM research pipeline that
argues **both sides** of the investment case — a Bear Agent and a Bull Agent — before
a neutral Analyst Agent weighs them
and issues a verdict, followed by a Sale Advisor that (assuming you own the stock)
names the specific business events that would break the thesis.

## What it does

```mermaid
flowchart LR
    A["Phase A — Screener<br/>Scans the FMP universe, applies<br/>Greenblatt's eligibility gates, and<br/>ranks survivors on ROC, Earnings Yield<br/>and Lynch's PEG ratio"]
    B["Phase B — Decomposer Analysis<br/>Bear and Bull agents argue the case;<br/>a neutral Analyst Agent weighs both<br/>and issues Buy / Watch / Avoid"]
    C["Phase C — Sale Advisory<br/>Assumes the stock is already owned;<br/>names 3 measurable events that would<br/>break the original investment thesis"]
    A --> B --> C
```

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
`config.yaml` ($2.00), is enforced *between* rounds, and always reserves enough for
a revision **plus the review that must follow it** — so the report you are handed
has always been checked as it stands. The rolling daily budget still applies on top.

The refinement gets its **own** run id and its own cost row; the report it reviewed
is left untouched, and the refined one is written to
`src/reports/<TICKER>_Refined_Report_<Verdict>.md` alongside
`src/reports/<TICKER>_Critic_Review.md`. It also shows up in the
[web UI](#web-ui-report-viewer) as its own run, marked with a critic-standing chip
and a dedicated Critic Review tab — see below.

> **The critic remembers.** Every finding is stored in a `critic_memory` table and
> replayed into both agents on later rounds *and later sessions for the same
> company*, along with how it was settled. That is what stops the loop spending a
> paid round relitigating a point the analyst already conceded, or reintroducing a
> correction it made last week. See
> [`docs/09-critic-and-refinement-loop.md`](docs/09-critic-and-refinement-loop.md).

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

A run produced by `refine.py` is marked in the run picker (✓/⚠), carries a critic
standing chip next to its verdict badge, and gets a fifth **Critic Review** tab
holding every round of the exchange — the Bear/Bull/Sale tabs on that run are
borrowed from the report it reviewed rather than duplicated.

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
