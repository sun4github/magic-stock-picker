# Gates & Validation — Consolidated Inventory

"Gate" is used loosely here for any point in the code that decides whether
something proceeds, gets dropped, gets flagged, or gets silently
substituted. They're scattered across five files by necessity (each gate
lives next to the data it gates), so this doc is the single map of all of
them, grouped by what they protect against. See
[04-screener-internals.md](04-screener-internals.md) and
[05-guardrails-cost-and-reuse.md](05-guardrails-cost-and-reuse.md) for the
narrative walkthroughs this table indexes into.

## The recurring design rule: fail loud, not silent-wrong

A repeated pattern across every gate below: when a gate **cannot** do its
job (missing data, an unavailable endpoint, an unpriced model), the code
logs/prints loudly and marks the result as unknown/degraded — it never
silently substitutes a default that *looks* like a normal success. This is
called out explicitly in the source comments in several places (e.g.
`fetch_recent_earnings_symbols` returning `ok=False`,
`get_embedding` logging an ERROR before returning a zero vector) because an
earlier version of this pipeline got burned by the opposite: a silent
`except: pass` around embeddings meant every stored vector was zero for
months with no signal anywhere that semantic search was broken (see
[agent_architecture.md §5B "Embeddings were silently broken"](../src/specs/agent_architecture.md)).

## A. CLI input validation — `main.py:2494-2604`

Runs once at startup, before any work begins. All of these `parser.error(...)`
(prints a usage message and exits non-zero) rather than proceeding with an
ambiguous combination of flags:

| Check | Line | Rejects |
| :--- | ---: | :--- |
| `--top-n` bounds | `main.py:2562-2563` | `--top-n` below 1 |
| `--run` requires a check mode | `main.py:2571-2572` | `--run RUN_ID` used without `--sell-check` or `--buy-check` |
| The two checks are mutually exclusive | after the `--run` check | `--sell-check` together with `--buy-check` — they test different stored conditions for opposite readers and each writes its own report, so one invocation would be ambiguous about which answer it reported |
| `--screen-only` exclusivity | `main.py:2577-2589` | Combined with `--from-csv`, `--sell-check`, `--buy-check`, `--skip-sale-advisor`, `--skip-buy-case`, `--force`, or a bare `TICKER` |
| `--sell-check` requires a ticker | `main.py:2594-2595` | `--sell-check` with no `TICKER` argument |
| `--buy-check` requires a ticker | dispatch chain | `--buy-check` with no `TICKER` argument |
| `--skip-sale-advisor` + `--sell-check` | `main.py:2596-2597` | Combining them (sell-check never runs Phase C anyway) |
| `--skip-buy-case` + `--buy-check` | dispatch chain | Combining them (buy-check never runs Phase E anyway) |

## B. Screener eligibility gates (Phase A) — `magic_formula_starter_screener.py`

Applied in this order, each *before* the next so a rejected company never
influences later gates or the final ranking (see
[04-screener-internals.md](04-screener-internals.md) for the full flowchart):

| Gate | Line | Rejects | On missing data |
| :--- | ---: | :--- | :--- |
| Universe exclusion (`_universe_exclusion_reason`) | `:192-216` | Excluded sector/industry, ETF/fund flag, foreign issuer (ADR) | N/A — based on fields the screener response always carries |
| ROA ≥ `min_roa` (`ratio_gate_reason`) | `:703-706` | ROA below the configured floor (default 25%) | **Kept** — a missing ratio is a provider gap, not a business failing the test |
| P/E ≥ `min_pe` (`ratio_gate_reason`) | `:708-715` | A **positive** P/E below the floor (default 5) | Loss-makers (no P/E) are exempt, not caught here — they're already gone via the negative-EBIT gate below |
| EPS growth > `min_eps_growth`, base margin ≥ `min_base_net_margin`, PEG ≤ `max_peg` (`growth_gate_reason`) | `:895-935` | Flat or shrinking earnings per share (measured as multi-year TOTALS, so loss years count), a base window below 3% net margin, or a PEG above 1.5. Lynch's test, not Greenblatt's; every threshold set from a 189-company study — [spec §10](../src/specs/agent_architecture.md) | **Dropped, and this is the one exception to the rule in the row above.** `1/PEG` is a ranking input, so a survivor without one cannot be ranked, and the gate asks a company to *demonstrate* growth. Each cause keeps a distinct reason (`growth_unavailable`, `peg_unavailable`, narrowed further by `EPSGrowth_Unavailable_Reason` inside `fetch_eps_growth`) and `main()` prints the split, so a provider outage is a visible count rather than a quietly shorter list |
| No earnings in last N days (`fetch_recent_earnings_symbols`) | `:253-349` | Anything that reported within the window | **Fails loud**: if neither the bulk calendar nor the per-symbol fallback works, returns `ok=False` and `main()` prints a warning that the filter was **not applied**, rather than silently passing everyone through |

## C. Per-company data-quality gates (inside `compute_company_metrics_detailed`) — `magic_formula_starter_screener.py:395-683`

These decide whether a single company's ratios can be computed **at all**;
each returns a structured `{"ok": False, "reason": ..., "message": ...}`
via `_skip()` (`:388-394`) rather than raising, so the screener's hot loop
can count and move on, and the single-ticker path (`compute_ticker_magic_metrics`,
`mcp_server.py:61`) can explain *why* to a report reader instead of showing
a bare "Not available":

| `reason` | Line | Trigger |
| :--- | ---: | :--- |
| `incomplete_quarters` | `:427-432` | Fewer than 4 complete quarters for a TTM EBIT sum |
| `no_income_data` / `no_ebit` | `:446-461` | No income statement, or no operating-income field, from the provider |
| `negative_ebit` | `:463-475` | EBIT ≤ 0 — Greenblatt's screen excludes unprofitable companies |
| `stale_income` / `stale_balance_sheet` (`_is_stale`) | `:478-484, 507-515` | Statement date older than `max_statement_age_days` (default ~200 days) |
| `no_balance_sheet` | `:496-504` | No balance sheet available |
| `no_capital_employed` | `:546-554` | Net working capital + net fixed assets ≤ 0 (division by zero) |
| `negative_enterprise_value` | `:558-567` | Cash exceeds market cap + debt (EV ≤ 0, makes Earnings Yield meaningless) |
| `unexpected_error` | `:677-682` | Anything else — caught so one company's bad data can't crash the whole screening run |

`ROIC_Unavailable_Reason` (`:599-603`, `negative_invested_capital` /
`not_computable`) is a *softer* degradation, not a rejection: the company
still ranks, but the goodwill-inclusive ROIC companion is reported as
unavailable (with a stated reason) rather than fabricated — see
[agent_architecture.md §2.F](../src/specs/agent_architecture.md).

## D. Post-screen integrity warnings — `magic_formula_starter_screener.py:main()`

Not gates that drop anything — warnings that the *screen itself* may be
misconfigured or the data provider may have changed behavior:

| Warning | Line | Trigger |
| :--- | ---: | :--- |
| Fewer than 30 survivors | `:1156-1162` | Phase B expects a top-30 list; fewer means the gates are too strict for the current universe |
| High per-symbol failure rate | `:1036-1042` | ≥25% of the universe skipped on fetch errors — suggests rate limiting or a plan restriction, not bad luck |
| TTM/Annual basis mix | `:1054-1066` | ≥5% of survivors fell back to annual EBIT — a signal FMP may have restricted quarterly access, which would otherwise silently mix ranking bases |

## E. Content-integrity gates (Phase B/C, post-generation) — `main.py`

| Gate | Line | Checks | On failure |
| :--- | ---: | :--- | :--- |
| Reconciliation gate (`_reconcile_agent_figures`) | `:1704-1785` | Agent-written debt/cap/EV figures against `VERIFIED_FIGURES`, tolerance per field (debt 10%, EV 20%, market cap 25%) | Logged as WARNING + appended to the report as `## Data Reconciliation Warnings` — never blocks persistence |
| Finding provenance (`_finding_basis`) | with the gate | Whether the verified figure came from a **filing** (debt, cash — fixed until the next one) or from the **market price** (market cap, EV — moves every session) | Not a gate but a qualifier on one: it decides the wording of the warning and adds a `Basis` column to the report table, so a market figure that has merely moved is not read as an error. Matters wherever a report is re-checked later than it was written |
| Price snapshot (`_price_snapshot` / `_format_price_section`) | with the gathering step | Whether a live quote could be fetched | **Fails open, visibly**: the report's `## Price` section says the price could not be retrieved and that the filing-derived ratios are unaffected — never a blank, a zero, or a guess |
| Verified-figures completeness check | `:1181-1188` | Whether the candidate has `TotalDebt`/`Cash`/`EnterpriseValue` (older CSVs may lack these columns) | WARNING logged; reconciliation gate is silently weaker for that ticker (documented, not hidden) |
| Verdict extraction (`_extract_verdict`) | `:1092-1105` | Parses `"Verdict: Buy/Watch/Avoid"` from the analyst's `## Final Verdict` section via regex, anchored (not a naive substring search — the prose often says "buy" while weighing the bull case) | Falls back to whichever of buy/watch/avoid appears earliest in the section; defaults to `WATCH` if none found |
| Sell recommendation extraction (`_extract_recommendation`) | `:2426-2429` | Parses `"Recommendation: SELL/HOLD"` | Defaults to `"UNKNOWN"` if not found |
| Buy recommendation extraction (`buy_case_agent.extract_buy_recommendation`) | `buy_case_agent.py` | Parses `"Recommendation: BUY/WAIT"`, anchored for the same reason — a buy-check's justification paragraph routinely says "buy" while explaining why not to | Defaults to `"UNKNOWN"` if not found |
| **The Watch gate** (`buy_case_agent.is_watch`) | `buy_case_agent.py` | Whether a verdict earns a buy case. Used by the pipeline, the critic loop and the standalone command, so the three cannot disagree | Fails **closed**: anything unrecognisable is treated as not-a-Watch. A missing buy case is a gap `buy_case.py` repairs; a buy case on an `Avoid` is a defect already published |

## F. Runtime resilience gates (fail-open or halt-and-preserve, by design) — `main.py`

| Gate | Line | Halts the run, or fails open? |
| :--- | ---: | :--- |
| Budget guard (`_check_budget`) | `:608-632` | **Halts** (on `on_exceed: halt`) — but only *between* tickers, never mid-ticker, and marks the run `BUDGET_EXCEEDED` rather than `COMPLETED` so a short run is distinguishable from a finished one. `on_exceed: warn` fails open (logs loudly, continues). |
| Per-agent 429 retry (`_is_rate_limit_error` + retry loop) | `:576, 952-976` | Retries up to `MAX_AGENT_RETRIES` (5) with exponential backoff; re-raises after exhausting retries, which aborts that ticker (returns zero usage — a known, accepted gap, see the comment at `main.py:970-974`) |
| DB write failures (`_check_db`) | `:1069-1076` | **Fails open** — logs an ERROR but the run continues; a persistence failure does not stop analysis |
| Embedding failures (`get_embedding`) | `mcp_server.py:764-812` | **Fails open** — logs an ERROR (loud, not silent — see the rule at the top of this doc) and stores a zero vector rather than blocking the write |
| Spend-lookup failure inside the budget guard | `agent_architecture.md`'s "Budget guard" §5B | Falls back to the per-run ceiling alone rather than failing open entirely — a guard that fails open silently would look protected when it isn't |

## G. Critic-loop gates (Phase D, opt-in) — `critic_agent.py` / `refine.py`

Only active under `python refine.py TICKER`. Full walkthrough:
[09-critic-and-refinement-loop.md](09-critic-and-refinement-loop.md).

| Gate | Line | Checks | On failure |
| :--- | ---: | :--- | :--- |
| Agreement cross-check (`extract_critic_verdict`) | `critic_agent.py:269-297` | The critic's *declared* `CRITIC VERDICT:` line against the severities it assigned itself. Takes the **stricter** of the two. | A declared `AGREE` with a BLOCKING/MATERIAL finding standing is forced to `REVISE`, and the override is returned as a `note` and logged — resolving the contradiction the other way would stamp "the critic agreed" on a report the critic's own text says is unsupportable. |
| Missing verdict line | `critic_agent.py:283-287` | Whether any `CRITIC VERDICT:` line parsed at all | **Falls back to the severities**, never to agreement — a formatting failure is not evidence of a clean report. |
| Findings-parse fallback (`parse_findings`) | `critic_agent.py:222-238` | Whether the review's findings could be parsed at the expected heading/bold structure | If nothing parses *and* the text doesn't say "No findings.", any severity keyword in the body promotes it to a single `unparsed` finding. Silently returning `[]` would read as "the critic found nothing". |
| Spend ceiling (`_affordable`) | `refine.py:217-230` | `spent + next-round estimate` against **both** `refinement.max_budget_usd` and the rolling `budget.per_day_usd` window | Returns a reason string; the loop stops **between** rounds and the report ships stamped un-agreed with that reason. Never aborts mid-round (an abandoned round is billed and produces nothing). |
| Revision-affordability rule (`_Estimator.full_round`) | `refine.py:205, 661` | Whether a revision, **the critique that must follow it**, and **the sale advisory it invalidates** all fit | Stops before the revision. Guarantees two things: the shipped report has always been critiqued *as it stands*, and a revision never strands the advisory it made stale. Reserved rather than given a separate budget, so `--max-budget` remains the one number that governs. |
| Zero-cost measurement guard (`_Estimator.observe`) | `refine.py:199-202` | Ignores a measured cost of `0` | A turn that failed before billing must not be recorded as a cheap round, or the next projection lets the loop start something it cannot finish. |
| Reconciliation gate on the refined report | `refine.py:739-751` | Same `_reconcile_agent_figures` as §E, re-run over the revised text, the critic's own review, **and** the sale advisory | Same behavior as §E — warnings appended, never blocks persistence. |
| Sale-advisory freshness (`_refresh_sale_advisory`) | `refine.py:330-427` | Whether the report was actually revised, and whether re-deriving the advisory fits the ceiling | No revision → carried forward unchanged (valid, since the prose did not move). Revision → re-derived against the refined report. Revision but unaffordable → **should be unreachable** (the cost is reserved before the revision is committed to); if reached, carried with a visible staleness warning and a loud log, never silently, because its sell triggers may be anchored to a figure the critic corrected. |
| Source-case availability (`_load_source_case`) | `refine.py:163-184` | Whether the reviewed run still has `BEAR_CASE`/`BULL_CASE` stored | **Fails open with an explicit prompt note** telling the critic the case is unavailable and not to read its absence as evidence the summary is wrong. |

## G1b. Stale-figure guards in the critic loop — `critic_agent.py` / `refine.py`

A review runs LATER than the report it reviews, against figures recomputed today.
These three stop that gap producing a false finding — a correct-at-the-time price
being "corrected" to today's. Prompt rules rather than code, so what is enforced in
code is that the rules are present and that both agents get the vintage they need.

| Gate | Where | Checks | On failure |
| :--- | :--- | :--- | :--- |
| `report_vintage` seeded | `refine.run_refinement_loop` | Both agents are told when the report was written and that the figures are from today | ADK fails the run at template time if the key is missing — the instruction references it, so it cannot silently go unseeded |
| Moved ≠ wrong (critic) | `critic_agent.py` agent block | A market-derived figure that has only moved must not be raised as a factual error; a filing-derived mismatch still must | Prompt rule. If the valuation moved enough to matter, it is to be raised as a finding about the reasoning instead |
| Do not re-price (reviser) | `critic_agent.py` agent block | The reviser leaves prices in the prose as they were | Prompt rule. The pipeline re-stamps a fresh timestamped price section around the revised text |

Regression coverage: `_staleness_cases` in [`src/test_critic.py`](../src/test_critic.py).

## G2. Standalone sale-advisory gates — `sale_advisory.py`

Only active under `python sale_advisory.py TICKER`. Walkthrough:
[10-sale-advisory-regeneration.md](10-sale-advisory-regeneration.md).

| Gate | Line | Checks | On failure |
| :--- | ---: | :--- | :--- |
| Report exists | `sale_advisory.py:83-90` | Whether the named (or latest) run has a final report | Errors out with the command to run first. An advisory is *derived from* a report; there is nothing to derive from. |
| Report has analyst prose | `:133-137` | Whether `strip_generated_sections` left anything | Errors out rather than sending an empty report to the advisor and paying for the answer. |
| Rolling daily ceiling | `:114-127` | `prior day spend + ~$0.12` against `budget.per_day_usd` | Refuses to start, naming the figures. `--no-budget` overrides. No per-invocation ceiling by design — one deliberate call for a known artefact. |
| Empty advisor output | `:159-164` | Whether the advisor returned anything | Nothing stored; the run is finalized `FAILED` **and the attempt's cost is still reported**, because it was still billed. |
| Reconciliation gate | `:166-176` | The new advisory's thresholds against `VERIFIED_FIGURES` | Same as §E — warnings logged, never blocks persistence. |

## G3. Phase E gates (the buy case) — `buy_case_agent.py` / `buy_case.py`

Walkthrough: [11-buy-case-and-buy-check.md](11-buy-case-and-buy-check.md).

| Gate | Where | Checks | On failure |
| :--- | :--- | :--- | :--- |
| Watch-only gate | `analyze_ticker`, `refine._refresh_buy_case`, `buy_case.generate_buy_case` | `is_watch(verdict)` | No buy case is written, and the reason is logged at INFO — this is a decision, not a failure. `buy_case.py` errors out naming the verdict and pointing at `refine.py`, with **no override flag** |
| Report exists / has analyst prose | `buy_case.py` | Same two checks `sale_advisory.py` makes | Errors out rather than paying to summarise nothing |
| Rolling daily ceiling | `buy_case.py` | `prior day spend + ~$0.18` against `budget.per_day_usd` | Refuses to start, naming the figures. `--no-budget` overrides |
| Price availability | `price_data_block` | Whether a live quote came back | **Fails open, loudly**: the block tells the agent to express the price trigger against a valuation multiple instead and to quote no price as current. The event triggers still stand |
| Empty advisor output | `write_buy_case` | Whether the agent returned anything | Nothing stored; the cost of the attempt is still reported, because it was still billed |
| Reconciliation gate | `write_buy_case` | The buy case's thresholds against `VERIFIED_FIGURES` | Warnings logged **and appended to the document itself** — a log line is seen once by whoever ran the pipeline, the document is read later by whoever is deciding |
| Buy-case reservation | `refine._Estimator.full_round` | That a revision's committed cost includes the buy case | Unconditional, because whether it will be needed depends on a verdict that does not exist yet. `refinement.max_budget_usd` was raised 2.25 → 2.45 to absorb it |
| Unaffordable regeneration | `refine._refresh_buy_case` | `_affordable(...)` before re-deriving | Carries the previous buy case with a visible staleness label, or — with nothing to carry — ends honestly with none and names the repair command |

## H. Persistence-layer gates — `mcp_server.py` / `sql-schema.sql`

| Gate | Where | Behavior |
| :--- | :--- | :--- |
| Verdict vocabulary `CHECK` constraint | `sql-schema.sql:47`, mirrored in `initialize_database()` (`mcp_server.py:673, 682-686`) | DB-level: `verdict IN ('BUY','WATCH','AVOID','HOLD','SELL')`. Legacy values kept valid so historical rows never violate the constraint after the vocabulary changed. |
| Verdict normalization before insert (`db_store_final_report`) | `mcp_server.py:1038-1040` | An unrecognized verdict string is coerced to `WATCH` (the neutral default) **before** the `INSERT`, so the constraint is never what catches a bad value — the app layer already guarantees it. |
| Duplicate-run reuse fingerprint (`_analysis_key`) | `main.py:652-675` | Not exactly a rejection gate, but gates whether billed work happens at all — see [05-guardrails-cost-and-reuse.md §5](05-guardrails-cost-and-reuse.md). Returns `""` (disabling reuse) rather than guessing when the balance-sheet date is unknown. |
| `magic_rank` type coercion (`db_store_ticker_run`) | `mcp_server.py:1071-1074` | A rank arriving as a numpy int/float/NaN (from pandas, in `--from-csv` mode) is guarded with a `try/except` and coerced to a plain `int` or `None` — psycopg2 can't bind a numpy type to an `INTEGER` column directly. |

## Where to look next

- Full narrative on the screener gates: [04-screener-internals.md](04-screener-internals.md).
- Full narrative on the reconciliation/budget/reuse gates and cost
  accounting: [05-guardrails-cost-and-reuse.md](05-guardrails-cost-and-reuse.md).
- How gate outcomes surface in logs vs. the persisted DB record vs. the
  report itself: [07-observability-and-logging.md](07-observability-and-logging.md).
