# 00. Module map — where the code lives

Start here if you are looking for a file. The other docs explain what the code
*does*; this one says where it is and why the boundaries fall where they do.

`src/` is the import root. There is no `pyproject.toml` and no installed package:
you `cd src` and run `python main.py`, which is what keeps the Raspberry Pi
deployment (a file copy — see `agent_architecture.md`) and every command in the root
README working unchanged.

## The two files everything imports

`main.py` and `mcp_server.py` are the names in every import statement in this
codebase and in the MCP client configuration. Both were once enormous — main.py
3,090 lines, mcp_server.py 2,458 — and both are now mostly re-exports over
packages that hold the actual code.

That re-export layer is load-bearing, not transitional. `refine`, `buy_case`,
`buy_case_agent`, `sale_advisory`, `critic_agent`, the cron scripts and the tests
all reach into `main` by attribute — `main._price_snapshot`, `main._new_usage` —
and `mcp_server` is what `python mcp_server.py` runs to serve all 33 MCP tools.
`tools/check_public_surface.py` resolves every one of those references statically;
run it after moving anything.

## Layout

| Path | Lines | What it owns |
| :--- | ---: | :--- |
| `main.py` | 838 | The CLI, and the workflows that decide WHICH tickers to run |
| `mcp_server.py` | 72 | Facade over `data/` + `persistence/`; run it to serve MCP |
| `mcp_app.py` | 22 | The FastMCP instance and the `mcp_server` logger |
| `settings.py` | 106 | Everything read from `specs/config.yaml`, and the constants tuned beside it |
| `usage.py` | 307 | What a run costs, and the ceiling on what it may cost |
| `runstamp.py` | 16 | One per-process timestamp, shared by the log filename and session ids |
| `prompt_loader.py` | 154 | Composing agent instructions from `prompts/*.md` |
| `agents/` | 325 | The five `LlmAgent`s, the fragments they share, and the Runner |
| `pipeline/` | 879 | The reuse fingerprint, the agent loop, verdict parsing, `analyze_ticker` |
| `report/` | 1,376 | Every deterministic report section, and the reconciliation gate |
| `data/` | 1,216 | Everything read from the outside world — screen, SEC, FMP, web search |
| `persistence/` | 1,596 | Everything written down and read back — runs, reports, critic memory, ledger |
| `prompts/` | — | One `.md` per agent, plus the shared mandates |
| `magic_formula_starter_screener.py` | 1,302 | Phase A: the screen itself (see **04**) |
| `refine.py`, `critic_agent.py` | 1,557 | Phase D, the critic loop (see **09**) |
| `buy_case.py`, `buy_case_agent.py` | 678 | Phase E, the buy case (see **11**) |
| `sale_advisory.py` | 251 | Phase C repair tool (see **10**) |

### `agents/`
- `definitions.py` — what each agent is TOLD: which prompt, which tools, which
  session key it writes, how much transcript it is sent.
- `runtime.py` — how a turn is EXECUTED: the Runner, context caching, rate-limit
  detection. The caching commentary records an optimisation that was implemented,
  measured, and found to make things worse.

### `pipeline/`
- `reuse.py` — the fingerprint that decides whether a run can be skipped.
- `execution.py` — driving the agents over one shared ADK session.
- `verdict.py` — reading a finished report back into data, and stamping it.
- `analysis.py` — `analyze_ticker`: one ticker, end to end. **This is the billed
  path.**

### `report/`
- `figures.py` — normalising a candidate row into displayable figures.
- `price.py` — the live quote and the `## Price` block.
- `sections.py` — the reader-facing sections and the agent-facing figure block.
- `reconciliation.py` — the gate that checks agent prose against those figures.

Nothing in `report/` imports `main`, which is the point: report text is built from
a candidate dict and a price dict and nothing else, so it can be exercised without
standing up a pipeline run — as `tools/snapshot_report_surface.py` does.

### `data/` and `persistence/`
Split by what they talk to. `data/` is `screener.py`, `sec.py`, `fundamentals.py`,
`news.py`, `forward.py`; `persistence/` is `db.py`, `embeddings.py`, `runs.py`,
`reports.py`, `critic.py`, `ledger.py`, `results.py`.

The split does real work: importing `persistence` alone takes 0.9s against
`mcp_server`'s 2.4s, so a cron job that only writes rows no longer pays for
edgartools and pandas. The schema bootstrap is lazy — it fires on the first
connection that needs it, not at import.

## Two invariants any change has to preserve

1. **`main._prompt_version()` must stay `68b74f9c3e9a`** unless you meant to change
   a prompt. It hashes the assembled agent instructions into every stored report's
   reuse fingerprint, so drifting it invalidates the cache and makes the next
   `--from-csv` re-analyze all 30 tickers — about $11. Anything that reads a prompt
   file by path feeds this, which is why `pipeline/reuse.py` names `src/`
   explicitly instead of using `os.path.dirname(__file__)`.
2. **All 7 test files must exit 0**: `python tools/run_tests.py <out>`.

## Verifying a move

Four instruments, all in `tools/`. The first two produce a text file you diff
before and after; an empty diff is the evidence.

| Tool | Answers |
| :--- | :--- |
| `snapshot_report_surface.py` | Does every report function emit the same bytes? Includes `_prompt_version`. |
| `snapshot_tool_surface.py` | Do all 33 MCP registrations and every exported signature still match? |
| `check_public_surface.py` | Does every `main.X` / `mcp_server.X` reference still resolve? |
| `run_tests.py` | Do the 7 test files still pass? |

Report output is stored in the database, injected into agent prompts, and hashed
into the reuse fingerprint, so "it looks the same" is not good enough — the bytes
are the contract. Baseline the "before" from a `git worktree add <tmp> HEAD`
checkout rather than from memory of what it said.

### One hazard worth knowing about

`from settings import BUDGET_ENABLED` copies the value. If something later assigns
`settings.BUDGET_ENABLED = False`, the copy does not change, and code reading the
copy carries on as though nothing happened. This has bitten twice — once on the
budget guard, once on the reuse fingerprint — and both times a test caught it,
because both tests work by assigning to exactly such a value.

The rule that came out of it: anything reassigned at runtime is read live from its
own module (`settings.BUDGET_ENABLED`, `pipeline.reuse._DOWNSTREAM_PROMPTS`) and is
**not** re-exported from `main`. Plain constants nobody reassigns are re-exported
freely.
