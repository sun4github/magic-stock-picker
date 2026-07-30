# Observability & Logging Walkthrough

There is no external APM/tracing (no OpenTelemetry, no Sentry, no structured
log shipping). Observability in this codebase is three separate mechanisms,
each covering a different slice of the system, and they do not share a
common backend:

1. **Python `logging`, per-process** — `main.py`'s own logger and
   `mcp_server.py`'s own logger, described below. This is the "what happened
   during this run" record.
2. **A persisted usage/cost ledger in Postgres** (`pipeline_runs` columns) —
   the "what did this run cost" record. See
   [05-guardrails-cost-and-reuse.md §6](05-guardrails-cost-and-reuse.md).
3. **Warnings embedded directly into the generated report** (the
   reconciliation gate's `## Data Reconciliation Warnings` section) — the
   "what should a reader of *this specific report* distrust" record. See
   [05-guardrails-cost-and-reuse.md §3](05-guardrails-cost-and-reuse.md).

There is **no per-request tracing across the FMP/SEC/Tavily/Gemini calls** —
if you need to correlate a specific outbound HTTP call to a specific log
line, the bracketed `[TICKER]` / `[TICKER agent_name]` prefixes (convention,
not structured fields — see below) are the only correlation mechanism.

## Three independent loggers, three different behaviors

| Logger | Set up in | Handlers | Where it writes |
| :--- | :--- | :--- | :--- |
| `"InvestmentAgentPipeline"` | `main.py:54-67` | Its own `StreamHandler` (console) + `FileHandler` (`logs/run_<timestamp>.log`) | Console **and** the per-run file |
| `"mcp_server"` | `mcp_server.py:24-25` | None of its own — relies on `logging.basicConfig(level=logging.INFO)`, which installs a `StreamHandler` on the **root** logger | Console only |
| Flask (`webapp/app.py`) | Not configured at all | Whatever Flask/Werkzeug's dev server does by default | Console only, request lines — no application-level `logger.*` calls exist in `app.py` |

Two consequences worth knowing before you go looking for a log line:

- **`mcp_server.py`'s own log lines (SEC/DB errors, embedding failures) never
  reach the per-run file.** `mcp_server.py` is imported by `main.py` (`main.py:22`)
  *before* `main.py` sets up its own logger (`main.py:54`), so
  `mcp_server.py`'s `logging.basicConfig()` call runs first and configures the
  **root** logger with a console-only handler. `mcp_server.py`'s own
  `logger.error(...)` calls (e.g. `mcp_server.py:775-780` on an embedding
  failure) go out through that root handler — console only, never the
  timestamped file `main.py` creates for the run. If you're debugging an
  embedding or DB-write failure after the fact from a saved log file, it
  won't be there; you need to have been watching the console, or capture
  stdout/stderr yourself when running the process.
- **`main.py`'s own log lines print to the console twice.** Its
  `"InvestmentAgentPipeline"` logger never sets `propagate = False`, and by
  the time it's created the root logger already has the `StreamHandler` from
  `mcp_server.py`'s `basicConfig()` call. So every `logger.info(...)` in
  `main.py` fires three times: once via its own `c_handler` (console), once
  via its own `f_handler` (the run's file — exactly once, correctly), and
  once more via propagation up to root's `StreamHandler` (console again).
  Harmless (the file is unaffected), but if you're wondering why the console
  shows every pipeline log line twice, this is why.

## `logs/` is relative to the current working directory, not to `main.py`

`main.py:49`, `os.makedirs("logs", exist_ok=True)`, is a **relative** path —
unlike the screener's `OUTPUT_FILENAME`
(`magic_formula_starter_screener.py:33`), which is deliberately anchored to
`os.path.dirname(__file__)` specifically to avoid this problem (see the
comment there). The README's documented workflow is `cd src` before running
(`README.md`'s "Running" section), which puts `logs/` at `src/logs/` — where
the bulk of this repo's log files actually live. But two log files exist at
the **repo root** (`logs/run_20260727_224446.log`,
`logs/run_20260727_232808.log`) — each just one line
(`"Initialized run logging to console and '...'"`) — direct evidence of a
run launched from the repo root instead of `src/`. Nothing breaks when this
happens (the config path *is* anchored to `__file__` and still resolves
correctly), but the log and `reports/` output land in a different directory
than expected, which is easy to miss.

## Log correlation convention (not structured logging)

Every log line that needs to be tied to a specific ticker, agent, or run
uses a manually-formatted bracketed prefix in the message string — there are
no `extra={}` fields, no JSON logs, no correlation IDs threaded through a
logging context:

- `[{ticker}]` — per-ticker lines, e.g. `main.py:1130` (`"[{ticker}] Reusing
  the report from run..."`).
- `[{ticker} {agent.name}]` — per-agent-per-ticker lines, e.g.
  `main.py:950` (`"[{ticker}] {agent.name} rate limited (429)..."`) and the
  per-agent cost breakdown at `main.py:963` (`_log_usage(f"{ticker}
  {agent.name}", agent_usage)`).
- `RUN {run_id} TOTAL` — the run-level cost rollup, `main.py:867`.
- `BUDGET:` — budget-guard breaches, `main.py:1039` / `_check_budget`.

If you pipe this to a log aggregator, these prefixes are what you'd parse on
— there's no other machine-readable correlation key in the log stream
itself (the `run_id` UUID *is* embedded in these strings, though, so a
`grep`/regex on it recovers everything for one run).

## What gets logged where, by category

| Concern | Logged via | Level | Also visible in |
| :--- | :--- | :--- | :--- |
| Per-agent token usage & cost | `_log_usage` (`main.py:832`) | INFO | `pipeline_runs` columns (`_finalize_run`) |
| Per-run total usage & cost | `_log_usage` + `_finalize_run` (`main.py:863`) | INFO | `pipeline_runs` row |
| Rate-limit (429) retries | `main.py:949, 1045` | WARNING | — (not persisted; only in the log) |
| Reconciliation findings (a figure contradicts the filings) | `main.py:1239-1248` | WARNING | The report itself, `## Data Reconciliation Warnings` |
| Budget ceiling breach | `main.py:617, 2039` | ERROR (halt) / WARNING (warn mode) | `pipeline_runs.status = 'BUDGET_EXCEEDED'` |
| DB write failures | `_check_db` (`main.py:1055`) | ERROR (failure) / INFO (success) | — (fails open; the run continues) |
| Embedding failures | `mcp_server.py:774-780` | ERROR (first 3 per process, then suppressed) | A zero vector silently stored in the row (unsearchable, but present) |
| Missing verified-figures columns for a ticker | `main.py:1170` | WARNING | — (reconciliation gate is silently weaker for that ticker) |
| Screener gate counts / warnings (Phase A) | **`print()`**, not `logging` | n/a | Console/stdout only — see below |

### Phase A (the screener) doesn't use `logging` at all

`magic_formula_starter_screener.py` has no `import logging` and no `logger.*`
calls anywhere — every diagnostic (universe exclusion counts, ROA/P/E gate
counts, the "fewer than 30 survived" warning, the TTM-vs-Annual basis-mix
warning, the earnings-calendar fallback notice) is a plain `print()` to
stdout. This means Phase A's diagnostics are **not** captured in
`main.py`'s timestamped `logs/run_<timestamp>.log` file at all — that file
only exists once `main.py`'s own logger is set up, and by then the
screener's `main()` (invoked via `run_magic_formula_screener` in
`mcp_server.py:39`) has already run and printed its output straight to the
console. If you need a durable record of a screening run's gate counts,
redirect stdout yourself (`python main.py --screen-only > screen.log`) —
the pipeline does not do this for you.

## Where to look next

- The cost/usage accounting these logs are built on top of:
  [05-guardrails-cost-and-reuse.md](05-guardrails-cost-and-reuse.md).
- Every gate mentioned above, consolidated with its exact trigger condition
  and failure behavior: [08-gates-and-validation-inventory.md](08-gates-and-validation-inventory.md).
