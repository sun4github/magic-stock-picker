# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-agent stock research pipeline built on **Google ADK + Gemini**. Phase A is a deterministic screener (Greenblatt's Magic Formula plus Lynch's PEG ratio, data from FMP). The LLM agents then argue each candidate: bear, then bull, then a neutral analyst who issues a verdict, then a sale advisor. Results persist to **PostgreSQL + pgvector**. The project is a research tool that produces documents: it places no trades and tracks no positions. Positions live in the separate `magic-stock-tracker` repo, which shares the database.

Phase letters: A = screener, B = bear/bull/analyst, C = sale advisor, E = buy case (inside the pipeline, `Watch` verdicts only), D = the opt-in critic loop (`refine.py`, never part of a pipeline run). The letters follow the order the phases were added, not the order they run.

The project is spec-driven. `src/specs/agent_architecture.md`, `src/specs/workflow.feature` and `src/specs/config.yaml` are the source of truth. When the code diverges from the spec, update the spec explicitly. `docs/` is the developer walkthrough; start with `docs/architecture.md` and `docs/00-module-map.md`.

## Commands

`src/` is the import root. There is no `pyproject.toml` and no installed package. Run pipeline commands from `src/`:

```bash
cd src
python main.py --screen-only          # Phase A only: free, refreshes rankings CSV
python main.py --from-csv --top-n 5   # Phases B/C/E on the cached CSV (BILLED, ~$0.37/ticker)
python main.py BKNG                   # on-demand single ticker (BILLED)
python main.py --sell-check TICKER [--run RUN_ID]
python main.py --buy-check TICKER [--run RUN_ID]
python refine.py TICKER               # Phase D critic loop (BILLED, several times the report cost)
python sale_advisory.py TICKER        # regenerate Phase C for a stored report
python buy_case.py TICKER             # regenerate Phase E (refuses on any verdict except Watch)
python cron/run_buy_check_sweep.py --dry-run   # preview the weekly sweep, no model calls
```

Never start a billed command unless the user asks for one. Use `--screen-only` or `--dry-run` to check behaviour for free.

Other entry points:
- Web viewer (read-only Flask app): `cd webapp && python app.py`, on port 8000. It is also configured as `report-viewer` in `.claude/launch.json`.
- Operator MCP server: `python -m agent_mcp.server`, run from the repo root.
- Schema: `psql "$DATABASE_URL" -f src/sql-schema.sql`. Optional, because startup migrates idempotently.
- Track-record queries: `psql "$DATABASE_URL" -f src/ledger-queries.sql`.

### Tests

There is no pytest and no linter. Each test file is a standalone, offline script that exits non-zero on failure.

```bash
cd src && python test_guards.py               # run a single suite from src/
python agent_mcp/test_agent_mcp.py            # MCP-layer suite, run from the repo root
python tools/run_tests.py out.txt             # run every suite; writes exit codes + output tails
```

`tools/run_tests.py` holds the authoritative list of suites (`TESTS` and `EXTERNAL_TESTS`). Add any new test file to that list.

### Verification tools (run after moving or refactoring code)

| Tool | Checks |
| :--- | :--- |
| `python tools/check_public_surface.py` | Every `main.X` / `mcp_server.X` attribute reference still resolves |
| `python tools/snapshot_report_surface.py before.txt` → diff with `after.txt` | Report functions emit identical bytes, and `_prompt_version` is unchanged |
| `python tools/snapshot_tool_surface.py` (before/after diff) | All 34 MCP tool registrations and their exported signatures still match |
| `python tools/check_doc_refs.py` | Every `file.py:line` reference in `docs/` still lands inside the named symbol |

Take the "before" snapshot from a `git worktree add <tmp> HEAD` checkout, not from memory.

## Architecture

The layers in `src/` only import downward; nothing imports upward and there are no mutual imports. Keep it that way:

- `main.py` is the CLI only: it decides which tickers to run and in which mode. `refine.py`, `sale_advisory.py` and `buy_case.py` are standalone commands at the same level.
- `pipeline/` handles one ticker end to end. `analysis.py::analyze_ticker` is the billed path. `reuse.py` holds the fingerprint, `execution.py` runs the agents, and `verdict.py` parses verdicts.
- `agents/`: `definitions.py` sets what each `LlmAgent` is told; `runtime.py` sets how a turn executes.
- `report/` turns a candidate dict and a price dict into text, and holds the reconciliation gate. It must not import from the pipeline.
- `mcp_server.py` is a facade over `data/` (FMP, SEC, Tavily, reads from the outside world) and `persistence/` (PostgreSQL). The two stay split so cron jobs import cheaply.
- `settings.py`, `run_log.py` and `mcp_app.py` have no local dependencies.

**`main.py` and `mcp_server.py` are mostly re-export facades, and the re-exports are load-bearing.** Commands, cron scripts and tests reach in by attribute (`main._price_snapshot`). These references resolve at call time, so a dropped re-export fails mid-run, not at import. Run `check_public_surface.py` after any move. A value that gets reassigned at runtime (`settings.BUDGET_ENABLED`, `pipeline.reuse._DOWNSTREAM_PROMPTS`) must be read live from its own module and must not be re-exported. `from settings import X` copies the value, and that has caused bugs twice.

**The agent graph** is a plain Python list, not ADK's `SequentialAgent`, so retries happen per agent. All agents share one ADK session and hand off through `output_key`, with the next agent's instruction templating in `{key}`. `include_contents="none"` stops each agent from being re-sent the full transcript. Forward-looking data (consensus estimates, earnings calendar) goes only to the buy-case agent, never to the bear/bull/analyst chain. The critic can run on a different model family (`agents.critic_model` in config) so its review is decorrelated from the analyst's.

**Trust boundary:** the pipeline computes `VERIFIED_FIGURES` deterministically and injects them into prompts as figures the agents may not contradict. `report/reconciliation.py` then checks the agents' prose against those same figures and adds any mismatches to the report as a warnings table.

## Invariants that cost money if broken

1. **`_prompt_version()` must not change unless you deliberately edited a prompt.** It hashes the assembled agent instructions into the reuse fingerprint, so any drift invalidates cached analyses. Current value: `6f1006680b2c`, as of the 2026-08-15 analyst-prompt change. After a deliberate prompt edit, diff the report snapshot and confirm the hash line is the only change. Anything that reads a prompt file feeds the hash, which is why `pipeline/reuse.py` names `src/` explicitly.
2. **The reuse fingerprint** combines ticker, balance-sheet date (not today's date) and prompt version. A hit within `reuse.max_age_hours` copies the prior rows at $0.
3. **The budget guard** (`usage.py`) checks per-run and rolling ceilings between tickers, never mid-ticker.
4. **Report output is a byte-level contract.** It is stored in the DB, injected into prompts and hashed, so "looks the same" is not good enough.

## Prompts (`src/prompts/`)

There is one `.md` file per agent. `<<NAME>>` markers expand at import from shared blocks in `mandates/`. `{name}` markers are ADK session-state keys that expand at call time. Any `{word}` that is not a real session key raises `KeyError` mid-run, after money has already been spent, so never write literal braces in prompt prose. The factor vocabulary (`<<FACTOR_KEYS>>`) comes from `verdict_ledger.factor_taxonomy` in `config.yaml`.

## Verdict ledger and `source`

`verdict_log.source` takes one of three values:
- `PIPELINE`: the screen picked the name and the agents analysed it.
- `ONDEMAND`: a one-off `main.py TICKER` run, analysed but not screened.
- `MANUAL`: an external purchase, synced by `sync_external_picks.py`.

A query that grades this system must never count `MANUAL` rows, and Q1 (screen vs. approved) must exclude `ONDEMAND`. Closed positions are graded to `exit_date`/`exit_price`, not today's price. The snapshot cron (`src/cron/run_outcome_snapshot.py`) runs off-box on a Raspberry Pi using only `requirements-cron.txt`. It must never import the agent stack (google-adk, pandas, edgartools).

## Operator MCP layer (`agent_mcp/`)

This layer sits over the CLI and shells out to the same commands by path. Nothing in `src/` imports it, so renaming a CLI entry point or flag breaks an MCP tool silently. Tools that spend money require `confirm=True`, and long operations return job IDs. Don't confuse it with `src/mcp_server.py`, which is the agents' own toolbelt used inside a run.

## Config and environment

All tunable parameters are in `src/specs/config.yaml`: screener gates, PEG thresholds, budgets, pricing, reuse window and models. `max_peg` and `max_growth_rate_for_peg` together set a hard P/E ceiling (`max_peg × cap × 100`), so change them together and re-derive both with `tools/analyze_growth_persistence.py`. Required env vars, in the root `.env`: `FMP_API_KEY`, `TAVILY_API_KEY`, `DATABASE_URL`, `SEC_USER_AGENT`, `GOOGLE_API_KEY`. FMP is used through `stable/*` endpoints only; the v3/v4 endpoints are retired.

`tools/` holds offline instruments that nothing in the runtime imports. Put similar scripts there, not in `src/`.
