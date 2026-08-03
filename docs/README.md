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
| [01-orchestration-and-cli.md](01-orchestration-and-cli.md) | CLI entry point, the five execution modes, and the per-ticker control flow that ties everything together | `src/main.py` |
| [02-agents-and-reasoning-graph.md](02-agents-and-reasoning-graph.md) | The `LlmAgent` definitions, their prompts, and how they hand off state | `src/main.py`, `src/research-instructions.md`, `src/bullish-research-instructions.md`, `src/sale-advisor-instructions.md` |
| [03-mcp-tools-and-persistence.md](03-mcp-tools-and-persistence.md) | Every MCP tool (data fetch + database) the agents and orchestrator call | `src/mcp_server.py` |
| [04-screener-internals.md](04-screener-internals.md) | Phase A: universe fetch, Greenblatt's eligibility gates, ranking, CSV output | `src/magic_formula_starter_screener.py` |
| [05-guardrails-cost-and-reuse.md](05-guardrails-cost-and-reuse.md) | Verified figures, the reconciliation gate, budget ceilings, duplicate-run reuse, and cost accounting | `src/main.py` |
| [06-webapp.md](06-webapp.md) | The read-only Flask report viewer | `webapp/app.py`, `webapp/templates/index.html` |
| [07-observability-and-logging.md](07-observability-and-logging.md) | How logging is actually wired up (three independent loggers), where each diagnostic ends up, and two verified logging quirks (duplicate console lines, CWD-relative log directory) | `src/main.py`, `src/mcp_server.py`, `src/magic_formula_starter_screener.py` |
| [08-gates-and-validation-inventory.md](08-gates-and-validation-inventory.md) | Every validation/eligibility/integrity/resilience gate in the system, consolidated into one table by category, with fail-loud-vs-fail-open behavior called out | all of the above |
| [09-critic-and-refinement-loop.md](09-critic-and-refinement-loop.md) | Phase D: the independent critic, the analyst/critic feedback loop, its spend ceiling, and the cross-run critic memory that keeps it from relitigating settled points | `src/critic_agent.py`, `src/refine.py`, `src/critic-instructions.md` |
| [10-sale-advisory-regeneration.md](10-sale-advisory-regeneration.md) | The standalone `sale_advisory.py` command: generating or repairing a Phase C sale advisory for any stored report, and why its artefact and its cost land on different runs | `src/sale_advisory.py` |
| [11-buy-case-and-buy-check.md](11-buy-case-and-buy-check.md) | Phase E: the buy case written for a `Watch` verdict (the entry price range and the triggers that would make it a Buy), the forward-looking feeds only it reads, and the `--buy-check` command that tests it later | `src/buy_case_agent.py`, `src/buy_case.py`, `src/buy-case-instructions.md`, `src/buy-check-instructions.md` |

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

## Conventions used in these docs

- `path/to/file.py:123` means "read this starting at line 123 of that file
  as of this writing." Line numbers drift as the code changes — if a
  reference looks off, search for the function/symbol name instead of
  trusting the number blindly.
- Diagrams are [Mermaid](https://mermaid.js.org/); they render directly in
  GitHub and most Markdown viewers.
- Phase letters match `agent_architecture.md`: A = screener, B = bear/bull/analyst
  reasoning, C = sale advisor, D = the critic refinement loop, E = the buy case.
  **D and E are lettered in the order they were added, not in pipeline order**: D is
  opt-in, run per ticker from its own entry point (`refine.py`), never as part of a
  pipeline run; E runs *inside* the pipeline, immediately after C, for a `Watch`
  verdict only.
