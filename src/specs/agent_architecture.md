# ADK Agent System Technical Architecture & Implementation Guide

This specification guides the Coding Agent in implementing a multi-agent investment workflow using **Google ADK (Agent Development Kit)**, **Model Context Protocol (MCP)** tools, and **Python**.

---

## 1. System Overview & Sequence Workflow

The application supports five execution modes (see §8.A for the full list —
full pipeline, screen-only, from-CSV, on-demand single-ticker, and sell-check):

- **Full pipeline run** — Phase A screener feeds the Top N candidates into Phase B/C.
- **Screen-only run** — Phase A alone; refreshes the rankings CSV and stops (no cost).
- **CSV run** — skips Phase A, reusing a previous rankings CSV, then runs Phase B/C.
- **On-demand single-ticker run** — Phase B/C runs directly on one user-supplied
  ticker, bypassing Phase A. It is still recorded as its own `pipeline_runs`
  entry (containing just that one ticker) for full traceability.
- **Sell-condition check** — a Phase C follow-up for a single already-owned
  ticker; does not create a `pipeline_runs` row.

The application executes up to a 4-stage workflow (screening, then three
per-ticker phases) managed by `main.py`'s orchestration functions (there is no
single top-level "Orchestrator Agent" object — `run_orchestrator`,
`run_from_csv`, `run_single_ticker`, etc. each drive the same shared
`analyze_ticker` per-ticker logic; see §8):

```mermaid
flowchart TD
    Start(["python main.py ..."]) --> Mode{"Execution mode"}

    Mode -->|"bare / --from-csv"| PhaseA["Phase A: Screener\n(run_magic_formula_screener)"]
    Mode -->|"--screen-only"| ScreenOnly["Phase A only\nwrite rankings CSV, stop"]
    Mode -->|"TICKER"| OnDemand["On-demand single ticker\ncompute_ticker_magic_metrics"]
    Mode -->|"--sell-check TICKER"| SellCheck["Sell-Condition Check\n(standalone, Phase C follow-up)"]

    PhaseA --> TopN["Top N candidates\n(config top_n_candidates / --top-n)"]
    TopN --> PhaseB
    OnDemand --> PhaseB

    subgraph PerTicker["Phase B + C — per ticker (analyze_ticker)"]
        direction TB
        Gather["Direct tool calls (no LLM):\nSEC 10-K, FMP metrics,\nquarterly trends, verified figures"]
        Gather --> Bear["Bear Agent\n(research-instructions.md)"]
        Bear -->|bear_data| Bull["Bull Agent\n(bullish-research-instructions.md)"]
        Bull -->|bull_data| Analyst["Analyst Agent\n(neutral judge)"]
        Analyst -->|final_report| SaleAdvisor["Sale Advisor Agent\n(sale-advisor-instructions.md)\nskippable: --skip-sale-advisor"]
    end

    PhaseB --> PerTicker
    PerTicker --> Persist["Persist to Postgres:\nagent_outputs, final_reports,\nticker_runs"]
    Persist --> Reports["Write reports/*.md"]

    SellCheck --> LoadCase["Load stored SALE_CASE\n(db_get_sale_case)"]
    LoadCase --> Evaluate["Sell Check Agent\ncompares current data vs conditions"]
    Evaluate --> SellReport["reports/TICKER_Sell_Check.md\n(SELL / HOLD)"]

    ScreenOnly --> CSVFile[("magic_formula_rankings_live.csv")]
    CSVFile -.->|"--from-csv reads this later"| PhaseA
```

1. **Phase A: Magic Formula Screening**
   - **Agent:** `MagicFormulaScreenerAgent`
   - **Action:** Triggers the MCP Tool wrapping `magic_formula_starter_screener.py`.
   - **Eligibility gates** (Greenblatt's step-by-step screen — see §2.H): drop financials, utilities, funds/REITs and foreign issuers (ADRs); drop **ROA below 25%**; drop **P/E below 5**; drop anything that **announced earnings in the last 7 days**.
   - **Output:** Ranks the survivors on the Magic Formula itself (ROC + Earnings Yield), extracting exactly the **Top 30 ranked companies**.
2. **Phase B: Balanced Decomposer Analysis** (per ticker)

   **Data gathering — direct tool calls (no LLM):**
   - **SEC 10-K** (`fetch_sec_10k_data`, `edgar-tools`): Item 1 business/segment data and **>10% customer concentration notes**.
   - **FMP metrics** (`fmp_metrics_extractor`): 3-year trailing metrics, 5-year P/E average, competitor metrics, analyst consensus targets. **These are ANNUAL periods** and describe multi-year trend only — they cannot show recent deterioration.
   - **FMP quarterly trends** (`fmp_quarterly_trends`): the last **8 quarters** of income statement, cash flow, and balance sheet, plus an explicit **year-over-year comparison of each recent quarter against the same quarter one year earlier** (Q vs Q−4, so seasonality is already aligned). See §2.D for why this is a separate, mandatory feed.
   - **Magic Formula value/quality signal** (`screen_context`): ROC + Earnings Yield + rank. For on-demand single tickers (where the screener didn't run), `compute_ticker_magic_metrics` computes ROC/EY on the fly.
   - **Verified figures** (`verified_figures`): the deterministic balance-sheet ground truth — total debt, cash, market cap, enterprise value, equity, total assets, goodwill/intangibles, EBIT — plus the goodwill-inclusive ROIC companion to ROC. See §2.E.

   These are gathered once and seeded into session state (`sec_data`, `metrics_data`, `quarterly_data`, `verified_figures`, `screen_context`) for the three reasoning agents below.

   **Reasoning — three roles run in sequence over one shared session (bear, then bull, then the neutral judge):** implemented as a plain ordered list of `LlmAgent`s run one after another against the same ADK session (`output_key` handoff), not ADK's `SequentialAgent` class — see §5B "Per-agent retry" for why: it lets a mid-graph 429 retry only the role that failed instead of re-running (and re-paying for) every role before it.
   - **Agent 1: Bear Agent** — instruction = `research-instructions.md` (skeptical). Uses `fmp_stock_news` + `web_search_tool` (Tavily, bear queries) plus the SEC/metrics data to build the **bear case** → `state['bear_data']`.
   - **Agent 2: Bull Agent** — instruction = `bullish-research-instructions.md`. Runs **after** the bear agent (its Section 4 directly refutes the bear case). Uses `fmp_stock_news` + `web_search_tool` (Tavily, bull queries) plus SEC/metrics/`screen_context` to build the **bull case** → `state['bull_data']`.
   - **Agent 3: Analyst Agent (Neutral Judge)** — carries **no** skeptical prompt. Weighs `bear_data` vs `bull_data` (plus `screen_context`, `quarterly_data`, `verified_figures`) and emits a combined Markdown report with `## Recent Quarter Check`, `## Bull Case`, `## Bear Case`, `## Final Verdict`, and `## What Would Make This Wrong` sections.
        - **Recent Quarter Check (written first):** reports what the most recent quarter did year-over-year and states whether it **confirms or contradicts** the longer-term picture the two cases argue over. The verdict may not be reached without addressing a contradiction.
        - **Verdict:** exactly one of `Buy` / `Watch` / `Avoid`, written for an investor who does **not** currently own the stock (Buy = passes the screen on its own merits; Watch = not compelling now / watchlist; Avoid = actively unattractive), with a one-paragraph justification of how the two cases net out.
        - **Verdict is a basket judgement, not a stock tip.** See §2.G — the verdict line must carry the screen-pass qualifier and the report must tell the reader the strategy depends on holding many such names.
        - **Evidence weighting:** realised results outrank projections; bull catalysts must be labelled CONFIRMED or SPECULATIVE; **a `Buy` may not rest on a SPECULATIVE catalyst** (if removing every speculative catalyst collapses the bull case, the verdict is at best `Watch`); "priced in" must be substantiated or dropped.
        - **What Would Make This Wrong:** every report, whatever the verdict, names the single most likely way it is wrong as a specific observable development, and what a reader would see in a future earnings report if it were happening.
        - **Balance:** skepticism lives in the Bear Agent, balanced by an equal Bull Agent; the judge itself is neutral. This removes the structural bear bias. The anti-over-caution guidance in the verdict stance is paired with an equal anti-optimism constraint — the two failure directions are symmetric and both are guarded.
        - **Output:** written to `reports/{Ticker}_Skeptical_Analysis.md`; `bear_data`/`bull_data` are also persisted separately as `agent_outputs` (BEAR_CASE / BULL_CASE) for drill-down.

   **Handoff sequence.** Each role runs as its own `Runner` call over the same
   ADK session, so a rate-limit retry only re-runs the role that failed (see
   §5B). The `output_key` on each `LlmAgent` writes into shared session state,
   which the next agent's instruction template reads:

   ```mermaid
   sequenceDiagram
       participant O as analyze_ticker
       participant S as Shared ADK session
       participant Bear as Bear Agent
       participant Bull as Bull Agent
       participant Judge as Analyst Agent
       participant Sale as Sale Advisor Agent

       O->>S: seed ticker, company_name, sec_data,<br/>metrics_data, quarterly_data,<br/>verified_figures, screen_context
       O->>Bear: run (reads sec_data/metrics_data/screen_context)
       Bear->>Bear: fmp_stock_news + web_search_tool (bear framing)
       Bear->>S: write bear_data
       O->>Bull: run (reads bear_data + sec_data/metrics_data/screen_context)
       Bull->>Bull: fmp_stock_news + web_search_tool (bull framing)
       Bull->>S: write bull_data
       O->>Judge: run (reads bear_data + bull_data + verified_figures)
       Judge->>S: write final_report (verdict: Buy/Watch/Avoid)
       alt not --skip-sale-advisor
           O->>Sale: run (reads final_report + verified_figures)
           Sale->>Sale: fmp_stock_news + web_search_tool (adverse events)
           Sale->>S: write sale_data
       end
       O->>S: read final state (bear_data, bull_data,<br/>final_report, sale_data)
       O->>O: reconciliation gate + persist to Postgres
   ```

   **Post-generation — reconciliation gate (no LLM):** after the graph completes, every agent-written section is checked against `verified_figures`; contradictions are logged and surfaced in the report. See §2.E.

3. **Phase C: Sale Advisory** (per ticker)
   **Sale advisor - a agent that finds advserse business events:**
   - **Agent: Sale Advisor agent**: - instruction = `sale-advisor-instructions.md`. Uses `fmp_stock_news` + `web_search_tool` (Tavily)
   - **Thresholds must be anchored.** Every numeric sell trigger has to be derived from `verified_figures` or `quarterly_data`, and the report must print the **current actual value beside each threshold** ("total debt above $32B — currently $29.3B"). A trigger calibrated off a wrong baseline is worse than no trigger: it silently cannot fire. This is not hypothetical — see §2.E.

4. **Sell-Condition Check** (on-demand, single stock — Phase C follow-up)
   **Sell-condition evaluator - tests whether the prior sale conditions are now met:**
   - **Agent: Sell Check agent** — no instruction file; carries a self-contained prompt. Loads a stored `SALE_CASE` via `db_get_sale_case(ticker, run_id)` (a **specific run's** conditions when a run is pinned, otherwise the **most recent**), gathers **current** FMP metrics (`fmp_metrics_extractor`) **and quarterly trends (`fmp_quarterly_trends`)** by direct call, plus live `fmp_stock_news` + `web_search_tool` research, then marks each sale condition **MET / NOT MET / UNCLEAR** with sourced evidence and emits a `## Sell Recommendation` (`SELL` if **any** condition is clearly met, else `HOLD`).
   - Sale conditions are routinely written in quarterly terms ("two consecutive quarters of negative growth"), which annual metrics cannot answer either way — hence the quarterly feed here as well as in Phase B.
   - Runs standalone for one ticker (not in the analysis graph). It is a lightweight flow: it reads the prior conditions and writes `reports/{Ticker}_Sell_Check.md`, but does **not** create a `pipeline_runs`/`ticker_runs` record, so it never appears in the web UI's run lists.
---

## 2. MCP Tools & Analytical Guardrails

The coding agent **must wrap underlying Python scripts into standard MCP (Model Context Protocol) tool wrappers** so Google ADK agents can invoke them natively.

Subsections A–C cover the data tools. Subsections **D–G cover the guardrails**:
the feeds and deterministic checks that constrain what the reasoning agents are
able to claim. Each one exists because of a specific observed failure, recorded
inline so the constraint is not later removed as redundant.

### A. Screener MCP Tool (`magic_formula_starter_screener.py`)
- Create an MCP server/tool wrapper `run_magic_formula_screener` around `magic_formula_starter_screener.py`.
- The tool must execute the script, read the resulting `magic_formula_rankings_live.csv`, extract the Top 30 ranked companies, and return them in JSON format to the `MagicFormulaScreenerAgent`.
- Ranking is **three** factors, not two: Greenblatt's ROC and earnings yield plus
  Lynch's `1/PEG` (§10). `compute_ticker_magic_metrics` computes the same figures for a
  single ticker on demand, and per §2.I both paths must always emit the same fields.

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

### D. Quarterly Trends MCP Tool (`fmp_quarterly_trends`) — the recency feed

`fmp_metrics_extractor` returns **annual** periods. A company whose last full
fiscal year looked healthy can be contracting sharply in its two most recent
quarters, and nothing in the annual feed can reveal that. The agents were
therefore structurally incapable of noticing recent deterioration — not
reluctant to, unable to.

`fmp_quarterly_trends(ticker)` closes that gap:

- Pulls **8 quarters** of income statement, cash flow, and balance sheet.
- Joins the three statements **by period end date**, so a missing or out-of-order
  statement can never misalign one quarter against another.
- Emits `yoy_comparison`: each of the last 4 quarters against the **same quarter
  one year earlier** (Q vs Q−4). Comparing to the immediately preceding quarter
  would confuse seasonality with deterioration.
- Normalises capex to a positive spend figure (FMP reports it negative), so
  "capex rose" reads the same direction as the source filings.

**Prompt contract (`RECENCY_MANDATE`, shared by bear, bull, analyst, sell-check):**
every agent must examine the most recent quarter before writing any conclusion,
must state whether it **confirms or contradicts** the multi-year annual trend, and
must quote the year-over-year percentage changes in revenue, operating income, and
operating cash flow. Missing quarterly data is a **gap in the evidence**, never
confirmation that recent quarters were fine.

### E. Verified figures & the reconciliation gate

**The failure this prevents.** In FISV run `f37d47ef` the bear agent asserted
**$36.9B** of total debt for a company carrying **$29.3B** — it had duplicated the
market-cap figure it quoted two tables earlier. Nothing checked it. The wrong
number propagated into the bull case's refutation, into the final verdict's
reasoning, and into a sale-advisory sell trigger of "total debt above $32.0B" —
a threshold set *above* the actual level, which therefore could never fire. Three
downstream documents were built on a number that never existed.

**Two mechanisms, both deterministic (no LLM):**

1. **`VERIFIED_FIGURES` (before generation).** `_format_verified_figures(candidate)`
   builds an authoritative block from the same balance sheet and income statement
   the Magic Formula ratios were computed from, and injects it into **every** agent
   prompt (bear, bull, analyst, sale advisor). The `VERIFIED_FIGURES_MANDATE`
   fragment forbids agents from stating a conflicting total debt, cash, market cap,
   enterprise value, or share count; instructs them to prefer the verified figure
   over any source that disagrees and note the discrepancy; and explicitly warns
   against confusing market capitalisation with total debt.

2. **Reconciliation gate (after generation).** `_reconcile_agent_figures()` scans
   each agent's prose for dollar amounts presented as **current** total debt,
   market cap, or enterprise value and flags any deviating more than
   `RECONCILE_TOLERANCE` (10%) from the verified figure. Findings are logged as
   warnings and appended to the report as a **`## Data Reconciliation Warnings`**
   table, so a reader can see that a figure above contradicts the filings. This
   runs *after* generation rather than constraining it because the failure being
   guarded against is only visible once the prose exists.

**Associating a label with its amount.** The naive rule — *nearest dollar figure to
the label* — is wrong, and wrong in a way that fires constantly on **correct**
reports. It produced three warnings on a VICR run whose figures were all accurate:

| Sentence | Naive rule reported |
| :--- | :--- |
| "TTM EBIT of **$88.35M** on a verified Enterprise Value of $8.91B" | EV off by 99% |
| "Cash of **$453.58M** vs. Total Debt of $7.61M" | debt off by 5,862% |
| a peer table whose row *above* EV ended `… \| $6.78B \|` | EV off by 23.9% |

The rules that replace it:

1. An amount **immediately before** the label wins first — no intervening words
   ("$36.9 billion debt burden", "($8.91B Enterprise Value)"). Direct adjacency is
   the strongest available signal.
2. Otherwise the first amount **after** the label, but only when joined to it by
   pure connective tissue — whitespace, markdown emphasis, a table pipe, a
   parenthetical gloss, or a linking verb (`of`, `is`, `stands at`, …). A comma or
   any other word means a new clause began and the amount belongs to something
   else: *"…$7.61M total debt, giving it roughly $446 million in net cash"*.
3. Never across a line break; at most one table-cell boundary, so a label reaches
   its own cell but never a peer column or the row above.
4. **Threshold words are excluded from the connector list** (`above`, `below`,
   `exceeds`). "Total debt above $32B" is a sell trigger, not a claim about today —
   and the sale advisory is written almost entirely in that form.

**Deliberate scope limits** — a gate that cries wolf gets ignored, so it stays
silent on:
   - **Changes, not levels:** "debt rose *by* $11 billion".
   - **Other periods:** "debt was $24.4B *in 2023*", "reaches $32B *by FY2027*".
   - **Thresholds, not levels:** "Re-leveraging *above* $6.00B Total Debt". Every
     sell trigger in a Phase C advisory names a level the company has **not**
     reached — that is what a trigger is. Threshold words are scanned only in the
     text *preceding* the amount, so a trigger in the next sentence cannot
     suppress a wrong baseline in this one ("Current total debt of $8.92B. Sell if
     debt rises above $9.50B." must still flag the $8.92B).
   - **Comparison rows (3+ amounts on one line):** a multi-year history or peer
     column set. A corpus sweep found the column order is not even consistent
     between reports — some run oldest-first, some newest-first — so attributing
     any one cell to "the current figure" is a guess.
   - **Named peers:** a ticker earlier on the line, parenthesised or bolded, that
     is not the subject's own symbol — *"**AKBA** … ($327M market cap)"*.
   - **Unit-in-header tables** (`| Total Debt ($M) | 36,908 |`): no unit beside the
     number, not checked.

**Per-field tolerance** (`_FIELD_TOLERANCE`): total debt 10%, enterprise value 20%,
market capitalisation 25%. These are different kinds of number — debt is an
accounting figure read off a filing and should barely move, while market cap is a
live price that legitimately drifts between a cited article and the run. Holding a
market price to an accounting tolerance just manufactures warnings.

A clean result means **"nothing contradicts our own basis"** — never "everything in
this report is true." Regression tests: `src/test_reconciliation.py` (offline, no
API keys required) — 38 cases, built from the real FISV, VICR, and SOLV runs.

**Gate findings are not evidence of agent error.** Three separate false-positive
classes were found *after* the gate shipped, each on a report whose figures were
entirely correct. When a warning appears, read the flagged sentence before acting
on it: the failure mode of a text-matching checker is misreading a correct
sentence, not inventing a wrong one. `VERIFIED_FIGURES` (§2.E mechanism 1) is what
actually prevents bad numbers; the gate is a backstop with known blind spots.

### F. Two return-on-capital figures, always reported together

Greenblatt's ROC divides EBIT by **net working capital + net fixed assets**,
deliberately excluding goodwill and acquired intangibles. That is the correct
question for *ranking a basket* — it measures the economics of the next dollar
invested — but for a company assembled by acquisition it produces a headline
number that says nothing about the return earned on the purchase price.

FISV is the worked example: **ROC 136.73%** against **ROIC including goodwill of
9.4%**, with goodwill and intangibles making up **59.1% of total assets**. A 14×
gap. The old bull case read the first figure as "elite capital efficiency… top
tier of capital-efficient technology companies," and the verdict cited it as part
of the margin of safety.

So `compute_company_metrics_detailed` now also returns `ROIC_InclGoodwill`
(EBIT ÷ (equity + debt − cash)) and `IntangiblesShareOfAssets`, and:

- Both figures appear in `VERIFIED_FIGURES`, with an instruction to **cite both,
  never the first alone**, and never to call the business exceptionally
  capital-efficient on the strength of the screen figure.
- Both appear in the deterministic `## Magic Formula Metrics` report section, in
  plain English, with the intangibles share explaining why the gap exists.
- The ranking itself is **unchanged** — Greenblatt's ROC remains the sort key.
  This is a disclosure fix, not a methodology change.

**When the companion cannot be computed.** Invested capital (equity + debt − cash)
goes non-positive for companies that have bought back more stock than their retained
earnings cover — shareholders' equity turns negative and there is no denominator
left. On the 2026-07-28 run this hit **3 of 42 survivors (BKNG, GRND, WINA)**, two of
them inside the top 10, carrying headline ROCs of 1,210%, 2,346% and 2,024%.

That is the worst possible place to lose the guard: the companies whose headline is
least trustworthy would be the ones reported without a counterweight. So:

- `ROIC_Unavailable_Reason` is carried alongside the null (`negative_invested_capital`
  or `not_computable`) and written to the CSV, so the omission can be explained rather
  than rendered as a bare "Not available". When that column is absent — CSVs archived
  before it existed — the cause is **inferred from negative `TotalEquity`** rather than
  left blank, and only from that evidence: a company with positive equity is never
  given the buyback explanation.
- The "cannot be calculated" statement is emitted **unconditionally** whenever the
  figure is missing. The paragraph above it promises "the companion figure below", so
  an unanswered promise reads as a dropped number and turns the substitute into an
  "instead" with no antecedent. The first cut of this feature emitted the substitute
  only when a reason was known and was caught by reading a real generated report, not
  by a test — hence the legacy-CSV cases now in `test_screen_gates.py`.
- **ROA stands in as the fallback counterweight.** It divides by *total assets* —
  which includes goodwill, intangibles and cash — so it is an even more conservative
  denominator than invested capital, and it is always computable. Both
  `VERIFIED_FIGURES` and the reader-facing report substitute it explicitly.
- ROA is **never relabelled as ROIC**. Both blocks state that it is a different,
  blunter measure serving the same sanity-check purpose. Fabricating a ROIC from a
  different denominator would defeat the point of having a second opinion at all.
- The agent mandate is correspondingly redirected: instead of "cite both", which is
  impossible here, it instructs the agent to cite ROC alongside ROA and states that
  the missing ROIC is a financing fact, **not** a data gap to be reported as one.

### G. Verdict semantics — a screen pass, not a stock tip

The Magic Formula is a **basket** strategy. Greenblatt's method buys 20–30 screened
companies and holds them roughly a year; it works *because* the winners outweigh
the names that turn out to be value traps. Individual value traps are an expected
cost of the method, not a malfunction of it.

This pipeline takes one name out of that basket and renders a verdict on it, for a
reader the prompt itself describes as "investing their own hard-earned savings."
The statistical validity of the screen does not survive that transformation, and
an unqualified "**Verdict: Buy**" reads to a non-finance reader as an instruction
to act. That framing — not any individual ticker — is the main risk this system
carries.

Requirements:

- The verdict line is written with the qualifier attached:
  `**Verdict: Buy** — screen pass; candidate for a diversified basket, not a
  single-stock recommendation.`
- The Final Verdict must contain one plain sentence stating that this is a single
  screened candidate and that the strategy's historical results depend on holding
  many such names.
- Every report ends with **`## What Would Make This Wrong`**, naming one specific
  observable development and what a reader would see in a future earnings report
  if it were happening. For a `Buy` this is the most likely thesis-breaker; for
  `Watch`/`Avoid`, the most likely proof of excess caution.

**Stored vocabulary is unchanged.** The DB `CHECK (verdict IN ('BUY','WATCH',
'AVOID','HOLD','SELL'))` constraint, `_extract_verdict()`, the report filenames,
and the web UI's counts all continue to use the bare tokens. The reframing is in
the **rendered prose**, so no stored verdict or historical run is invalidated.

### H. Screening eligibility gates — Greenblatt's step-by-step screen

Separate from the ranking, and applied before it. *The Little Book That Still Beats
the Market* gives step-by-step instructions for building the screen by hand on a free
stock screener; those steps are eligibility conditions, not the formula. The ranking
formula decides the **order**; these decide who is **in the list at all**. All are
configurable under `screening_parameters:` in `config.yaml`, and any can be disabled by
setting it to `null`.

Gate 4 below (**PEG / EPS growth**) is *not* Greenblatt's — it is Peter Lynch's, added
here because his two ratios say nothing about growth. It is documented in full in
**§10**, it is the only gate that feeds the ranking as well as the list, and it is
the only one whose thresholds were set by measurement rather than by the book (§10.I).

**Return on Assets is not Return on Capital.** They share a numerator (EBIT) and
differ entirely in the denominator:

| | Denominator | Effect |
| :--- | :--- | :--- |
| **ROC** (ranks the list) | Net working capital + net fixed assets | Excludes cash and goodwill — measures the return on the next dollar put into the business |
| **ROA** (gates the list) | **Total assets** | Includes cash and goodwill, so it is always the lower, blunter figure |

Greenblatt uses ROA in the DIY steps only because a free screener exposes ROA and not
ROC. The 25% hurdle is set high precisely to compensate for how rough the measure is.
Conflating the two is the second-easiest error in these reports after the ROC/ROIC gap
in §F, so the report section names the difference explicitly rather than printing two
similar-looking percentages side by side.

The gates, in the order they are applied:

```mermaid
flowchart TD
    U["FMP screener universe\n(up to universe_limit companies)"] --> G1{"1. Universe exclusions\nsector/industry/ETF/fund/ADR"}
    G1 -->|excluded| Drop1[["dropped\n(no API cost)"]]
    G1 -->|survives| G2{"2. ROA >= min_roa\n(25% default)"}
    G2 -->|below| Drop2[["dropped"]]
    G2 -->|survives| G3{"3. P/E >= min_pe\n(5 default; loss-makers exempt)"}
    G3 -->|below| Drop3[["dropped"]]
    G3 -->|survives| G5{"4. EPS growth > 0\nAND base margin >= 3%\nAND PEG <= 1.5\n(§10; costs 1 extra call)"}
    G5 -->|"flat/shrinking,\nbreakeven base,\nPEG too high,\nor not computable"| Drop5[["dropped"]]
    G5 -->|survives| G4{"5. Earnings in last\nexclude_recent_earnings_days?"}
    G4 -->|"reported recently"| Drop4[["dropped"]]
    G4 -->|survives| Rank["Rank survivors:\nROC_Rank + EY_Rank + PEG_Rank\n= Composite_Score\n(lower score = better Final_Rank)"]
    Rank --> Top["Top N candidates\n(warns if fewer than top_n_candidates survive)"]
```

1. **Universe exclusions** (`_universe_exclusion_reason`) — no API cost, applied to the
   screener response before any statements are fetched. Drops excluded **sectors**
   (Financial Services, Utilities), excluded **industries** (banks, insurers, asset
   managers, capital markets, mortgage lenders, closed-end funds, SPAC shells, and
   REITs), rows flagged `isEtf`/`isFund`, and **foreign issuers**. Financials and
   utilities are excluded because their balance sheets make "capital employed"
   meaningless; REITs are cut by *industry* rather than by sector so that operating
   developers and homebuilders survive.
   *ADR detection:* the screener payload has no `isAdr` flag, so this uses the two
   signals it does carry — depositary-receipt wording in `companyName` ("ADR", "ADS",
   "American Depositary…") and a non-US `country`. A **blank** country is treated as
   unknown, not foreign; a company is never dropped on missing data.
2. **ROA ≥ 25%** (`ratio_gate_reason`) — needs the financials, so it runs after the
   metrics are computed but **before ranking**, so a rejected company cannot shift the
   percentile ranks of the survivors. `roa_basis` selects the denominator's numerator:
   `ebit` (default; keeps one numerator across ROC/EY/ROA) or `net_income` (what a
   retail screener shows, materially stricter). Both are always computed and written
   to the CSV; the setting only chooses which one the hurdle tests.
3. **P/E ≥ 5** — computed as market cap ÷ TTM net income (identical to price ÷ EPS,
   but without depending on a share count FMP reports inconsistently). **Note the
   direction**: Greenblatt drops the *low* ratios. A P/E under 5 nearly always means a
   one-off event (an asset sale, a settlement) inflated a single year's earnings, so
   the "bargain" will not repeat. Loss-makers have **no** P/E and are not caught by
   this gate — they are already gone via the negative-EBIT check.
4. **EPS growth above zero, a base window above 3% net margin, and PEG ≤ 1.5**
   (`growth_gate_reason`) — **not Greenblatt's**; Peter Lynch's test, added because
   gates 2 and 3 are blind to a shrinking business. Costs one extra request per
   company, so it is spent only on what survives the gates above. Unlike every other
   gate here, **missing data is a rejection** — `1/PEG` is a ranking input, so a
   survivor without one cannot be ranked. Fully documented in **§10**: the growth
   rate is measured as multi-year TOTALS rather than two endpoint years (§10.C), the
   base window must have been a real trading period rather than a breakeven one
   (§10.D), and each threshold was set from a 189-company study (§10.I).
5. **No earnings announcement in the last 7 days** (`fetch_recent_earnings_symbols`) —
   a fresh report has not been absorbed into the price yet. Applied **only to the
   survivors** of gates 1–3, via one bulk `/stable/earnings-calendar` call over the
   trailing window, falling back to per-symbol `/stable/earnings` if that endpoint is
   unavailable. A calendar **date** in the window is sufficient; `epsActual` is not
   required, because FMP backfills results with a lag and requiring it would keep
   exactly the company that reported two days ago. If neither path works the function
   returns `ok=False` and the run **says the filter was not applied** rather than
   reporting an empty result as success.

**Severity.** On a sampled run the ROA gate alone eliminated ~95% of the universe
(the 90th percentile of EBIT-basis ROA is ≈19%), leaving ~70 candidates from a 1,773
company universe. The PEG gate then removes a large share of *those* — on the
2026-07-31 run at the original thresholds, 62 survivors became 13 (see §10.L for the
full funnel and the tuning advice). That is Greenblatt's intent
compounded with Lynch's, but Phase B analyzes the top 30, so the screener prints a
**warning when fewer than 30 companies survive** — at that point the screen is not
stricter, it is mistuned. That warning fired on the first run with both gates
active, at 11 survivors.

### I. The two candidate shapes — a standing source of silent failure

A "candidate" reaches the report layer by two routes that **do not agree on key names
or value types**, and every mismatch between them fails silently — a section simply
does not render, and nothing errors:

| | Screener CSV (`--from-csv`, full pipeline) | Single ticker (`compute_ticker_magic_metrics`) |
| :--- | :--- | :--- |
| Ratio fields | raw floats: `ROIC_InclGoodwill` | percent strings: `ROIC_InclGoodwill_Pct` |
| Missing value | `NaN` (via pandas) — **truthy** | `None` — falsy |

Three defects came from this one seam:

1. **The ROC/ROIC companion never rendered on batch runs.** Both formatters read only
   the `_Pct` form, which the CSV does not contain. So on every full-pipeline and
   `--from-csv` run the goodwill-inclusive companion was skipped — in the report AND
   in `VERIFIED_FIGURES`. The §F guard, written specifically to stop a four-digit ROC
   being read as elite efficiency, was inert on precisely the runs it existed for.
2. **`NaN` is truthy**, so `if roa_pct:` passed on a missing value and would have
   printed `nan` into a report.
3. A substitute figure rendered with its explanation missing, because the branch that
   explained it keyed off a column absent from older CSVs.

The countermeasures are structural, not spot fixes:

- **`_normalize_candidate()`** derives the `_Pct` fields from their raw ratio columns
  before anything reads them. Normalising (rather than renaming CSV columns) keeps
  previously archived CSVs readable by `--from-csv`.
- **`_present()`** collapses `None`, `NaN`, `""` and the strings `"nan"`/`"none"` to a
  single falsy value, and every report field goes through it.
- Any statement the report *promises* (e.g. "the companion figure below") must be
  answered **unconditionally**, with the explanation degrading rather than the whole
  block disappearing.
- `test_screen_gates.py` asserts all three against both candidate shapes, including a
  legacy CSV missing the newer columns.

**When adding a field to the screener output, add it to BOTH paths and to
`_RATIO_TO_PCT_FIELD` if it is a ratio.** A field added to only one path will be
missing from half of all runs and will not raise.

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

> **This section is the original scaffolding sketch and does not match the shipped
> implementation.** It predates the decision (§2, §8) to gather SEC/metrics data via
> direct tool calls with no LLM in the loop, so `sec_agent`, `metrics_agent`, and
> `search_agent` below were never built — there is no LLM agent wrapping SEC/FMP
> data gathering in `main.py`. `screener_agent` was also never built as an `Agent`;
> the screener runs as a direct call to `run_magic_formula_screener()` inside
> `run_orchestrator`/`run_screen_only`. The actual reasoning agents are `bear_agent`,
> `bull_agent`, `analyst_agent`, `sale_advisor_agent`, and `sell_check_agent`,
> defined in `main.py` (see [`docs/02-agents-and-prompts.md`](../../docs/02-agents-and-prompts.md)
> for the real definitions with line references). Kept here only as historical
> context for the original design intent.

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
2. `agent_outputs` — raw per-step outputs, keyed by `(run_id, ticker, agent_type)`; `agent_type` ∈ `SEC_DATA`, `QUANT_METRICS`, `BEAR_CASE`, `BULL_CASE`, `SALE_CASE`. Optional `embedding vector(768)`.
3. `final_reports` — the neutral analyst's combined report + `verdict` (`BUY`/`WATCH`/`AVOID`; legacy `HOLD`/`SELL` still valid) + embedding + `analysis_key` (the reuse fingerprint).
4. `ticker_runs` — a lean per-ticker index of runs (`ticker`, `run_id`, `company_name`, `verdict`, `run_date`, `magic_rank`), **built to drive the web UI**. No report text is duplicated here; the UI joins back to `agent_outputs`/`final_reports` on `run_id`. `initialize_database()` backfills it from existing `final_reports`.

```mermaid
erDiagram
    pipeline_runs ||--o{ agent_outputs : "run_id"
    pipeline_runs ||--o{ final_reports : "run_id"
    pipeline_runs ||--o{ ticker_runs : "run_id"

    pipeline_runs {
        uuid run_id PK
        text status
        text[] top_30_tickers
        int model_requests
        bigint total_tokens
        numeric total_cost_usd
    }
    agent_outputs {
        uuid output_id PK
        uuid run_id FK
        varchar ticker
        varchar agent_type "SEC_DATA / QUANT_METRICS / BEAR_CASE / BULL_CASE / SALE_CASE"
        text raw_content
        jsonb metadata
        vector embedding "768 dims, zero-vector on failure"
    }
    final_reports {
        uuid report_id PK
        uuid run_id FK
        varchar ticker
        varchar verdict "BUY / WATCH / AVOID (legacy HOLD/SELL)"
        text markdown_report
        vector embedding
        text analysis_key "sha256(ticker | balance-sheet date | prompt version)"
    }
    ticker_runs {
        uuid id PK
        varchar ticker
        uuid run_id FK
        text company_name
        varchar verdict
        timestamptz run_date
        int magic_rank "NULL for on-demand runs"
    }
```

### B. Database MCP Tools
1. `db_store_agent_output(run_id, ticker, agent_type, raw_content, metadata_json, embed=True)`:
   - Inserts into `agent_outputs`. When `embed=True`, generates a `gemini-embedding-001` vector (pinned to 768 dims) on `raw_content`. **SEC/metrics are stored with `embed=False`** (raw provenance, low search value); **bear/bull/sale cases are embedded** for semantic search.
2. `db_store_final_report(run_id, ticker, verdict, markdown_report)`:
   - Normalizes the verdict to `BUY`/`WATCH`/`AVOID` (default `WATCH`), embeds the report, and inserts into `final_reports`.
3. `db_store_ticker_run(run_id, ticker, company_name, verdict)`:
   - Upserts the `ticker_runs` index row (idempotent per `(ticker, run_id)`).
4. `db_search_historical_reports(query_text, ticker="", limit=5)`:
   - Embeds `query_text` and runs a cosine similarity search (`<=>`) against embedded rows.
5. Run lifecycle: `db_create_pipeline_run` (parent row, created first for FK integrity) and `db_finalize_pipeline_run` (terminal status + usage/cost).

### C. Per-Ticker Persistence Flow (`analyze_ticker`)
- SEC 10-K, FMP annual metrics, and FMP quarterly trends are gathered by **direct tool calls** and stored via `db_store_agent_output(..., embed=False)` — 100% fidelity, no LLM in the loop.
- The bear and bull agents' outputs are stored (embedded) as `BEAR_CASE` / `BULL_CASE`.
- **The reconciliation gate runs before persistence** (§2.E): every agent-written section is checked against `verified_figures`. Findings are logged as `RECONCILIATION` warnings and a `## Data Reconciliation Warnings` table is appended to the stored report. A clean pass is logged too, so the check's absence is distinguishable from its success.
- After the neutral analyst produces the report and a `Buy`/`Watch`/`Avoid` verdict, call `db_store_final_report`, then `db_store_ticker_run` to index it for the UI, and write the report file to `reports/{Ticker}_Final_Report_{Verdict}.md`.
- Report section order as stored: `## Magic Formula Metrics` (deterministic) → the analyst's `## Recent Quarter Check` / `## Bull Case` / `## Bear Case` / `## Final Verdict` / `## What Would Make This Wrong` → `## Data Reconciliation Warnings` (only when the gate fired).
- The Phase C sale advisor's output is stored (embedded) as `SALE_CASE` and written to `reports/{Ticker}_Sale_Advisory.md`.
- The four analytical reports (`BEAR_CASE`, `BULL_CASE`, `SALE_CASE`, and the final report) are stamped with a **run_id banner** at the top before storage (a blockquote with the `run_id` + ticker). This makes the `run_id` visible in the web viewer so it can be recorded against a purchased lot (the value `--run` pins for the sell-condition check). SEC/metrics rows are left unstamped. The banner does not affect verdict parsing (it sits above the `## Final Verdict` section) and is negligible for the embeddings.


## 5A. Cost Accounting

**Price the resolved model, never the alias.** Every agent is configured with
`gemini-flash-latest` — a *moving* alias that Google repoints at each new Flash
release. A flat price in `config.yaml` therefore goes stale silently the moment
the alias moves. It did: the alias now resolves to **`gemini-3.6-flash`** while
the config still priced Gemini 2.5 Flash ($0.30/$2.50), understating real spend by
**~2.4x** — two runs billed at $0.55 were reported as $0.233.

Rules:

- `llm_pricing.models` in `config.yaml` is keyed by **resolved model version**,
  read from `event.model_version` on each response and accumulated per model in
  `usage["by_model"]`.
- **An unpriced model is an ERROR, not a default.** Its tokens are excluded from
  the estimate and logged loudly, so the figure is a known lower bound rather than
  a wrong number presented as fact. Silently falling back to another model's rate
  is how the original understatement went unnoticed.
- Prices carry a `confirmed` flag. Unconfirmed prices log a warning naming the
  rates in use and telling the reader to check them against the Cloud Billing SKU.
- Per-run logs print a **per-model breakdown**, so a repointed alias is visible in
  the output rather than silent.

**Confirmed rates** (`gemini-3.6-flash`, derived from Cloud Billing SKU rows on
2026-07-27 — exact division, not published-rate guesswork):

| SKU | Tokens | Charge | Rate |
| :--- | ---: | ---: | ---: |
| Generate content input token count | 789,522 | $1.184282 | **$1.50 / 1M** |
| Generate content output token count | 135,146 | $1.013594 | **$7.50 / 1M** |
| Generate content cached input token count | 212,609 | $0.031888 | **$0.15 / 1M** |

**Cached input is a third SKU.** Gemini bills repeated prompt prefixes at one tenth
the input rate, and `promptTokenCount` **includes** them — so full-rate input is
`prompt - cached`. Cached ran **21%** of prompt tokens. Ignoring the split
overstates cost; ignoring caching as a lever leaves the single largest saving on
the table (see §5B).

**Thinking tokens.** These models bill `thoughtsTokenCount` at the *output* rate,
and they dominate: a probe of `gemini-3.6-flash` showed **388 of 478** output
tokens were thoughts, and a two-token "say hi" produced 134. They are folded into
the output counter — dropping them would understate cost several-fold. At $7.50/1M
this is the expensive side of the ledger.

**Billing data lags by hours.** A report pulled mid-day covers only the runs that
had already posted. On 2026-07-27 the export showed $2.23 against 1,002,131 prompt
tokens while the pipeline had recorded 2,952,571 for the day — the report covered
~34% of the day's work. Repricing the first 6 runs (through 18:42) at the rates
above gives **$2.29 against the posted $2.23, a 2.7% match**, which is what
confirms both the rates and the token accounting. Never treat a same-day export as
the day's total: `calibrate_pricing.py` warns when the SKU token counts fall well
short of what was recorded.

**Embeddings.** Four `text-embedding-004` vectors are generated per ticker
(`BEAR_CASE`, `BULL_CASE`, `SALE_CASE`, final report). These were billed but never
counted. `mcp_server.EMBEDDING_USAGE` accumulates payload size and
`analyze_ticker` drains it via `_collect_embedding_usage`. The API returns no usage
metadata for embeddings, so tokens are approximated at 4 chars/token — immaterial
to the total, but counted approximately rather than not at all.

**Calibration.** `src/calibrate_pricing.py` reconciles recorded token counts
against a real bill:

```
python calibrate_pricing.py --runs 2 --actual 0.55                       # ratio check
python calibrate_pricing.py --runs 2 --input-charge X --output-charge Y  # exact solve
```

A combined total is one equation in two unknowns and cannot pin both rates; the
two Cloud Billing SKU amounts (Billing → Reports, grouped by SKU) solve it exactly.

---

## 5B. Cost Reduction — what worked, and what did not

Measured on repeated FISV runs at the confirmed `gemini-3.6-flash` rates. Baseline
before any of this: **$0.497/ticker**.

| Change | Effect | Kept? |
| :--- | :--- | :--- |
| Prune duplicated payload | `quarterly_data` 9,404 → 2,132 chars (−77%); `metrics_data` 23,413 → 11,689 (−50%) | ✅ |
| Per-agent 429 retry | a mid-sequence rate limit no longer re-runs and re-pays for completed roles | ✅ |
| Pin the model version | prevents a repeat of the silent 3.5x cost rise | ✅ |
| Stop replaying the session transcript | input 183,202 → 129,617 tokens; LLM $0.3881 → $0.2869 (−26%) | ✅ |
| Explicit context caching | **cost went UP**, 32% → 6% cached | ❌ reverted |

**Payload pruning.** `quarterly_data` was written as nested JSON that repeated every
value twice — once in `quarters`, again inside `yoy_comparison` as
`current`/`year_ago`. It is now two markdown tables: values appear once, field names
appear once instead of per-quarter. It gained an interest-expense row while
shrinking 77%, and the deterioration arc is legible at a glance. `metrics_data` was
64 FMP ratio fields × 5 years plus 47 key-metric fields × 3 years; whitelisted to
the ~35 and ~33 the research instructions actually reference, and serialised with
compact separators rather than `indent=2`.

**Per-agent retry.** `run_pipeline` previously wrapped the whole `SequentialAgent`
in a retry loop, so one 429 in the sale advisor discarded completed bear, bull and
analyst work and paid for all of it again — a full extra ticker. The four roles now
run as separate runners over one shared session, retrying only the role that
failed. Handoff is unchanged: each agent's `output_key` writes into the shared
session exactly as `SequentialAgent` did internally. Tokens burned on a failed
attempt are now counted rather than dropped.

**Transcript replay (`include_contents='none'`).** The shared session that makes the
`output_key` handoff work also accumulates every earlier role's events, and ADK's
default `include_contents='default'` sends that whole transcript to each agent —
rewritten as user-role context, one entry per tool call carrying the full Tavily/FMP
payload. Nothing here reads it: every input the agents use is templated from session
state. So each downstream role was paying to receive, in raw form, what its
instruction already carried in distilled form. All four roles are now set to
`include_contents='none'` (current turn only — the user message plus that agent's own
tool calls, which is what a tool loop needs). The saving scales with position in the
pipeline, which is the signature of the replay: bull −24%, analyst −62%, sale advisor
−78% on input tokens; bear is flat per call, having no history to replay. Verdict on
the control ticker was unchanged (WATCH). Note the reuse fingerprint in
`_prompt_version()` hashes instructions only, so this change does not invalidate
reports cached under the old behaviour.

Per-role token/cost lines are now logged (`[TICKER agent_name] Tokens: ...`), which
is what made the above measurable — a ticker-level total cannot say which of the four
roles moved.

### Explicit context caching — tried, measured, reverted

Gemini bills a repeated prompt prefix at $0.15/1M against $1.50/1M, so pinning the
~15k-token shared block looked like the largest single lever. It does not work here:

1. **ADK cannot create the cache.** Every attempt failed with
   `Failed to create cache: contents are required` — visible only at WARNING level
   inside ADK. An explicit cache cannot be built from a system instruction alone,
   and all our data sits in the agent `instruction` while `contents` is nearly empty.
2. **Fixing that made it worse.** Moving the block into the first conversation
   message (so it lands in `contents`) was implemented and measured: cached share
   fell **32% → 6%** and cost rose **$0.32 → $0.43** per run.

The reason is that the long, stable system instruction was exactly what Gemini's
**implicit** caching had been matching on. Relocating it into `contents` — which
grows and shifts as each agent appends to the session — destroyed that stable prefix
and bought no explicit cache in exchange.

**Conclusion: leave the data in the instruction.** Implicit caching needs no
configuration and covers ~32% of prompt tokens. `context_cache.enabled` is `false`
in config.yaml and the plumbing (`_build_runner`, `CONTEXT_CACHE`) is retained for
when ADK or the API supports caching a system instruction directly — re-measure
before trusting it.

### Budget guard

A 30-ticker screen is ~$11 at current rates, and the cost accounting was wrong by
3.5x for a while without anyone noticing — so an unbounded run is a real risk.
Ceilings live under `budget:` in config.yaml and are checked **between tickers**,
never mid-ticker: stopping half way through would leave a partial analysis and
still have paid for it.

- `per_run_usd` — one pipeline run's accumulated cost.
- `per_day_usd` / `day_window_hours` — a **rolling** window, not a calendar day.
  Prior spend comes from `db_spend_since`, so the ceiling accounts for earlier runs
  rather than just the current one.
- `on_exceed: halt` stops cleanly before the next ticker and finalizes the run as
  **`BUDGET_EXCEEDED`**, so a short run is distinguishable from a completed one in
  the history. Everything already analyzed is kept. `warn` logs loudly and continues.
- `--no-budget` disables the ceilings for one invocation.

The single-ticker path checks once up front and returns **before** creating the
`pipeline_runs` row, so a blocked run leaves no orphan record.

If the spend lookup fails the guard logs a warning and falls back to the per-run
ceiling alone — a guard that fails open silently would be worse than none, because
the run would look protected when it is not.

### Duplicate-run skip

`analyze_ticker` checks for a reusable report **before any billed work**. The
fingerprint (`analysis_key`, stored on `final_reports`) is:

```
sha256(ticker | balance-sheet date | prompt version)
```

- **Balance-sheet date, not today's date.** Two runs a day apart against the same
  filing are the same analysis; a new filing moves the date and forces a fresh run.
- **Prompt version** is a hash of every agent instruction plus the shared prompt
  fragments, so editing the verdict stance invalidates reuse instead of silently
  serving reports written under the old rules.
- **No balance-sheet date → reuse disabled** for that ticker. Pay again rather than
  serve a report whose provenance cannot be established.

`reuse.max_age_hours` (default 24) bounds staleness from what the fingerprint
*cannot* see: the agents do live news and web research, so an otherwise-identical
run a week later would genuinely surface different information. Keep it well under
a week. `--force` bypasses reuse entirely.

A reused report is still indexed under the new `run_id` in `ticker_runs` and
rewritten to `reports/`, so the web UI and the local files behave as if it ran.

Regression tests: `src/test_guards.py` (offline, no billed work).

### Embeddings were silently broken

Found while adding embedding cost accounting: `get_embedding` imported
`google.generativeai` (the legacy SDK, **not installed** — the project depends on
`google.genai`) inside a bare `except: pass`. Every call fell through to a zero
vector in complete silence. **All 248 stored `agent_outputs` embeddings and all 98
`final_reports` embeddings are zeros**, so similarity search has never worked.
Additionally `text-embedding-004` is retired and now 404s.

Fixed to use `google.genai` with `gemini-embedding-001` (3072 dims by default,
pinned to 768 via `output_dimensionality` to match `vector(768)`), and the fallback
now logs an ERROR naming the row as unsearchable instead of failing silently.
**Historical rows still hold zero vectors and need re-embedding to become
searchable.**

---

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
1. **Dynamic Logic:** The MCP tool wrapping `magic_formula_starter_screener.py` must be modified to read its algorithmic variables (e.g., `MIN_MARKET_CAP`, `EXCLUDED_SECTORS`) directly from `specs/config.yaml`. This includes every eligibility gate in §2.H — `min_roa`, `roa_basis`, `min_pe`, `exclude_recent_earnings_days`, `excluded_industries`, `exclude_adr`, `allowed_countries` — each of which can be set to `null` to disable that gate without touching code.
2. **Agent Limits:** The orchestrator agent must dynamically read `top_n_candidates` (e.g., 30) from `config.yaml` to determine how many companies to process in Phase B. `--top-n N` overrides it for a single invocation — depth is the dominant cost lever (~$0.37/ticker) and is the one setting that legitimately varies run to run, so it belongs on the command line rather than requiring a config edit before each run. The override also moves the screen-only short-list warning threshold, so the warning tracks what THIS run will actually analyze.

## 8. ORCHESTRATOR & WORKFLOW

Phase B distinguishes **data-relay** steps from **reasoning** steps:

- **Data relays — direct tool calls (no LLM).** SEC 10-K extraction
  (`fetch_sec_10k_data`) and FMP metrics (`fmp_metrics_extractor`) are
  deterministic tools, so `analyze_ticker` calls them **directly** and seeds
  their raw output into session state as `sec_data` / `metrics_data`. This gives
  100% data fidelity (no LLM summarization) and costs zero tokens.
- **Reasoning — an ordered list of four `LlmAgent` roles run over one shared
  ADK session (`PIPELINE_AGENTS` in `main.py`), not ADK's `SequentialAgent`
  class:** `bear_agent` (`research-instructions.md`, → `bear_data`) →
  `bull_agent` (`bullish-research-instructions.md`, → `bull_data`; runs after
  bear so §4 can refute it) → `analyst_agent` (neutral judge → `final_report`)
  → `sale_advisor_agent` (Phase C, → `sale_data`). Both advocates read
  `{sec_data}` / `{metrics_data}` / `{screen_context}`; the bull agent also
  reads `{bear_data}`; the judge reads `{bear_data}` / `{bull_data}`; the sale
  advisor reads `{final_report}`. `ticker`, `company_name`, `sec_data`,
  `metrics_data`, and `screen_context` are seeded into session state before the
  graph runs. Handoff between roles works exactly as `SequentialAgent` would
  (each agent's `output_key` writes into the shared session state the next
  agent templates), but running the roles as separate `Runner` calls instead
  of one `SequentialAgent` lets `_run_pipeline_async` retry only the role that
  hit a 429 rather than discarding and re-paying for every earlier role — see
  §5B "Per-agent retry".

Phase C makes predictive analysis and uses **reasoning**:
(`sale-advisor-instructions.md` + FMP news search + Web search) -> `SALE_CASE`; runs after Phase B is complete and `analyst_agent`
produced its final analyst report. Uses the Investment thesis from final analyst report (in context with output key `final_report`). 
Ignores the verdict, assumes that the stock is purchased and advises on sale conditions for that stock.

**Model tiers:** all four reasoning agents run on the thinking
`gemini-flash-latest` (they do genuine research/weighing). SEC/metrics are direct
tool calls, so no lite/data-gathering agents remain.

The per-ticker logic is factored into `analyze_ticker(run_id, ticker,
company_name, screen_context, candidate)`, which does the direct tool calls, runs
the graph, and persists SEC/metrics/bear/bull outputs plus the final report. All
execution modes call it, so persistence and verdict logic are identical across
modes. `candidate` (the raw EY_Pct/ROC_Pct/EBIT_Basis/Final_Rank dict) is used to
deterministically prepend a '## Magic Formula Metrics' section to every final
report — independent of the LLM — so those figures, their basis (TTM vs Annual
fallback), and the components they are derived from (EBIT / enterprise value /
capital employed) are always present, falling back to "Not available" per-field
rather than omitting the field.

`compute_company_metrics_detailed` (in `magic_formula_starter_screener.py`) is
the source of those figures. It returns a structured `{"ok": False, "reason":
..., "message": ...}` when a company cannot be ranked, so the report can state
the plain-English cause — most often that the company is loss-making, since both
ratios divide by operating profit and Greenblatt's screen excludes unprofitable
companies. `calculate_company_metrics` remains a thin None-on-failure wrapper for
the screener's universe loop, which only needs success/failure.

### A. Execution Modes

1. **Full pipeline run** (`run_orchestrator`)
   - Runs Phase A (screener → writes the rankings CSV), then Phase B, followed by Phase C over the Top N.
2. **CSV run — skip Phase A** (`run_from_csv`)
   - Phase A (the full FMP universe scan) is slow. This mode **reuses the
     rankings CSV from a previous run**, picks the Top N, and runs Phase B and Phase C — no
     screener re-run. Accepts an optional CSV path (defaults to the screener's
     `magic_formula_rankings_live.csv`).
3. **On-demand single-ticker run** (`run_single_ticker`)
   - Skips Phase A entirely for a single user-supplied ticker. Only runs Phase B and Phase C

The three modes above create a `pipeline_runs` row and produce records that are
structurally identical and fully queryable by `run_id`.

3a. **Screen-only run** (`run_screen_only`) — Phase A ALONE
   - The exact inverse of `--from-csv`: runs the screener, writes the rankings CSV,
     logs the top N with their ROC/EY/ROA/PE, and stops. **No agents, no
     `pipeline_runs` row, no database writes, nothing billed** — so it never appears
     in the web UI.
   - This exists because the two halves have completely different cost profiles: FMP
     is subscription-metered (Phase A costs time only), while Phase B/C is the billed
     LLM work at roughly $11 per 30-ticker run. Welding them together forced anyone
     who wanted a fresh ranking to pay for an analysis they had not asked for.
     Screening and analysis can therefore run on **different cadences**: refresh the
     list as often as useful, and pay only in the periods you intend to act on it.
   - Warns when fewer candidates cleared the gates than `top_n_candidates`, so that
     is discovered before a paid run is scheduled rather than after it produces a
     short list.
   - Rejects combination with any Phase B/C flag (`--from-csv`, `TICKER`,
     `--sell-check`, `--skip-sale-advisor`, `--force`) rather than silently ignoring
     it.

4. **On-demand sell-condition check** (`run_sell_check`)
   - A separate single-stock flow (Phase C follow-up). Loads a stored `SALE_CASE`
     — a **specific run's** when `run_id` is passed (the run you bought under),
     otherwise the ticker's most recent — and evaluates whether those conditions are
     now met against current data, advising `SELL`/`HOLD`. It does **not** create a
     `pipeline_runs`/`ticker_runs` row (nothing new appears in the web UI); it only
     reads the prior conditions and writes `reports/{Ticker}_Sell_Check.md`.

### B. Invocation

```
python main.py                       # full pipeline (Phase A screener + Phase B + Phase C)
python main.py --screen-only         # Phase A ONLY: refresh the rankings CSV; no agents, no cost
python main.py --from-csv            # skip Phase A; Phase B/C on top N from the rankings CSV
python main.py --from-csv PATH       # same, from a specific CSV file
python main.py TICKER                # on-demand Phase B/C for one ticker
python main.py TICKER "Company"      # on-demand with an explicit company name
python main.py --sell-check TICKER            # test if TICKER's latest sale conditions are now met (Sell/Hold)
python main.py --sell-check TICKER --run RUN_ID  # ...against a SPECIFIC run's conditions (the run you bought under)

# Phase C is optional on ANY Phase B mode (~1/4 of the per-ticker cost):
python main.py --from-csv --skip-sale-advisor    # bear -> bull -> analyst, no sale advisor
python main.py --from-csv --top-n 12             # analyze only the top 12 this run
```

**Phase separation.** `--screen-only` (A alone) and `--from-csv` (B/C alone) are
inverses and together do the work of a bare `python main.py`. `--skip-sale-advisor`
separates C from B: the sale advisor is the LAST role in `PIPELINE_AGENTS` and
nothing downstream reads its output, so dropping it truncates the graph cleanly
rather than leaving a hole.

**Reuse safety.** `skip_sale_advisor` is folded into `_analysis_key`. Without it a
cheap Phase-C-less report would satisfy the reuse check for a later full run, and
`db_copy_ticker_outputs` would copy a set of outputs with no `SALE_CASE` — the ticker
would look complete in the web UI with an empty Sale tab, and `--sell-check` would
find no conditions to test. The two variants therefore live under different
fingerprints and neither can be served in place of the other.

`run_screen_only()`, `run_from_csv(path)`, `run_single_ticker(ticker, company_name)`,
and `run_sell_check(ticker, company_name, run_id)` are also directly importable for
driving these modes from a service endpoint or notebook.

### C. Ordering Constraint

The parent `pipeline_runs` row MUST be created before any `agent_outputs` or
`final_reports` inserts, because those tables carry a foreign key onto
`pipeline_runs(run_id)`. The pipeline status is set to `COMPLETED` after the
run finishes.

### D. Which `SALE_CASE` should the sell-condition check use?

Sale conditions are **exit criteria for a specific entry thesis** — the concrete
business events that would break *the reason you bought*. So the check should be
anchored to the run **under which the position was actually purchased**, not
whichever analysis is newest:

- A later re-analysis (e.g. a single-ticker run that now says `Watch`/`Avoid`)
  produces a **new** `SALE_CASE` derived from a **different** thesis you never
  acted on. Evaluating against it silently moves the goalposts and mixes two
  distinct theses.
- The later verdict downgrade is itself meaningful — but it is a *separate*
  signal ("a fresh analysis no longer likes it"), a reason to re-underwrite, not a
  substitute for the exit criteria of the thesis you hold.

**Behavior:** `run_sell_check` defaults to the ticker's *most recent* `SALE_CASE`
(via `db_get_sale_case`), which is correct only when no re-analysis has happened
since purchase. For a held position that has since been re-analyzed, **pin the
purchase run** with `--run RUN_ID` so the check evaluates the exact conditions you
committed to. The `run_id` is the identifier of the pipeline/on-demand run under
which you bought (shown in the run logs and stored on every `SALE_CASE`). It is
exactly `public.pipeline_runs.run_id`; a full pipeline run shares one `run_id`
across all its tickers, and `SALE_CASE` is keyed by `(run_id, ticker)`, so a single
stored `run_id` per position resolves that ticker's conditions.

**Position tracking is external (decoupled).** The agent does not track holdings.
An external positions store (e.g. a `lots` table in a separate `stock_tracker`
schema in the same database) owns the mapping from a purchased lot to the analysis
run it was bought under — store `public.pipeline_runs.run_id` on the lot (FK with
`ON DELETE SET NULL` so purging analysis history never deletes a real purchase).
That app then drives the sell-check by passing the stored `run_id` — either via the
CLI (`--run RUN_ID`) or by importing `run_sell_check(ticker, company_name, run_id)`.
The agent stays ignorant of the positions schema.


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
bodies are fetched on demand from `agent_outputs` (`BEAR_CASE` / `BULL_CASE` / `SALE_CASE`)
and `final_reports`:

```mermaid
flowchart LR
    Browser["Browser\n(templates/index.html)"] -->|"GET /api/tickers"| App["Flask app\n(webapp/app.py)"]
    Browser -->|"GET /api/runs?ticker="| App
    Browser -->|"GET /api/pipeline-runs"| App
    Browser -->|"GET /api/pipeline-run?run_id="| App
    Browser -->|"GET /api/report?ticker=&run_id="| App
    Browser -->|"GET /download, /download-run"| App

    App -->|"SELECT ... FROM ticker_runs"| DB[("PostgreSQL\n(same DB the agent writes to)")]
    App -->|"SELECT ... FROM agent_outputs\nWHERE agent_type IN (BEAR_CASE, BULL_CASE, SALE_CASE)"| DB
    App -->|"SELECT ... FROM final_reports"| DB
    App -->|"markdown.markdown(...)\nserver-side render"| Browser
```

The webapp never writes to the database — it is strictly read-only, and it
reads its own `webapp/.env` (`DATABASE_URL`, `PORT`) rather than the pipeline's.

**By ticker**
- `GET /api/tickers` → distinct tickers (alphabetical; optional `?q=` substring).
- `GET /api/runs?ticker=` → that ticker's runs, newest first, with `run_date` + `verdict`.

**By pipeline run**
- `GET /api/pipeline-runs` → **multi-ticker** runs, newest first, each with its
  `run_date`, `ticker_count`, and a Buy/Watch/Avoid breakdown (`GROUP BY run_id`
  over `ticker_runs`, `HAVING COUNT(*) > 1`). Single-ticker on-demand one-offs
  are excluded — this view is for value-discovery screens.
- `GET /api/pipeline-run?run_id=` → all tickers in that run, each with
  `company_name`, `verdict` and `magic_rank`, ordered **Buy → Watch → Avoid, then
  by rank** (`magic_rank ASC NULLS LAST`, ties and unranked rows alphabetical).
  Verdicts outside that vocabulary (legacy `HOLD`/`SELL`, or a null from an
  interrupted run) sort after all three rather than among the Buys. The ordering
  lives in SQL, not in the browser, so the CSV download and the on-screen list
  cannot drift apart.
- `GET /download-run?run_id=` → the run's tickers + recommendations as a
  `.csv` attachment (`Ticker,Company,Recommendation,MagicFormulaRank`), in that
  same order.

**Shared (report drill-down + download)**
- `GET /api/report?ticker=&run_id=` → server-rendered HTML for the bear, bull, sale and
  final reports plus the verdict.
- `GET /download?ticker=&run_id=&kind=bear|bull|sale|final` → the raw markdown as a
  `.md` attachment for the viewing device.

### C. User Flows
- **By ticker:** pick a ticker (alphabetical dropdown / 3-letter type-ahead) →
  pick a run (sorted by date, date shown) → view Bear / Bull / Sale / Final reports as
  rendered markdown with the Buy/Watch/Avoid recommendation badge → optionally
  download any report.
- **By pipeline run:** pick a run (sorted by date, newest first, ticker count
  shown) → see every analyzed ticker under a Buy / Watch / Avoid group heading,
  ordered by Magic Formula rank within each group, with its verdict badge, its
  rank (or `—` for runs predating rank recording) and a
  "View reports" link into that ticker's reports **for that run** → optionally
  download the whole run's decisions as CSV. The report drill-down reuses the
  shared viewer (a "Back to run" control returns to the decisions list).
- **Learn the terms:** an embedded lemonade-stand simulator with sliders that flow a
  change through the income statement, cash flow, balance sheet and the Magic Formula
  ratios, plus a hover/tap glossary on every line item.

### D. The Learn tab and its numeric contract

`webapp/static/learn/lemonade-cheat-sheet.html` is a standalone, downloadable page and
is the **authoritative source of the worked figures**. The simulator in
`templates/index.html` must reproduce them exactly at default slider positions:

| Figure | Value |
| :--- | ---: |
| EBIT | $120.00 |
| Capital employed | $132.00 |
| Total invested capital | $212.00 |
| Total assets | $252.00 |
| Enterprise value | $460.00 |
| Earnings yield | 26.09% |
| Return on capital | 90.91% |
| ROIC including goodwill | 56.60% |
| **Return on assets** | **47.62%** |

Rules for keeping the two in step:

- A term added to the simulator must also be added to the cheat sheet in **all three**
  places it appears there — the terms section, the ratios table, and the formula index
  — or the downloadable page silently disagrees with the live one.
- Every table row carries a `termKey`, and `showPopover()` **returns silently** when
  the key has no glossary entry — an orphan key is an invisible dead tooltip, not an
  error. Both directions must be checked: no key without an entry, no entry unreached.
- ROA and ROC are the pair most easily confused (see §2.H), so the ROA glossary entry
  and cheat-sheet term both lead with the distinction and show both figures computed
  off the same $120.00 of profit — 47.62% against 90.91% — with the gap attributed to
  the $40.00 cash and $80.00 goodwill that ROC excludes and ROA counts.

---

## 10. The PEG ratio — Peter Lynch's growth test

Introduced from *One Up on Wall Street*. It is the **third** ranking factor, added
alongside Greenblatt's two, and simultaneously a Phase A eligibility gate.

Three thresholds govern it, and each was set from measurement rather than taste. The
study behind them is in §10.I; each threshold's own subsection states what it does,
what happens if it is wrong in either direction, and the evidence for the number:

| Setting | Value | What it constrains |
| :--- | :--- | :--- |
| `max_peg` | **1.5** | how expensive, relative to growth, a company may be (§10.E) |
| `min_base_net_margin` | **3%** | whether the starting period was a real trading period (§10.D) |
| `max_growth_rate_for_peg` | **60%** | the largest growth rate the PEG may divide by (§10.F) |
| `eps_growth_method` | **"sums"** | how the growth rate itself is measured (§10.C) |

### A. Why it was added

Greenblatt's two ratios ask whether a company is **cheap** (earnings yield) and
whether it is a **good business** (return on capital). Neither asks whether it is
**growing**, and a company whose profits are shrinking gets *cheaper on paper every
year* — the earnings yield rises as the earnings fall away. That is the value trap the
Magic Formula buys by design and leaves the basket to average out.

Not hypothetical here. On the screen immediately before this change the **#1 ranked
company was IRWD**, on a 20.14% earnings yield and a 1,673% ROC — with earnings per
share that had gone from $3.21 (FY2021) to $0.15 (FY2025), through a $945M write-off
in between.

**A caution about the mechanism, because it is easy to state backwards.** Falling
earnings do not *raise* the earnings yield — they shrink its numerator. IRWD ranked
first because its **price fell far further than its profit did**:

| | End 2022 | Now | Change |
| :--- | ---: | ---: | ---: |
| Operating profit (EBIT) | $250.34M | $201.84M (TTM) | −19% |
| Market value | $1.90B | $0.61B | −68% |
| EBIT ÷ market value | 13.2% | 33.1% | 2.5× higher |

The screen's cheapness measure is a snapshot — profit now over price now — and it
cannot distinguish *cheap because the market is wrong* from *cheap because the
profits are going away*. Lynch's ratio divides the P/E by the growth rate, so a
company counts as cheap only relative to how fast it is compounding.

### B. The PEG formula

$$\text{PEG} = \frac{\text{P/E}}{\text{growth rate expressed in PERCENTAGE POINTS}}$$

**The unit of the denominator is the single easiest thing here to get wrong.** Lynch's
rule is that a fairly priced company's P/E equals its growth rate, so a P/E of 20
against 20% growth is a PEG of exactly 1.0 — the denominator is `20`, not `0.20`.
Dividing by the decimal gives a PEG a hundred times too large and the ceiling would
reject the entire market. `test_screen_gates.py` asserts the `20 / 20% = 1.0` identity
for exactly this reason.

`compute_peg` returns **None**, never a number, when either input is missing or
non-positive. A negative P/E or a shrinking EPS produces a *negative* PEG, and a
negative sorts as the cheapest thing on the list — the precise inversion this ratio
exists to expose. On IRWD the naive arithmetic would have been
`6.81 ÷ (−46.14) = −0.15`, ranking the worst business best.

| | Source | Period |
| :--- | :--- | :--- |
| P/E | market cap ÷ net income (already computed for gate 3) | **TTM** (annual fallback) |
| Growth rate | annual `/stable/income-statement` EPS, §10.C | fiscal years |

The growth figure therefore lags the P/E it is divided by. That is **stated in the
report, not hidden**: `EPS_Current_Date` and `EPS_Base_Date` are carried through to
the CSV and both report blocks so the span is always visible.

### C. How the growth rate is measured — `eps_growth_method`

**Default `"sums"`: the CAGR of the last N years' TOTAL EPS against the prior N
years' total.**

$$\text{Growth Rate} = \left( \frac{\sum_{i=0}^{N-1} \text{EPS}_{\text{FY}-i}}{\sum_{i=N}^{2N-1} \text{EPS}_{\text{FY}-i}} \right)^{\frac{1}{N}} - 1$$

The alternative, `"endpoint"`, is the original two-point form
$(\text{EPS}_{\text{now}} / \text{EPS}_{N\ \text{ago}})^{1/N} - 1$ and remains
selectable in `config.yaml`.

**Why the change.** A two-point CAGR is decided entirely by which two years the
calendar happens to pick, and it steps straight over everything in between. That is
wrong in *both* directions, and the same defect produced both of the outliers that
drove this design:

- **GRND** read `$0.0054 → $0.49` as **+349% a year**, ignoring that it lost money in
  two of those three years.
- **IRWD** read `$0.96 → $0.15` as **−46% a year**, a window that happened to bracket
  a one-off $945M write-off while the trailing twelve months were recovering ~4×.

Totalling whole windows fixes both, because a loss year is counted at its real
negative value instead of being skipped:

```
GRND   recent 3 years:  0.49 + (−0.74) + (−0.32)  =  −$0.5700
       base   3 years:  0.0054 + 0.0331 + (−0.60) =  −$0.5615
```

Both totals negative → no growth rate, no PEG, excluded. Note that the *blind*
arithmetic here would have produced `(−0.57)/(−0.5615) = 1.0151 → +0.5% growth`: two
losses dividing into apparent growth. The non-positive-base guard fires first
precisely so that cannot happen.

**Units.** The two sums are dollars, so the division cancels them and leaves a plain
multiple; the Nth root turns "multiple over N years" into "multiple per year"; and
subtracting 1 turns a multiple into a rate. NVDA end to end:

| Step | Calculation | Result | What it is |
| ---: | :--- | ---: | :--- |
| 1 | recent 3 = 4.90 + 2.94 + 1.19 | $9.03 | dollars |
| 2 | base 3 = 0.17 + 0.38 + 0.17 | $0.72 | dollars |
| 3 | 9.03 ÷ 0.72 | 12.54 | unitless multiple |
| 4 | 12.54 ^ (1/3) | 2.32 | multiple **per year** |
| 5 | 2.32 − 1 | **132%** | growth **rate** |
| 6 | cap: 132% > 60% (§10.F) | 60 | denominator used |
| 7 | P/E 30.6 ÷ 60 | **0.51** | PEG |

**Measured effect** (189-company sample, §10.I): among companies the endpoint form
called >50% growers, the median fell from 66.1% to 41.8% — AU 111%→17%, IBM 84%→21%,
WLKP 67%→25% — while genuine compounders stayed high (NVDA 207%→132%). It is also
*more* generous where the endpoint form was unfair: `no_eps_growth` rejections fell
from 56 to 42, because one weak endpoint year no longer condemns a company.

**Cost.** `"sums"` needs `2N` annual filings rather than `N+1`, so it excludes more
recently listed companies. That is the trade-off accepted for the robustness above.

Other implementation decisions, all in `fetch_eps_growth`:

- **Diluted EPS preferred**, and *every* year used must come from the same measure —
  mixing a diluted numerator with a basic base would read the dilution itself as a
  change in earnings power.
- The window length comes from the two `date` fields, not the row index. A span within
  0.2 of a whole number is snapped to it so the published figure is exactly the formula
  above and can be hand-checked; a genuinely irregular span (a changed fiscal year end)
  keeps its real length.
- **`MAX_ANNUAL_HISTORY_AGE_DAYS = 640`**, far looser than the 200-day
  `MAX_STATEMENT_AGE_DAYS` used for quarterly data. An annual `date` is a fiscal *year
  end*, published only after the filing lag, so a current filer legitimately shows a
  period end up to ~15 months old.

### D. The base window must be real — `min_base_net_margin` (3%)

A compound rate is only as meaningful as what it starts from, and a base near zero
blows it up while saying nothing about the business. GRND's base year earned **$852K
on $195M of revenue — a 0.4% net margin**: technically profitable, economically
breakeven, and a denominator that turns any recovery into a three-digit rate.

**An absolute dollar floor on EPS cannot express this.** Ten cents of EPS is nothing
for a $500 stock and enormous for a $2 stock; the same number means different things
in different sectors and at different price levels. So the test is **net margin over
the base window** — scale-free, and comparable across the whole universe:

| Base-window net margin | |
| :--- | ---: |
| p25 | 4.3% |
| **p50** | **10.5%** |
| p90 | 28.1% |
| GRND | **0.4%** |
| NVDA | 16.2% |
| LLY | 21.9% |

3% sits below the ordinary range while clearing genuine compounders comfortably.

**A rejected alternative, recorded so it is not re-proposed.** "Base EPS as a share of
the company's own peak EPS" looks like the natural measure and is actively harmful: it
scores **NVDA at 3.5%**, because NVDA's base year *was* its trough. That rule would
have excluded the single best compounder in the sample. Any base-quality test must
measure the *quality of the year*, not its size relative to what came later — growing
a long way from a low base is the thing we are trying to find, not the thing we are
trying to exclude.

### E. The valuation ceiling — `max_peg` (1.5)

Lynch's fair-value line is PEG 1.0. **Raised from 1.2 to 1.5 on evidence.** Every
company in the sample sitting in the 1.2–1.5 band:

| | Sector | P/E | Growth | PEG | Loss years in window |
| :--- | :--- | ---: | ---: | ---: | ---: |
| NMRK | Real Estate | 18.6 | 14.8% | 1.26 | 0 |
| JNJ | Healthcare | 23.0 | 17.9% | 1.29 | 0 |
| VMC | Basic Materials | 32.2 | 23.5% | 1.37 | 0 |
| KDP | Consumer Defensive | 20.4 | 14.8% | 1.37 | 0 |
| MCK | Healthcare | 21.1 | 15.3% | 1.38 | 0 |
| MPLX | Energy | 12.1 | 8.7% | 1.38 | 0 |
| OKE | Energy | 16.9 | 12.2% | 1.38 | 0 |
| CMG | Consumer Cyclical | 31.1 | 21.2% | 1.47 | 0 |
| ISRG | Healthcare | 43.7 | 29.2% | 1.50 | 0 |

Steady growth of 8.7–29.2%, **zero loss years**, and **not one of them there because
its growth had been capped**. The speculative high-growth cohort does not live in that
band at all: a capped denominator plus a low multiple already puts it well under 1.2.

So 1.2 was excluding **quality, not risk** — the opposite of its intent. On the live
screen the same effect is visible in ADBE (PEG 1.42) and LLY (1.48), both admitted at
1.5 and both rejected at 1.2.

**Why this does not open the door to the "P/E 150 on 100% growth" case.** It cannot:
that company's PEG is `150 ÷ 60 = 2.5` once the cap in §10.F applies. **The cap, not
`max_peg`, is what blocks an extreme multiple** — see the implied-P/E-ceiling note
below.

### F. The growth cap — `max_growth_rate_for_peg` (60%)

A ceiling on the growth rate the PEG **divides by**. It does not touch the rate that
is *reported*, and it does not touch the "is it growing at all" gate; both use the
measured figure. Mechanically it substitutes one input:

```python
if growth_rate > MAX_GROWTH_FOR_PEG:
    return MAX_GROWTH_FOR_PEG, True     # denominator becomes 60
return growth_rate, False               # otherwise untouched
```

**Grounded in measured persistence, not in a guess about what is "sustainable".** For
every company with 7+ years of filings, §10.I compares the growth it had recorded as
of three years ago against what it *actually went on to deliver*:

| Growth 3 years ago | n | Median next 3 years | Went negative |
| :--- | ---: | ---: | ---: |
| shrinking | 32 | **+23.7%** | 16% |
| 0–10% | 31 | +7.3% | 29% |
| 10–25% | 37 | +7.5% | 32% |
| 25–50% | 23 | +2.8% | 48% |
| 50–100% | 11 | **−20.4%** | **73%** |
| >100% | 4 | **−26.3%** | **75%** |

Growth does not merely fail to persist — it **inverts**, and the fastest growers are
the worst subsequent performers. The >50% cohort was dominated by cyclicals at a cycle
peak (FANG, CVX, VLO, XOM, MPC, NUE, STLD, CF, CRS). Meanwhile the 95th percentile of
*actually realised* 3-year EPS CAGR is 63%. A denominator much above 60 is therefore
pricing in growth the data says almost never arrives.

Three properties make the cap safe:

1. It caps the **denominator only**.
2. Capping can only ever **raise** a PEG, never lower it, so it cannot flatter a
   company into the list.
3. When it bites, **both report blocks say so** — otherwise a reader checking
   `PEG = P/E ÷ growth` would find the arithmetic does not work and assume an error.

**Implied P/E ceiling — the consequence to keep in view.** Work `max_peg` backwards
through a capped denominator:

```
capped PEG = P/E ÷ 60
pass needs   PEG ≤ 1.5   ⟹   P/E ≤ 1.5 × 60 = 90
```

and an uncapped company (growth ≤ 60%) needs `P/E ≤ 1.5 × growth ≤ 90` as well. So
**no company can enter the list with a P/E above `max_peg × max_growth_rate_for_peg ×
100`** — 90 at the defaults — however fast it grows. This is a larger effect than the
artifact the cap was first introduced for, and it is the reason to review the two
knobs **together** rather than either alone.

**The cap is now a backstop, not the main defence.** Once `"sums"` (§10.C) and the
base-margin rule (§10.D) are in place, the cap changed **no pass/fail outcome at all**
on the sample — 36 companies passed with it and 36 without. It still affects
*ranking*, which is why it is retained: without it NVDA's PEG would be ~0.23 rather
than 0.51 and it would rank far higher on a rate the persistence table says will not
hold.

`EPSGrowth` (measured) and `EPSGrowth_ForPEG` (used) are both carried;
`EPSGrowth_Capped` is the flag the report renders off.

### G. The gate

`growth_gate_reason()`, applied **after** `ratio_gate_reason()` because it costs one
extra request — the ROA/PE gates clear ~95% of the universe first, turning ~2,500
extra calls into roughly a hundred. Checks run in this order, and the order matters:

| Reason | Meaning |
| :--- | :--- |
| `growth_unavailable` | no growth rate could be computed (cause in `EPSGrowth_Unavailable_Reason`) |
| `no_eps_growth` | growth ≤ `min_eps_growth` (default 0) — flat or shrinking |
| `base_year_breakeven` | base window below `min_base_net_margin` |
| `base_margin_unavailable` | the base window's margin could not be measured |
| `peg_unavailable` | growing, but no P/E to divide (loss-making on a TTM basis) |
| `peg_above_max` | PEG > `max_peg` |

The growth test deliberately precedes the base-window test, so a *shrinking* company
is reported as no-growth rather than misattributed to a margin problem.

**Missing data is a REJECTION here — a deliberate departure from the rule everywhere
else in the screener**, where a company is never dropped for a provider gap. Two
reasons: `1/PEG` is a ranking input, so a survivor without one cannot be ranked
against the others; and the gate asks a company to *demonstrate* growth, which one
without a computable rate has not done. To keep that from silently thinning the list,
every rejection carries its own reason and the run prints the split:

```
Lynch PEG gate (sums growth over 3 years): 12 dropped for no EPS growth, 8 dropped
for a base window below 3% net margin, 21 dropped for a PEG above 1.5, 14 dropped
because a growth rate, margin or PEG could not be computed.
```

`EPSGrowth_Unavailable_Reason` narrows `growth_unavailable` further —
`non_positive_base_eps`, `non_positive_current_eps`, `insufficient_history`,
`stale_eps_history`, `no_eps_reported`, `no_period_dates`, `period_too_short`,
`fetch_failed` — and `_no_peg_reason()` in `main.py` renders each as plain English in
both report blocks. Most are **facts about the company, not data outages**. It also
covers the two cases where the growth rate is perfectly well measured and the PEG
still cannot exist — a **negative** rate, and a grower with no P/E — because
conflating those with a data gap misinforms in opposite directions.

### H. Ranking

```mermaid
flowchart LR
    ROC["ROC_Rank\n(desc)"] --> MF["MagicFormula_Score\n= ROC_Rank + EY_Rank"]
    EY["EY_Rank\n(desc)"] --> MF
    PEG["PEG_Rank\nfrom 1/PEG (desc)"] --> C["Composite_Score\n= MagicFormula_Score + PEG_Rank"]
    MF --> C
    C --> F["Final_Rank\n(lower is better)"]
```

`MagicFormula_Score` keeps its original meaning — Greenblatt's two ranks — so the pure
formula stays visible in the CSV next to the composite the list is actually ordered by.

The growth factor enters as **1/PEG** rather than PEG so that all three inputs point
the same way (higher is better) and one `ascending=False` ranks them all. Ranking
1/PEG descending is arithmetically the same order as PEG ascending; the column reads
as a score rather than as a cost, which is what a score should add up from.

`PEG_Rank` uses `na_option="bottom"`. The gate already drops every company without a
PEG, but if that gate is disabled in `config.yaml` a missing PEG must rank **last**
rather than turn the composite into `NaN` and take `Final_Rank` down with it.

### I. The study behind the thresholds

Reproducible by running **`tools/analyze_growth_persistence.py`** (in `tools/`, not
`src/` — it is an offline instrument, not part of the pipeline), which is the study
itself, committed so a threshold is never changed by feel. It re-derives every table
below from live data and prints each configured value next to what the fresh sample
implies. **189 companies**, stratified across all 9 eligible sectors (largest by
market cap within each), 8 fiscal years each; one screener call plus one annual
`/stable/income-statement` call per company, no LLM calls and no writes.

- **Persistence** (§10.F table): 138 companies with 7+ years and positive-EPS
  endpoints. Past growth = CAGR(FY-6 → FY-3); realised growth = CAGR(FY-3 → FY0). No
  forecast required — both windows are historical.
- **Realised-growth distribution**, which is what a cap should be set against:
  p50 7.7%, p75 21.2%, p90 39.0%, **p95 63.3%**, p99 111.3%.
- **Formula comparison** (§10.C): endpoint vs sums on the same companies.
- **Base-window margin distribution** (§10.D), and the rejected base-vs-peak measure.
- **Design simulation**, whole gate end to end over the same 189:

| Design | Passing |
| :--- | ---: |
| shipped at the time (endpoint, cap 50%, peg 1.2, no base rule) | 27 |
| + `max_peg` 1.5 only | 36 |
| + `"sums"` formula | 44 |
| + base margin ≥ 2% | 39 |
| **current (sums, cap 60%, peg 1.5, margin 3%)** | **36** |
| same but with no cap at all | 36 |

The last two rows are the evidence for §10.F's "backstop, not main defence" claim.

**Caveats.** One sample, one point in time, US large/mid-cap, ~21 companies per
sector; the persistence buckets above 50% hold only 11 and 4 companies respectively,
so treat their *direction* as established and their exact medians as indicative. Run
`tools/analyze_growth_persistence.py` before moving any threshold, and periodically
regardless — a threshold tuned to one market should not persist silently into a
different one. The README carries the operator-facing version of these instructions.

### J. Where the figures surface

Per §2.I, every field goes out on **both** candidate paths — the screener CSV (raw
ratios) and `compute_ticker_magic_metrics` (percent strings) — and `EPSGrowth` /
`BaseNetMargin` are registered in `_RATIO_TO_PCT_FIELD` so `_normalize_candidate`
derives the display form from the CSV. `PEG` is not in that map: it is a plain
multiple, not a percentage, and both paths already emit it as `PEG_Ratio`.

| Surface | Content |
| :--- | :--- |
| Rankings CSV | `EPSGrowth`, `EPSGrowth_Pct`, `PEG`, `PEG_Ratio`, `InversePEG`, `PEG_Rank`, `Composite_Score`, `EPSGrowth_ForPEG`, `EPSGrowth_Capped`, `EPS_Current`, `EPS_Base`, `EPS_Current_Date`, `EPS_Base_Date`, `EPS_Basis`, `EPS_Window`, `EPS_Window_Years`, `BaseNetMargin`, `EPSGrowth_Years`, `EPSGrowth_Unavailable_Reason` |
| `VERIFIED_FIGURES` (every agent) | P/E, growth rate, PEG, the EPS totals behind them, the base-window margin, and any cap or unavailability note. Covered by `VERIFIED_FIGURES_MANDATE`, so an agent may not contradict them |
| `MAGIC_FORMULA_CONTEXT` | growth rate and PEG as the third leg of the bull signal, plus `Composite_Score` |
| `## Magic Formula Metrics` (report) | growth and PEG as first-class ranking figures with full workings, in the plain-English house style |
| Analyst instruction | the Final Verdict **must** state the PEG and growth rate and what they mean, or say why they could not be computed |

Two wording rules the report code enforces, because both would otherwise be plain
misstatements of what was divided:

- Under `"sums"` the figures are **multi-year totals**, so `_eps_window_phrase()`
  describes them as totals across N years — never as "earnings per share in the year
  ended X".
- When the cap bites, the reported growth rate and the PEG **do not divide into each
  other**. Both blocks say so explicitly and give the rate actually used.

The analyst is also told that the PEG is **backward-looking**: where the most recent
quarter contradicts the multi-year growth, the quarter is the present-tense evidence
and the PEG is the stale one. Without that, a low PEG would vouch for a business that
is currently shrinking — the same failure §2.D's recency mandate exists to prevent.

### K. Verification

**Formula, against hand computation.** Every figure below was cross-checked against a
hand computation from the raw FMP annual filings; `compute_peg` and `fetch_eps_growth`
agreed to 1e-9 in every case.

| Ticker | Endpoint growth | Sums growth | Base margin | PEG | Outcome |
| :--- | ---: | ---: | ---: | ---: | :--- |
| NVDA | 206.6% | 132.3% | 26.1% | 0.51 | passes (capped) |
| EXEL | 70.6% | 47.1% | 13.0% | 0.34 | passes |
| WLKP | 67.0% | 24.9% | 5.6% | 0.24 | passes |
| BKNG | 29.4% | 62.6% | 12.3% | 0.40 | passes (capped) |
| DECK | 29.5% | 30.5% | 14.5% | 0.43 | passes |
| HRB | 10.9% | 25.2% | 11.5% | 0.30 | passes |
| ADBE | 18.3% | 9.7% | 32.1% | 1.42 | passes at 1.5, **rejected at 1.2** |
| LLY | 51.7% | 28.9% | 22.1% | 1.48 | passes at 1.5, **rejected at 1.2** |
| GRND | +349.4% | **−$0.57 total** | −1.6% | — | `growth_unavailable` |
| IRWD | −46.1% | **−$6.29 total** | 66.7% | — | `growth_unavailable` |
| HALO | 21.1% | — | — | 1.33 | passed 1.5, rejected at 1.2 |
| CROX | — | — | — | — | `growth_unavailable` (loss-making) |

**Full pipeline**, three companies through the complete bear → bull → analyst → sale
advisor graph (EXEL, WLKP, GRND under the earlier thresholds): 11 model calls each,
$1.43 total, $0.48/ticker. Every PEG printed in the reports reproduced
`P/E ÷ min(growth, cap) × 100` exactly, the reconciliation gate passed on two of
three, and both report blocks disclosed the cap where it bit.

Notably, **WLKP's bear agent independently raised the base-year objection** —
"reflects a rebound from a low earnings baseline rather than fundamental business
growth, meaning the low 0.12 PEG ratio overstates actual expansion potential" — which
is the §10.D disclosure doing its job through the reasoning layer rather than only in
the footnotes. Under `"sums"` that same company's growth rate falls from 67% to 25%,
so the objection is now priced into the figure itself.

### L. Severity, and the standing tuning question

**Current settings** (sums, cap 60%, `max_peg` 1.5, margin 3%), full run 2026-08-01:

```
1,769 eligible universe
−1,221  ROA below 25%
    −0  P/E below 5
    −9  no EPS growth
    −0  base window below 3% net margin
   −24  PEG above 1.5
   −13  growth rate / margin / PEG not computable
    −4  reported earnings this week
    ————
    12  final
```

Ranked list: HRB, BKNG, WLKP, BSM, EXEL, DECK, ADBE, NVDA, MEDP, APPF, NVR, LLY.
**ADBE (PEG 1.42) and LLY (1.48) are admitted here and would have been rejected at
1.2** — the §10.E effect, live. GRND and IRWD are both gone. Only 2 of the 12 had
their growth capped (BKNG 62.6%, NVDA 132.3%), confirming §10.F's "backstop, not main
defence".

**`base_year_breakeven` fired zero times, and that is expected rather than
disappointing.** The `"sums"` formula reaches the same companies first: a breakeven
base year is almost always adjacent to loss years, so the base *window* totals to a
non-positive figure and the company exits at `non_positive_base_eps` (inside the 13
"not computable") before the margin test is consulted. GRND is exactly this case. The
margin rule earns its place as the guard for the case sums does **not** catch — a
company that was thinly but consistently profitable across the whole base window — and
as an explicit, named reason when it does fire. It should not be removed on the
strength of a zero count.

**Earlier run for comparison** (endpoint formula, cap 50%, `max_peg` 1.2):

```
1,769 eligible universe
  −486  not scorable (negative EBIT, no capital employed, stale, fetch errors)
1,283 scored
−1,221  ROA below 25%              →  62 left
    −0  P/E below 5                →  62 left
   −49  PEG gate                   →  13 left
    −2  reported earnings this week →  11 final
```

**The PEG gate is the second most severe cut in the screen.** At the original settings
it took the list from ~60 to 11; at the current ones, to 12.

**A short list does not break anything.** `top_n_candidates` is a CEILING, not a
quota: `run_from_csv` takes `df.head(top_n)` and `run_orchestrator` takes
`candidates[:top_n]`, both of which return all 12 rows of a 12-row list. Phase B
analyses whatever survived. The screener's short-list message is therefore a NOTE, not
a warning, and says so — it exists because a short list usually means a gate is
mistuned, and because Greenblatt's method assumes a 20-30 name basket whose
diversification a very short list weakens.

**If the list is short, `min_roa` is the lever to reach for first.** It alone
eliminates 95% of the scored universe (1,221 of 1,283), an order of magnitude more
than every other gate combined. `max_peg` has already been loosened on evidence and
loosening it further would start admitting the mean-reverting cohort in §10.F;
`universe_limit` is the other clean lever. Note also that raising `max_peg` without
revisiting `max_growth_rate_for_peg` moves the implied hard P/E ceiling (§10.F) — at
1.5 × 60 it currently sits at 90.

## 11. Phase D — the independent critic and the refinement loop

The **Producer–Critic pattern** (a.k.a. **reflection** / generator–evaluator): the
analyst produces, an independent critic evaluates, the analyst revises, repeat until
the critic is satisfied or a spend ceiling stops it. What follows is less about the
pattern (which is standard) than about the three decisions that determine whether it
is worth its cost here — what independence concretely means (§B), what makes the
loop terminate (§C, §D), and how it avoids paying to relitigate settled ground (§F).

Implementation: [`critic_agent.py`](../critic_agent.py) (agents + parsers),
[`refine.py`](../refine.py) (loop, spend control, CLI),
[`critic-instructions.md`](../critic-instructions.md) (the critic's prompt).
Developer walkthrough: [`docs/09-critic-and-refinement-loop.md`](../../docs/09-critic-and-refinement-loop.md).

### A. The gap it closes

Phases B and C already run an adversarial pair and a neutral judge. But all four
roles read the **same** pre-fetched evidence and descend from the same prompt
lineage, so nothing stands outside them. §2.E's reconciliation gate catches a
wrong **figure**; there was nothing catching wrong **reasoning** — a verdict that
does not follow from the report's own body, a Buy resting entirely on a rumoured
transaction, a "priced in" assertion with nothing behind it, a sell trigger that
would fire annually on a seasonal business.

The commit history is the argument for this. "Fix analyst rubber-stamping, peer
mislabelling, and seasonality blindness" was a batch of prompt patches, each
written after a human read a report and found the fallacy by hand. Those patches
are still the right first line of defence — they are free and they apply to every
run. The critic is the second line, for the reports you are about to act on: it
finds the ones the prompt rules did not anticipate.

### B. Independence, concretely

Independence is a property of what the critic can and cannot see, not a claim in
its prompt:

- **Its own instruction file**, written without reference to the analyst's prompt.
- **Its own tools** — FMP news/profile/metrics/quarterly plus Tavily — so it checks
  load-bearing claims against the world, not against the same cached blob.
- **No sight of the analyst's instruction.** A critic told how the report was
  supposed to be written grades against that spec; the failure being hunted here is
  invisible from inside the spec.
- It **does** get the bear and bull cases the analyst was given, because "the
  summary misrepresents the case" and "the summary drops its strongest point" are
  both real findings and neither is checkable without the originals.

`reviser_agent` inherits `analyst_agent.instruction` **verbatim** — see §11.E.

### C. Agreement is decided in Python

The critic writes `CRITIC VERDICT: AGREE|REVISE` and assigns each finding
`BLOCKING`, `MATERIAL` or `MINOR`. `extract_critic_verdict()` takes the stricter of
the two: a declared AGREE with a BLOCKING or MATERIAL finding standing is forced to
REVISE, and a missing verdict line falls back to the severities rather than being
read as agreement. `MINOR` never blocks — a critic that cannot agree over a wording
nit spends the budget and leaves the reader a report stamped un-agreed for nothing.

Every stopping decision is deterministic Python over the model's prose. None of it
is delegated to a second model call, for the same reason `_extract_verdict` is not.

### D. Why not `LoopAgent`

The stopping condition is a **spend** decision, not an agent decision: the loop
stops when it can no longer afford a revision **plus the critique that must follow
it**, which needs the running dollar total and a per-role projection — figures that
live in `main`'s cost accounting, outside any agent's context. Cost also has to be
attributed per role per round, exactly as `_run_pipeline_async` attributes it per
role per ticker (§5A), or a prompt change that doubles the critic's cost is
invisible inside a moving loop total. And the hand-off is a rewrite, not an append.
This is the same call §5B made in rejecting `SequentialAgent`, for the neighbouring
reason.

### E. Why the reviser reuses the analyst's instruction

A report out of a refinement round must obey exactly the same rules as one out of
the pipeline: the same five sections, the same Buy/Watch/Avoid vocabulary, the same
basket framing (§2.G), the same plain-English standard. Restating those rules in
`critic_agent.py` would create a second copy that drifts, and the first symptom
would be a refined report subtly different **in kind** from an unrefined one — the
hardest inconsistency to notice, because both look fine in isolation.

The reviser is told it may **rebut** a finding rather than defer to it. A critic can
misread a report, and a loop where the analyst always concedes is not a review, it
is a ratchet. The rebuttal rides after a marker
(`<<<RESPONSE TO CRITIC>>>`), is split off in Python before storage, and is shown to
the critic next round — without it, the critic cannot tell a rejected finding from an
ignored one and re-raises it for as many rounds as the budget allows.

### F. Long-term memory (`critic_memory`)

The loop's cost failure mode is repetition across rounds *and across sessions*. One
row per finding, with severity, the analyst's reply, and how it was settled;
replayed into both agents every round via `format_past_corrections()`.

Retrieved by **exact ticker + recency, not vector similarity** — the retrieval key
is known exactly, so semantic search would return approximate neighbours where an
exact answer exists, and every row would add an embedding call to a loop whose whole
design constraint is cost. This is the deliberate exception to §5's
"analytical content gets embedded" rule.

It carries **no foreign key onto `pipeline_runs`**, unlike every other table in §5.A.
The others are per-run artefacts and should cascade away with their run. This is
memory: if a finding vanished with the run it was learned from, the same fallacy
would return on the next refinement and be paid for again.

### G. What the reader is told

The refined report is assembled like any other (deterministic metrics section,
analyst prose, reconciliation warnings) plus one section the pipeline never writes.
Agreed: the round count, the review cost, and an explicit statement that agreement
means neither agent could find a fault — **not** that the call is right. Not agreed:
"**The independent critic has NOT agreed this report**", the count of blocking and
material objections still standing, the stopping reason, and the critic's full final
review reproduced underneath.

Agreement does not require the critic to have found *nothing*. A MINOR finding never
blocks agreement (§C's severity rule), which means a revision never
runs for it and its "Required fix" is never applied — the first live run surfaced
exactly this: HRB's report overstated quarterly net income growth by not flagging an
$84.1M one-off tax settlement, graded MINOR because operating profit and cash flow
carried the thesis without it. Silently dropping a finding just because it did not
block agreement would be worse than not finding it: the reader would believe nothing
was found. So the agreed banner (`refine._agreed_banner`) lists every outstanding
MINOR by title, with its "Required fix" line carried verbatim (`finding_summary` in
`critic_agent.py` — it prefers "Required fix" over "Why it is wrong" precisely
because a MINOR reaches the reader only when it was NOT corrected, so what the reader
needs is the fix that never happened, not a critique of prose they are about to
read). A clean agreement with zero findings still says nothing extra.

The original run's rows and report file are left untouched. The refinement gets its
own `run_id`, its own `pipeline_runs` cost row, and its own `final_reports` row with
an **empty `analysis_key`** — a refined report must never be served by the
duplicate-run skip (§5B) in place of a fresh analysis, because its provenance is a
review session and reuse keys on filings.

`pipeline_runs.refines_run_id` (nullable, no FK — a refinement outliving the run it
reviewed is worth keeping) names the reviewed run. Set only by this loop, so a
non-NULL value **is** the "this is a refinement" test; there is deliberately no
second boolean that could disagree with it. With `status` it also carries the
outcome, which is what the web UI (§9) reads to show a Critic Review tab, a standing
chip beside the verdict, a marker in the run picker, and — since a refinement run
holds only its own `CRITIC_REVIEW` rows — to borrow the Bear/Bull/Sale tabs from the
reviewed run at read time instead of copying ~26KB of text and two vectors per
refinement to say nothing new.

### G2. The sale advisory after a review

The advisory (§1 Phase C) is an **output** of the final report, not an input to it,
and the critic never sees it. So a review that changes the report leaves the advisory
describing a thesis that no longer exists — and because §2.E requires every sell
trigger to be anchored to `VERIFIED_FIGURES` with the current value quoted beside it,
a figure the critic corrected can leave a threshold calibrated against a number the
pipeline itself now says was wrong.

Handled by outcome, so the cost is only paid when it buys something: **no revision**
→ the analyst's prose is byte-identical, so the existing advisory is carried forward
unchanged for $0; **a revision ran** → re-derived against the revised report
(~$0.07–0.10).

There is no third outcome by design. The revision is what makes the advisory stale,
so the two are **one commitment** and are priced as one: `_Estimator.full_round`
reserves the advisory before the loop agrees to a revision. The alternative — a
separate budget the advisory draws on — would mean `--max-budget 2.00` billing $2.12,
and a ceiling that can be exceeded by design is not a ceiling. Reserving costs
nothing in practice (~$0.12 against rounds of ~$0.20) and only binds in the session
about to need it. A "revised but unaffordable" branch survives as a defensive last
resort — carrying the old advisory with a visible staleness warning and a loud log —
for the cases the reservation cannot foresee: an advisory that costs far more than
its seed, or the rolling daily ceiling moving underneath a running session. The advisory is not itself critiqued — it has its own
guardrails and reviewing it would roughly double the loop's cost for a second-order
artefact.

Carrying it forward also repairs a broken promise. `_with_run_header` stamps the
refinement's `run_id` on the refined report and tells the reader that is the id to
record against a lot so `--sell-check --run RUN_ID` can pin the exact thesis bought
under (§8.D). A refinement wrote no `SALE_CASE`, so that command failed outright, and
the unpinned form silently resolved to the pre-review conditions.

### G3. Repairing an advisory outside the loop — `sale_advisory.py`

§G2's third outcome is rare by construction but not impossible, and it is not the
only way a report ends up without a current advisory: `--skip-sale-advisor` writes
none at all, the advisor can return nothing (§1 logs a warning and continues), and an
advisory's thresholds age even when nothing is wrong. Before, every one of those
required re-running the whole pipeline — ~$0.40, and it rewrites the report you
already reviewed — to recover one artefact.

`python sale_advisory.py TICKER [--run RUN_ID]` derives a fresh advisory from any
stored report for ~$0.08–0.10. Two placement rules, deliberate in both halves:

- **The advisory is stored on the run you name.** An advisory belongs to the report
  it was derived from; storing it under a run of its own would leave
  `db_get_sale_case(ticker, run_id)` unable to find it and the viewer unable to show
  it. A run may therefore hold more than one `SALE_CASE`; every reader takes the
  newest, and the viewer's query orders accordingly.
- **The cost is recorded as its own run.** Folding it into the target run's totals
  would rewrite what that run cost, which is what run history exists to record — and
  the target may have been finalized days earlier. The cost row carries no
  `ticker_runs` row (nothing to browse) and no `refines_run_id` (§11.G), so it is
  never mistaken for a critic review.

Note that re-running `refine.py` does **not** repair a stale advisory: the critic
agrees immediately on an already-corrected report, so no revision runs, so §G2's "no
revision → carry forward" rule carries the stale advisory forward again — this time
without its staleness warning. That is precisely why the repair is a separate
command rather than a flag on the loop.

### H. Why it is not in the pipeline

It costs several times what producing the report cost, and takes as long. Putting a
2–4 round loop behind all 30 tickers of a Phase B run would multiply an ~$11 run by
roughly the round count, for a batch in which most verdicts are "Watch" and will
never be acted on. Which names are worth that is a judgement only the operator can
make, so it is a separate command (`python refine.py TICKER`), taken deliberately,
one ticker at a time, with a ceiling named on the command line.

### I. First live runs (2026-08-01, gemini-3.6-flash)

The offline test suite (`test_critic.py`) proves the parsing, spend projection, and
banner logic behave correctly on FIXED inputs. It cannot prove the critic finds real
defects, or that the severity scale is used honestly rather than as an excuse to
either rubber-stamp everything or force revisions nobody needs. Three tickers, run
for real, against `refinement.max_budget_usd: 2.00`:

| Ticker | Source verdict | Rounds | Cost | Finding |
| :--- | :--- | ---: | ---: | :--- |
| HRB | Buy | 1, AGREE | $0.1248 | MINOR — Q3 net income +17.4% was inflated by an $84.1M one-off IRS settlement; not visible in any feed the pipeline gives its agents (`fmp_quarterly_trends` returns figures, not 10-Q narrative). |
| LLY | Watch | 1, AGREE | $0.0959 | MINOR — "What Would Make This Wrong" named no specific quarter (`critic-instructions.md`'s "Unfalsifiable 'What Would Make This Wrong'" checklist item). |
| FISV | Watch | 2, AGREE after 1 revision | $0.2556 | BLOCKING — cited Magic Formula ROC (136.73%) alone on a company where goodwill is 59.1% of assets; goodwill-inclusive ROIC is 9.4%, §2.F exactly. Plus 1 MATERIAL (market cap/P-E contradicted VERIFIED_FIGURES) and 2 MINOR. All four fixed in one revision; round 2 returned zero findings. |

FISV is the load-bearing case: its report was written on 2026-07-24, **before** the
"Fix analyst rubber-stamping, peer mislabelling, and seasonality blindness" prompt
commit, and the critic rediscovered the exact class of defect that commit exists to
prevent — without being told to look for it, from an independent read of the report
text against VERIFIED_FIGURES.

Original seed estimates (§C's `_Estimator`) were calibrated blind at $0.30 /
$0.20 and turned out 2.4–3x too high against these five measured rounds; the config
comment in `config.yaml`'s `refinement:` block records the recalibration
(`seed_critique_usd: 0.15`, `seed_revision_usd: 0.12`). Not yet exercised by a live
run: the un-agreed path (a session ending on the round/budget ceiling with
objections still standing) — every run so far has reached AGREE within the default
ceiling.
