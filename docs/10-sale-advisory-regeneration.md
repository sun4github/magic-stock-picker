# Standalone Sale Advisory Walkthrough

Primary file: [`src/sale_advisory.py`](../src/sale_advisory.py). Shares its generator
with [`src/refine.py`](../src/refine.py) (`run_sale_advisor`, `:347`).

`python sale_advisory.py TICKER [--run RUN_ID]` derives a fresh Phase C sale advisory
from **any stored report** and attaches it to that report's run. It is the repair
tool for the gap between "a report exists" and "that report has a current advisory."

## Why it exists

The pipeline writes an advisory once, as the last step of the run that produced the
report. That is the right default and the wrong *only* option — a report can outlive
its advisory in four ordinary ways:

| Situation | What you had before |
| :--- | :--- |
| A critic refinement revised the report but the ceiling could not cover re-deriving the advisory (`refine.py`'s `carried_stale` branch) | A stale advisory with a warning label, and no way to repair it |
| `--skip-sale-advisor` was used | No `SALE_CASE` at all, so `--sell-check` has nothing to test |
| The sale advisor produced no output (`analyze_ticker` logs a warning and continues) | A silent hole |
| The advisory is simply old — its thresholds are anchored to figures from the day it was written | Re-run the whole pipeline |

Every one of those previously required re-running the **entire** pipeline (~$0.40,
and it rewrites the report you already reviewed) to recover one artefact. This costs
~$0.08–0.10 and touches nothing else.

### Why re-running `refine.py` does not fix a stale advisory

Worth stating explicitly, because it is the intuitive thing to try and it silently
makes matters worse. Re-running the critic loop against an already-corrected report
means the critic agrees immediately with nothing to change, so **no revision runs** —
and `_refresh_sale_advisory`'s rule for that case is "no revision → carry the
existing advisory forward unchanged." The stale advisory gets carried forward again,
this time **stripped of its staleness warning** (the note-stacking guard removes the
old note and the `carried` branch writes the confident one). A stale advisory that
now looks fresh is worse than a stale advisory that says so.

## Control flow, `sale_advisory.py:67` (`generate_sale_advisory`)

| Step | Line | What happens |
| :--- | ---: | :--- |
| 1 | `:77` | Load the report via `db_get_final_report(ticker, run_id)`. Works against a pipeline run or a refinement run alike — both write to `final_reports` the same way. Errors out if the ticker has no report, since an advisory is *derived from* one. |
| — | `:100` | Log whether the run already has an advisory. Regenerating over a good one is legitimate (thresholds age), but it should never be a surprise, and this is what tells you whether the run is a repair or a refresh. |
| 2 | `:114` | Rolling daily ceiling check. **No per-invocation ceiling** — this is one deliberate call for a known artefact, so a second number to tune would be ceremony. `--no-budget` overrides. |
| 3 | `:130` | Inputs, all deterministic and token-free: `strip_generated_sections` for the analyst's prose, `_load_candidate` + `_format_verified_figures` for **current** figures, `fmp_quarterly_trends`. |
| 4 | `:142` | Its own `pipeline_runs` row for cost — see below. |
| — | `:150` | `refine.run_sale_advisor(...)` — the shared generator. |
| 5 | `:166` | The same reconciliation gate the pipeline runs over an advisory: its thresholds must not contradict the figures they are anchored to. |
| — | `:184` | Store on the **target** run, write `reports/{TICKER}_Sale_Advisory.md`, `_finalize_run` the cost row. |

## The split: artefact on the target run, cost on its own

This is the one non-obvious design decision, and it is deliberate in both halves.

**The advisory is stored under the `run_id` you named**, not under a run of its own.
That is the entire point — an advisory belongs to the report it was derived from, and
storing it anywhere else would mean `db_get_sale_case(ticker, run_id)` still could not
find it and the webapp still could not show it on that run's Sale Advisory tab.

**The cost gets a separate `pipeline_runs` row.** Adding it to the target run's totals
would rewrite the record of what that run cost, which is the one thing run history is
for — and the target run may have been finalized days earlier. A separate row keeps
the spend visible to `db_spend_since` (and therefore to the rolling daily guard)
without falsifying history. That row deliberately carries:

- **no `ticker_runs` row**, so it never appears as a browsable run in the web UI —
  there is no report to browse, only a cost;
- **no `refines_run_id`**, so it is never mistaken for a critic refinement. Setting it
  would give the run a "✓ critic agreed" chip it has not earned.

## More than one `SALE_CASE` per run is now normal

A run whose advisory has been regenerated holds two `SALE_CASE` rows: the one it
originally produced and the newer one. Every reader takes the newest —
`db_get_agent_output` and `db_get_sale_case` both `ORDER BY created_at DESC LIMIT 1`.

The web viewer needed a fix for this. `_fetch_reports` built its `parts` dict from an
**unordered** query, so with two rows of the same type the winner was whatever order
the planner happened to return. It now orders oldest-first
([`webapp/app.py`](../webapp/app.py), the `_CASE_TYPES` query), which makes the dict
comprehension keep the newest — the same rule the `db_*` readers follow. Verified by
inserting a deliberately older decoy row and confirming both the Sale tab and the
`.md` download ignored it.

## The provenance note

Every advisory this command writes is prefixed by `_origin_note` (`:202`), naming the
report's run, the generating run, and — the part that matters — that its thresholds
are **anchored to the figures current when it was generated, not to the figures the
report was written against**. Without that, an advisory generated days after its
report is indistinguishable from one written alongside it, and the difference is
exactly what a reader needs to judge how much to trust a numeric trigger.

## Where to look next

- The critic loop whose `carried_stale` branch this repairs, and the reservation that
  makes that branch rare: [09-critic-and-refinement-loop.md](09-critic-and-refinement-loop.md).
- Phase C itself — what a sale advisory is and the rules it is written under:
  [02-agents-and-reasoning-graph.md](02-agents-and-reasoning-graph.md).
- `--sell-check`, the consumer of what this writes:
  [01-orchestration-and-cli.md](01-orchestration-and-cli.md).
