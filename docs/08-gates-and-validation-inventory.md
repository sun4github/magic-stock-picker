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

## A. CLI input validation — `main.py:2262-2372`

Runs once at startup, before any work begins. All of these `parser.error(...)`
(prints a usage message and exits non-zero) rather than proceeding with an
ambiguous combination of flags:

| Check | Line | Rejects |
| :--- | ---: | :--- |
| `--top-n` bounds | `main.py:2330-2331` | `--top-n` below 1 |
| `--run` requires `--sell-check` | `main.py:2339-2340` | `--run RUN_ID` used without `--sell-check` |
| `--screen-only` exclusivity | `main.py:2345-2357` | Combined with `--from-csv`, `--sell-check`, `--skip-sale-advisor`, `--force`, or a bare `TICKER` |
| `--sell-check` requires a ticker | `main.py:2362-2363` | `--sell-check` with no `TICKER` argument |
| `--skip-sale-advisor` + `--sell-check` | `main.py:2364-2365` | Combining them (sell-check never runs Phase C anyway) |

## B. Screener eligibility gates (Phase A) — `magic_formula_starter_screener.py`

Applied in this order, each *before* the next so a rejected company never
influences later gates or the final ranking (see
[04-screener-internals.md](04-screener-internals.md) for the full flowchart):

| Gate | Line | Rejects | On missing data |
| :--- | ---: | :--- | :--- |
| Universe exclusion (`_universe_exclusion_reason`) | `:157-181` | Excluded sector/industry, ETF/fund flag, foreign issuer (ADR) | N/A — based on fields the screener response always carries |
| ROA ≥ `min_roa` (`ratio_gate_reason`) | `:668-671` | ROA below the configured floor (default 25%) | **Kept** — a missing ratio is a provider gap, not a business failing the test |
| P/E ≥ `min_pe` (`ratio_gate_reason`) | `:673-680` | A **positive** P/E below the floor (default 5) | Loss-makers (no P/E) are exempt, not caught here — they're already gone via the negative-EBIT gate below |
| No earnings in last N days (`fetch_recent_earnings_symbols`) | `:218-314` | Anything that reported within the window | **Fails loud**: if neither the bulk calendar nor the per-symbol fallback works, returns `ok=False` and `main()` prints a warning that the filter was **not applied**, rather than silently passing everyone through |

## C. Per-company data-quality gates (inside `compute_company_metrics_detailed`) — `magic_formula_starter_screener.py:360-648`

These decide whether a single company's ratios can be computed **at all**;
each returns a structured `{"ok": False, "reason": ..., "message": ...}`
via `_skip()` (`:353-359`) rather than raising, so the screener's hot loop
can count and move on, and the single-ticker path (`compute_ticker_magic_metrics`,
`mcp_server.py:60`) can explain *why* to a report reader instead of showing
a bare "Not available":

| `reason` | Line | Trigger |
| :--- | ---: | :--- |
| `incomplete_quarters` | `:392-397` | Fewer than 4 complete quarters for a TTM EBIT sum |
| `no_income_data` / `no_ebit` | `:411-426` | No income statement, or no operating-income field, from the provider |
| `negative_ebit` | `:428-440` | EBIT ≤ 0 — Greenblatt's screen excludes unprofitable companies |
| `stale_income` / `stale_balance_sheet` (`_is_stale`) | `:443-449, 472-480` | Statement date older than `max_statement_age_days` (default ~200 days) |
| `no_balance_sheet` | `:461-469` | No balance sheet available |
| `no_capital_employed` | `:511-519` | Net working capital + net fixed assets ≤ 0 (division by zero) |
| `negative_enterprise_value` | `:523-532` | Cash exceeds market cap + debt (EV ≤ 0, makes Earnings Yield meaningless) |
| `unexpected_error` | `:642-647` | Anything else — caught so one company's bad data can't crash the whole screening run |

`ROIC_Unavailable_Reason` (`:564-568`, `negative_invested_capital` /
`not_computable`) is a *softer* degradation, not a rejection: the company
still ranks, but the goodwill-inclusive ROIC companion is reported as
unavailable (with a stated reason) rather than fabricated — see
[agent_architecture.md §2.F](../src/specs/agent_architecture.md).

## D. Post-screen integrity warnings — `magic_formula_starter_screener.py:main()`

Not gates that drop anything — warnings that the *screen itself* may be
misconfigured or the data provider may have changed behavior:

| Warning | Line | Trigger |
| :--- | ---: | :--- |
| Fewer than 30 survivors | `:867-870` | Phase B expects a top-30 list; fewer means the gates are too strict for the current universe |
| High per-symbol failure rate | `:780-786` | ≥25% of the universe skipped on fetch errors — suggests rate limiting or a plan restriction, not bad luck |
| TTM/Annual basis mix | `:798-810` | ≥5% of survivors fell back to annual EBIT — a signal FMP may have restricted quarterly access, which would otherwise silently mix ranking bases |

## E. Content-integrity gates (Phase B/C, post-generation) — `main.py`

| Gate | Line | Checks | On failure |
| :--- | ---: | :--- | :--- |
| Reconciliation gate (`_reconcile_agent_figures`) | `:1616-1697` | Agent-written debt/cap/EV figures against `VERIFIED_FIGURES`, tolerance per field | Logged as WARNING + appended to the report as `## Data Reconciliation Warnings` — never blocks persistence |
| Verified-figures completeness check | `:1167-1174` | Whether the candidate has `TotalDebt`/`Cash`/`EnterpriseValue` (older CSVs may lack these columns) | WARNING logged; reconciliation gate is silently weaker for that ticker (documented, not hidden) |
| Verdict extraction (`_extract_verdict`) | `:1078-1091` | Parses `"Verdict: Buy/Watch/Avoid"` from the analyst's `## Final Verdict` section via regex, anchored (not a naive substring search — the prose often says "buy" while weighing the bull case) | Falls back to whichever of buy/watch/avoid appears earliest in the section; defaults to `WATCH` if none found |
| Sell recommendation extraction (`_extract_recommendation`) | `:2194-2197` | Parses `"Recommendation: SELL/HOLD"` | Defaults to `"UNKNOWN"` if not found |

## F. Runtime resilience gates (fail-open or halt-and-preserve, by design) — `main.py`

| Gate | Line | Halts the run, or fails open? |
| :--- | ---: | :--- |
| Budget guard (`_check_budget`) | `:594-618` | **Halts** (on `on_exceed: halt`) — but only *between* tickers, never mid-ticker, and marks the run `BUDGET_EXCEEDED` rather than `COMPLETED` so a short run is distinguishable from a finished one. `on_exceed: warn` fails open (logs loudly, continues). |
| Per-agent 429 retry (`_is_rate_limit_error` + retry loop) | `:562, 938-962` | Retries up to `MAX_AGENT_RETRIES` (5) with exponential backoff; re-raises after exhausting retries, which aborts that ticker (returns zero usage — a known, accepted gap, see the comment at `main.py:956-960`) |
| DB write failures (`_check_db`) | `:1055-1062` | **Fails open** — logs an ERROR but the run continues; a persistence failure does not stop analysis |
| Embedding failures (`get_embedding`) | `mcp_server.py:733-781` | **Fails open** — logs an ERROR (loud, not silent — see the rule at the top of this doc) and stores a zero vector rather than blocking the write |
| Spend-lookup failure inside the budget guard | `agent_architecture.md`'s "Budget guard" §5B | Falls back to the per-run ceiling alone rather than failing open entirely — a guard that fails open silently would look protected when it isn't |

## G. Persistence-layer gates — `mcp_server.py` / `sql-schema.sql`

| Gate | Where | Behavior |
| :--- | :--- | :--- |
| Verdict vocabulary `CHECK` constraint | `sql-schema.sql:47`, mirrored in `initialize_database()` (`mcp_server.py:642, 651-655`) | DB-level: `verdict IN ('BUY','WATCH','AVOID','HOLD','SELL')`. Legacy values kept valid so historical rows never violate the constraint after the vocabulary changed. |
| Verdict normalization before insert (`db_store_final_report`) | `mcp_server.py:1007-1009` | An unrecognized verdict string is coerced to `WATCH` (the neutral default) **before** the `INSERT`, so the constraint is never what catches a bad value — the app layer already guarantees it. |
| Duplicate-run reuse fingerprint (`_analysis_key`) | `main.py:638-661` | Not exactly a rejection gate, but gates whether billed work happens at all — see [05-guardrails-cost-and-reuse.md §5](05-guardrails-cost-and-reuse.md). Returns `""` (disabling reuse) rather than guessing when the balance-sheet date is unknown. |
| `magic_rank` type coercion (`db_store_ticker_run`) | `mcp_server.py:1040-1043` | A rank arriving as a numpy int/float/NaN (from pandas, in `--from-csv` mode) is guarded with a `try/except` and coerced to a plain `int` or `None` — psycopg2 can't bind a numpy type to an `INTEGER` column directly. |

## Where to look next

- Full narrative on the screener gates: [04-screener-internals.md](04-screener-internals.md).
- Full narrative on the reconciliation/budget/reuse gates and cost
  accounting: [05-guardrails-cost-and-reuse.md](05-guardrails-cost-and-reuse.md).
- How gate outcomes surface in logs vs. the persisted DB record vs. the
  report itself: [07-observability-and-logging.md](07-observability-and-logging.md).
