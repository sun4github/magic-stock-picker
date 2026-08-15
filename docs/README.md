# Developer Documentation

This `docs/` folder is a **code walkthrough for developers** getting familiar
with the codebase — what runs when, which file owns which behavior, and the
exact lines to read next. It complements, but does not replace, the design
spec in [`src/specs/agent_architecture.md`](../src/specs/agent_architecture.md)
(the "why," with diagrams) and the root [`README.md`](../README.md) (the
"how to run it," for end users).

That spec, together with [`src/specs/workflow.feature`](../src/specs/workflow.feature)
(behavior, in Gherkin) and [`src/specs/config.yaml`](../src/specs/config.yaml)
(tunable parameters), is a **Spec-Driven Development** artifact set: it was
authored first and used to generate the implementation, not written afterward
to describe it. Both spec files are living documents, revised as the system
changed — `agent_architecture.md` §4 is a worked example, explicitly marked as
superseded scaffolding rather than silently left to contradict the shipped code.

Read [`../README.md`](../README.md) first if you just want to run the tool.
Come here once you need to change or extend the code.

## Map

| Doc | Covers | Primary source files |
| :--- | :--- | :--- |
| [architecture.md](architecture.md) | What the system is, the shape of a run, the layer boundaries and why they fall where they do, and the two mechanisms that exist purely to bound cost | all of `src/` |
| [00-module-map.md](00-module-map.md) | Where every file lives, why the package boundaries fall where they do, and the four tools that verify a change did not move anything it should not have | all of `src/` |
| [01-orchestration-and-cli.md](01-orchestration-and-cli.md) | CLI entry point, the five execution modes, and the per-ticker control flow that ties everything together | `src/main.py`, `src/pipeline/` |
| [02-agents-and-reasoning-graph.md](02-agents-and-reasoning-graph.md) | The `LlmAgent` definitions, their prompts, and how they hand off state | `src/agents/`, `src/prompts/`, `src/prompt_loader.py` |
| [03-mcp-tools-and-persistence.md](03-mcp-tools-and-persistence.md) | Every MCP tool (data fetch + database) the agents and orchestrator call | `src/data/`, `src/persistence/`, `src/mcp_server.py` |
| [04-screener-internals.md](04-screener-internals.md) | Phase A: universe fetch, Greenblatt's eligibility gates, ranking, CSV output | `src/magic_formula_starter_screener.py` |
| [05-guardrails-cost-and-reuse.md](05-guardrails-cost-and-reuse.md) | Verified figures, the reconciliation gate, budget ceilings, duplicate-run reuse, and cost accounting | `src/report/reconciliation.py`, `src/usage.py`, `src/pipeline/reuse.py`, `src/settings.py` |
| [06-webapp.md](06-webapp.md) | The read-only Flask report viewer | `webapp/app.py`, `webapp/templates/index.html` |
| [07-observability-and-logging.md](07-observability-and-logging.md) | How logging is actually wired up (three independent loggers), where each diagnostic ends up, and two verified logging quirks (duplicate console lines, CWD-relative log directory) | `src/main.py`, `src/mcp_app.py`, `src/magic_formula_starter_screener.py` |
| [08-gates-and-validation-inventory.md](08-gates-and-validation-inventory.md) | Every validation/eligibility/integrity/resilience gate in the system, consolidated into one table by category, with fail-loud-vs-fail-open behavior called out | all of the above |
| [09-critic-and-refinement-loop.md](09-critic-and-refinement-loop.md) | Phase D: the independent critic, the analyst/critic feedback loop, its spend ceiling, and the cross-run critic memory that keeps it from relitigating settled points | `src/critic_agent.py`, `src/refine.py`, `src/prompts/critic.md` |
| [10-sale-advisory-regeneration.md](10-sale-advisory-regeneration.md) | The standalone `sale_advisory.py` command: generating or repairing a Phase C sale advisory for any stored report, and why its artefact and its cost land on different runs | `src/sale_advisory.py` |
| [11-buy-case-and-buy-check.md](11-buy-case-and-buy-check.md) | Phase E: the buy case written for a `Watch` verdict (the entry price range and the triggers that would make it a Buy), the forward-looking feeds only it reads, and the `--buy-check` command that tests it later | `src/buy_case_agent.py`, `src/buy_case.py`, `src/prompts/buy-case.md`, `src/prompts/buy-check.md` |
| [12-verdict-ledger.md](12-verdict-ledger.md) | The verdict ledger: what each verdict claimed, what the stock did next, and the off-box weekly job that grades it by arithmetic. Also the externally-sourced picks this system did not make, graded to their exit on the same arithmetic. Records only — it feeds nothing back into any prompt | `src/sql-schema.sql`, `src/persistence/ledger.py`, `src/pipeline/analysis.py`, `src/cron/run_outcome_snapshot.py`, `src/sync_external_picks.py`, `src/ledger-queries.sql` |

## What is *not* in the runtime path

[`tools/`](../tools/) holds offline instruments that nothing imports and no run
executes. Today that is `analyze_growth_persistence.py`, which re-derives the
evidence behind the PEG thresholds in `config.yaml` (see
[`agent_architecture.md`](../src/specs/agent_architecture.md) §10.I and the
"Re-checking and adjusting the growth cap" section of the root README). It computes
no PEG and screens no company; it imports the screener's growth math rather than
reimplementing it, so a tuning study can never measure something the screener does
not. Put anything similar there rather than in `src/`.

## Reading order for a first pass

0. **architecture.md** first, for what the system is and the shape of a run, then **00** for one page on where everything lives. Worth two minutes before **01**:
   `main.py` and `mcp_server.py` are now largely re-export facades over `pipeline/`,
   `report/`, `agents/`, `data/` and `persistence/`, so a doc that says "in
   `main.py`" below usually means "reachable as `main.something`" rather than
   "defined in that file."
1. **01** to see the shape of a run end to end (`python main.py` → screener →
   per-ticker analysis → reports/DB).
2. **02** to see what each agent actually reads and writes.
3. **03** and **04** for the tool/data layer each agent and the screener depend on.
4. **05** once you need to touch cost accounting, the reconciliation gate, or
   why a re-run didn't bill anything.
5. **06** only if you're working on the viewer.
6. **07** and **08** are reference material, not narrative — read them when
   you need to know where a specific diagnostic ends up, or want the full
   list of every gate in the system in one place.
7. **09** when you need the opt-in critic loop (`python refine.py TICKER`) —
   it sits entirely outside the pipeline and nothing in **01**–**08** calls it.
8. **10** for the standalone sale-advisory command
   (`python sale_advisory.py TICKER`) — a small repair tool, also outside the
   pipeline, for reports whose advisory is missing or out of date.
9. **11** for Phase E, the buy case. Unlike **09** and **10** this one *is* part of a
   pipeline run — but only for the tickers whose verdict is `Watch`, so it is easy to
   read **01** end to end without noticing it exists.
10. **12** for the verdict ledger. It runs inside every pipeline run, but purely as
    bookkeeping: it reads nothing back, changes no verdict, and its scoring job lives
    off-box on a Raspberry Pi with none of this repo's dependencies. Read it when you
    need to know how the system's own track record is being accumulated.

## Conventions used in these docs

- `path/to/file.py:123` means "read this starting at line 123 of that file."
  Paths are relative to `src/`.

  These are checked, not hoped for. `python tools/check_doc_refs.py` verifies
  every one of them: the file exists, the line is in range, and — where the
  reference sits beside a symbol name, which is the house style here — that the
  line falls inside that symbol. Run it after moving code, and re-point what it
  reports. Nothing executes a document, so that tool is what stops these decaying
  between reads.

  It cannot check a reference to a REGION rather than a symbol ("the CLI
  parsing, `main.py:678-825`"); it reports how many of those it is taking on
  trust. If a number still looks off, search for the symbol name.
- Diagrams are [Mermaid](https://mermaid.js.org/); they render directly in
  GitHub and most Markdown viewers.
- Phase letters match `agent_architecture.md`: A = screener, B = bear/bull/analyst
  reasoning, C = sale advisor, D = the critic refinement loop, E = the buy case.
  **D and E are lettered in the order they were added, not in pipeline order**: D is
  opt-in, run per ticker from its own entry point (`refine.py`), never as part of a
  pipeline run; E runs *inside* the pipeline, immediately after C, for a `Watch`
  verdict only.
