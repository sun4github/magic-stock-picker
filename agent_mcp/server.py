"""MCP server exposing the magic-stock-picker CLI as tools.

This is a *layer over* the agent, not part of it. Every operation here shells out to
the same command a person would type, from the same directory, with the same flags -
so the CLI remains the one implementation of every behaviour and keeps working
exactly as before whether this server is running or not. Nothing in `src/` imports
anything from this package, and nothing here imports the pipeline.

Not to be confused with `src/mcp_server.py`, which is a different thing at a
different layer: that one is the *agents'* toolbelt (fetch a filing, read FMP
metrics, write a row), consumed by the LLM agents inside a run. This one is the
*operator's* toolbelt - start a run, watch it, read what it produced.

Three groups of tools:

  * `start_*`  - launch a CLI operation as a background job, returning a job id.
                 Runs take minutes to an hour, which no MCP client will wait for.
  * `job_*`    - poll, tail, list and cancel those jobs.
  * read-only  - reports, rankings and configuration, answered immediately.

Plus `describe_capabilities`, which explains the whole surface to a caller that has
just connected and has no other way to learn what any of it costs.

Run it:

    python -m agent_mcp.server                      # stdio, for a local client
    python -m agent_mcp.server --transport http     # network, token required
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Absolute imports, so `python -m agent_mcp.server` and `python agent_mcp/server.py`
# both work - the second only if the repo root is on the path, which the __main__
# block below arranges.
if __package__ in (None, ""):  # pragma: no cover - direct-script invocation
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_mcp import catalog, jobs, library  # noqa: E402

logger = logging.getLogger("agent_mcp")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

INSTRUCTIONS = """\
Operator interface for magic-stock-picker: a multi-agent stock research pipeline
combining Greenblatt's Magic Formula, Lynch's PEG ratio, and an LLM chain that argues
both sides of each candidate before reaching a Buy / Watch / Avoid verdict.

Call `describe_capabilities` first. It explains every tool, what each costs, and the
workflows they compose into.

Two things to know before calling anything else:

1. Operations that spend money require `confirm=True`. Called without it they return
   a costed preview and spend nothing. A full pipeline run is $11-13; read the
   preview before confirming one.
2. `start_*` tools return a job id, not a result. Runs take 2-60 minutes. Poll with
   `job_status` and read progress with `job_log`.
"""

mcp = FastMCP("magic-stock-picker", instructions=INSTRUCTIONS)


# ==========================================================================
# Shared launch machinery
# ==========================================================================

def _preview(op: catalog.Operation, argv: list, cost_override: Optional[tuple] = None) -> dict:
    """What a billed operation returns when it was called without `confirm=True`.

    The point is that this costs nothing and reads like the receipt the caller is
    about to sign: the exact command, the money, the time, and what it will write.
    """
    entry = catalog.as_dict(op)
    if cost_override:
        entry["estimated_cost"] = catalog.format_cost(cost_override)
        entry["estimated_cost_usd"] = list(cost_override)
    entry.update(
        {
            "status": "preview",
            "spent": "nothing - this was a preview",
            "command": f"python {' '.join(argv)}",
            "run_from": str(SRC_DIR if argv and not argv[0].startswith("tools") else PROJECT_ROOT),
            "next_step": f"Call {op.tool} again with confirm=True to actually start it.",
        }
    )
    return entry


def _launch(
    op_key: str,
    argv: list,
    *,
    confirm: bool,
    requested_by: str = "",
    cost_override: Optional[tuple] = None,
    cwd: Optional[Path] = None,
    label: str = "",
) -> dict:
    """Preview, refuse, or start - the one path every `start_*` tool goes through."""
    op = catalog.BY_KEY[op_key]

    if op.bills and not confirm:
        return _preview(op, argv, cost_override)

    if op.bills:
        # Billed work is serialised. Two overlapping runs share one rolling daily
        # ceiling and one log directory, and with several agents able to call this
        # server there is nothing else stopping them from starting at once.
        busy = jobs.running_billed_job()
        if busy:
            return {
                "status": "refused",
                "reason": "Another billed operation is already running.",
                "running_job": {
                    "job_id": busy["job_id"],
                    "operation": busy["operation"],
                    "label": busy.get("label"),
                    "started_at": busy.get("started_at"),
                    "requested_by": busy.get("requested_by"),
                },
                "detail": (
                    "Billed operations run one at a time: they share the rolling daily "
                    "spend ceiling and would interleave their run logs. Wait for it, or "
                    "stop it with job_cancel."
                ),
            }

    try:
        meta = jobs.launch(
            operation=op_key,
            argv=argv,
            cwd=cwd or SRC_DIR,
            label=label or op.title,
            bills=op.bills,
            requested_by=requested_by or None,
        )
    except RuntimeError as exc:
        return {"status": "error", "error": str(exc)}

    cost = cost_override or op.cost_usd
    return {
        "status": "started",
        "job_id": meta["job_id"],
        "operation": op_key,
        "label": meta["label"],
        "command": meta["command"],
        "started_at": meta["started_at"],
        "estimated_cost": catalog.format_cost(cost),
        "typical_duration": catalog.format_duration(op.minutes),
        "writes": list(op.writes),
        "next_step": (
            f"Poll job_status('{meta['job_id']}'). Expect it to take "
            f"{catalog.format_duration(op.minutes)}; job_log shows progress meanwhile."
        ),
    }


def _ticker(value: str) -> str:
    """Validate a ticker before it becomes a command-line argument."""
    return library._safe_ticker(value)


# ==========================================================================
# Discovery
# ==========================================================================

@mcp.tool()
def describe_capabilities(topic: str = "overview") -> dict:
    """Explain what this server can do, what each operation costs, and how to combine them.

    Call this first. It is the map for every other tool here, and it is generated from
    the same catalogue the tools are built from, so it cannot describe a cost or a
    precondition that differs from the one actually enforced.

    Args:
        topic: One of "overview", "operations", "costs", "workflows", "jobs",
            "library", "safety", or an operation key (e.g. "refine") for a single
            operation in full.
    """
    topic = (topic or "overview").strip().lower()

    if topic in catalog.BY_KEY:
        return {"topic": topic, "operation": catalog.as_dict(catalog.BY_KEY[topic])}

    if topic == "operations":
        grouped: dict = {}
        for op in catalog.OPERATIONS:
            grouped.setdefault(op.group, []).append(catalog.as_dict(op))
        return {"topic": "operations", "operations_by_group": grouped}

    if topic == "costs":
        return {
            "topic": "costs",
            "unit_costs": {
                "per_ticker_analysis_usd": catalog.COST_PER_TICKER_USD,
                "per_watch_buy_case_usd": catalog.COST_PER_WATCH_BUY_CASE_USD,
                "refinement_ceiling_usd": catalog.REFINEMENT_CEILING_USD,
            },
            "operations": [
                {
                    "tool": op.tool,
                    "cost": catalog.format_cost(op.cost_usd),
                    "duration": catalog.format_duration(op.minutes),
                    "bills": op.bills,
                }
                for op in sorted(catalog.OPERATIONS, key=lambda o: -(o.cost_usd[1] if o.cost_usd else 0))
            ],
            "free_operations": [op.tool for op in catalog.OPERATIONS if not op.bills],
            "how_to_spend_less": [
                "start_screen is free. Run it as often as you like and read the "
                "rankings with get_rankings before paying for any analysis.",
                f"Reports are reused for {catalog.REUSE_WINDOW_HOURS}h, so re-running "
                "start_analysis_from_csv with a larger top_n bills only the new "
                "tickers. Start shallow, then deepen.",
                "skip_sale_advisor drops Phase C for about a quarter off the "
                "per-ticker cost; skip_buy_case drops Phase E.",
                "start_buy_check_sweep with dry_run=True shows which names would be "
                "checked, for nothing.",
                "Every billed tool called without confirm=True returns a costed "
                "preview and spends nothing.",
            ],
        }

    if topic == "workflows":
        return {
            "topic": "workflows",
            "workflows": [
                {
                    "name": "Periodic research run (the main one)",
                    "when": "Every ~2 months, per Greenblatt's basket method.",
                    "steps": [
                        "start_screen - free, 20-30 min. Refreshes the rankings.",
                        "get_rankings - read the shortlist before spending anything.",
                        "start_analysis_from_csv(top_n=30, confirm=True) - $11-13, 25-40 min.",
                        "list_reports / read_report - read the Buy verdicts first.",
                        "start_critic_review(ticker, confirm=True) on the handful you "
                        "are actually about to act on.",
                    ],
                },
                {
                    "name": "Look at one company now",
                    "when": "A name came up and you want the full argument on it.",
                    "steps": [
                        "start_ticker_analysis(ticker, confirm=True) - ~$0.37-0.55, 2-5 min. "
                        "It does not have to have cleared the screen.",
                        "read_report(ticker, 'final').",
                        "read_report(ticker, 'buy_case') if the verdict came back Watch.",
                    ],
                },
                {
                    "name": "Decide on a stock you already own",
                    "steps": [
                        "start_sell_check(ticker, run_id=<the run you bought under>, "
                        "confirm=True) - pinning the run matters, see the caveat.",
                        "read_report(ticker, 'sell_check').",
                    ],
                },
                {
                    "name": "Watchlist maintenance",
                    "steps": [
                        "start_buy_check_sweep(dry_run=True) - free, shows who would be checked.",
                        "start_buy_check_sweep(confirm=True) - only the escalated names cost anything.",
                        "start_buy_case(ticker, confirm=True) to re-anchor a stale price range.",
                    ],
                },
                {
                    "name": "Tune the screen",
                    "steps": [
                        "get_screen_config - the current thresholds and the P/E ceiling they imply.",
                        "start_growth_persistence_study() - free, 10-25 min, re-derives the evidence.",
                        "Edit src/specs/config.yaml by hand, then start_screen to see the new gates.",
                    ],
                },
            ],
        }

    if topic == "jobs":
        return {
            "topic": "jobs",
            "why": (
                "Operations run for minutes to an hour, far longer than a tool call. "
                "start_* returns a job id immediately; the work continues in its own "
                "process and survives this server restarting."
            ),
            "tools": {
                "job_status": "State, elapsed time and exit code for one job.",
                "job_log": "Tail of the captured output, optionally filtered.",
                "job_list": "Recent jobs, newest first.",
                "job_cancel": "Stop a running job and its whole process tree.",
            },
            "states": {
                jobs.RUNNING: "In flight.",
                jobs.SUCCEEDED: "Exited 0.",
                jobs.FAILED: "Exited non-zero - read job_log for why.",
                jobs.CANCELLED: "Stopped by request. Work already finished is kept.",
                jobs.ORPHANED: (
                    "The server restarted mid-run, so the exit code was never observed. "
                    "The log tail says how far it got."
                ),
            },
            "polling_advice": (
                "Poll no more than once every 30-60s. Each start_* response carries the "
                "operation's typical duration - use it to pick an interval rather than "
                "polling tightly."
            ),
        }

    if topic == "library":
        return {
            "topic": "library",
            "why": (
                "These answer immediately and cost nothing. They matter most to a caller "
                "reaching this server over the network, which shares no filesystem with "
                "it and otherwise could not read anything a run produced."
            ),
            "tools": {
                "list_reports": "Every stored report, filterable by ticker and kind.",
                "read_report": "The text of one report, windowed for long documents.",
                "get_rankings": "The ranked candidate list from the last screen.",
                "get_screen_config": "Gates, PEG thresholds, budget ceilings, reuse window.",
            },
            "report_kinds": {
                "final": "The Buy/Watch/Avoid report from Phase B.",
                "refined": "A report after critic review (Phase D). The original is kept.",
                "sale_advisory": "Phase C: events that would break the thesis if you own it.",
                "buy_case": "Phase E: entry price range and triggers. Watch verdicts only.",
                "sell_check": "Result of testing a sale advisory - Sell or Hold.",
                "buy_check": "Result of testing a buy case - Buy or Wait.",
                "critic_review": "The critic's findings from a Phase D review.",
                "skeptical": "Legacy single-sided analysis from before the bull/bear split.",
            },
        }

    if topic == "safety":
        return {
            "topic": "safety",
            "confirmation": (
                "Every operation that makes billed LLM calls requires confirm=True. "
                "Without it you get a costed preview and nothing runs. Free operations "
                "(start_screen, start_ledger_snapshot, start_growth_persistence_study, "
                "and a dry-run sweep) need no confirmation."
            ),
            "serialisation": (
                "One billed operation at a time, server-wide. A second is refused and "
                "told which job is holding the slot."
            ),
            "budget_ceilings": (
                "The pipeline's own guards still apply underneath this layer: a per-run "
                "and a rolling 24h ceiling from config.yaml, which halt cleanly rather "
                "than overspending. Read them with get_screen_config. This layer never "
                "passes --no-budget; disabling a ceiling stays a deliberate act at the "
                "command line."
            ),
            "cancellation": (
                "job_cancel kills the process tree. Everything already finished is kept "
                "- the pipeline writes each ticker's report and rows as it completes them."
            ),
            "not_exposed": [
                "--no-budget, on any command. Removing a spend ceiling should not be "
                "one tool call away from an agent that cannot be billed.",
                "Editing config.yaml. Threshold changes alter every future screen, and "
                "two of the thresholds are multiplicatively linked; that stays manual.",
            ],
        }

    return {
        "topic": "overview",
        "server": "magic-stock-picker",
        "what_it_is": (
            "A multi-agent stock research pipeline. Phase A screens the FMP universe on "
            "Greenblatt's Magic Formula plus Lynch's PEG ratio; Phase B has a Bear Agent "
            "and a Bull Agent argue each candidate before a neutral Analyst reaches Buy / "
            "Watch / Avoid; Phase C names the events that would break the thesis; Phase E "
            "names the price and events that would turn a Watch into a Buy; Phase D is an "
            "opt-in independent critic that audits the analyst's reasoning."
        ),
        "tool_count": len(catalog.OPERATIONS) + 9,
        "start_here": [
            "describe_capabilities('workflows') - what to call in what order.",
            "describe_capabilities('costs') - what each operation spends.",
            "describe_capabilities('safety') - the confirmation and budget rules.",
            "get_rankings - the current shortlist, free, no run required.",
        ],
        "operations": [
            {
                "tool": op.tool,
                "title": op.title,
                "cost": catalog.format_cost(op.cost_usd),
                "duration": catalog.format_duration(op.minutes),
            }
            for op in catalog.OPERATIONS
        ],
        "two_rules": [
            "Billed tools need confirm=True; without it they preview and spend nothing.",
            "start_* returns a job id, not a result. Poll job_status.",
        ],
        "more": ["operations", "costs", "workflows", "jobs", "library", "safety"],
    }


# ==========================================================================
# Phase A - screening
# ==========================================================================

@mcp.tool()
def start_screen(requested_by: str = "") -> dict:
    """Run Phase A alone: refresh the ranked candidate list. Free, 20-30 minutes.

    Scans the FMP universe, applies Greenblatt's eligibility gates and the PEG growth
    test, and rewrites the rankings CSV. No agents run, nothing is written to the
    database, and nothing is billed - FMP is subscription-metered, so this costs time
    only. Read the result with `get_rankings`.

    Args:
        requested_by: Optional label for whoever asked, shown in job listings.
    """
    return _launch("screen", ["main.py", "--screen-only"], confirm=True, requested_by=requested_by)


# ==========================================================================
# Phases B / C / E - analysis
# ==========================================================================

@mcp.tool()
def start_pipeline(
    top_n: Optional[int] = None,
    skip_sale_advisor: bool = False,
    skip_buy_case: bool = False,
    force: bool = False,
    confirm: bool = False,
    requested_by: str = "",
) -> dict:
    """Run the full pipeline: screen the universe, then analyse the top N. $11-13, ~1 hour.

    The most expensive operation here. `start_screen` followed by
    `start_analysis_from_csv` does the same work in two steps and lets you read the
    shortlist before committing to the billed half.

    Args:
        top_n: How many ranked candidates to analyse, overriding the configured
            default of 30. The main cost lever, at roughly $0.37 per ticker.
        skip_sale_advisor: Drop Phase C, saving about a quarter of the per-ticker
            cost. No sale advisory is written, so a later sell-check will have
            nothing from this run to test.
        skip_buy_case: Drop Phase E, so no buy case is written even for a Watch.
        force: Re-analyse tickers even where a recent identical report exists.
            Normally leave this off - reuse is what makes deepening a run cheap.
        confirm: Required. Without it you get a costed preview and nothing runs.
        requested_by: Optional label for whoever asked.
    """
    argv = ["main.py"]
    if top_n is not None:
        if top_n < 1:
            return {"status": "error", "error": "top_n must be at least 1."}
        argv += ["--top-n", str(top_n)]
    if skip_sale_advisor:
        argv.append("--skip-sale-advisor")
    if skip_buy_case:
        argv.append("--skip-buy-case")
    if force:
        argv.append("--force")

    cost = catalog.estimate_batch_cost(top_n) if top_n else None
    return _launch("pipeline", argv, confirm=confirm, requested_by=requested_by, cost_override=cost)


@mcp.tool()
def start_analysis_from_csv(
    top_n: Optional[int] = None,
    csv_path: Optional[str] = None,
    skip_sale_advisor: bool = False,
    skip_buy_case: bool = False,
    force: bool = False,
    confirm: bool = False,
    requested_by: str = "",
) -> dict:
    """Analyse the top N from an existing rankings CSV, skipping the screener. ~$0.37/ticker.

    The billed half of a pipeline run. Because reports are reused for 24 hours,
    re-running this with a larger `top_n` bills only the tickers that are new - so
    starting shallow and deepening costs the same as going deep once.

    Args:
        top_n: How many candidates to analyse. Overrides the configured default.
        csv_path: A specific rankings CSV. Defaults to the one the last screen wrote.
        skip_sale_advisor: Drop Phase C (~25% cheaper per ticker).
        skip_buy_case: Drop Phase E, so no buy case for Watch verdicts.
        force: Bypass the reuse check and re-analyse everything, paying again.
        confirm: Required. Without it you get a costed preview and nothing runs.
        requested_by: Optional label for whoever asked.
    """
    argv = ["main.py", "--from-csv"]
    if csv_path:
        argv.append(csv_path)
    if top_n is not None:
        if top_n < 1:
            return {"status": "error", "error": "top_n must be at least 1."}
        argv += ["--top-n", str(top_n)]
    if skip_sale_advisor:
        argv.append("--skip-sale-advisor")
    if skip_buy_case:
        argv.append("--skip-buy-case")
    if force:
        argv.append("--force")

    cost = catalog.estimate_batch_cost(top_n) if top_n else None
    return _launch("from_csv", argv, confirm=confirm, requested_by=requested_by, cost_override=cost)


@mcp.tool()
def start_ticker_analysis(
    ticker: str,
    company: Optional[str] = None,
    skip_sale_advisor: bool = False,
    skip_buy_case: bool = False,
    force: bool = False,
    confirm: bool = False,
    requested_by: str = "",
) -> dict:
    """Analyse one named ticker on demand, bypassing the screener. ~$0.37-0.55, 2-5 min.

    Runs the bear/bull/analyst chain, the sale advisory, and a buy case if the verdict
    comes back Watch. Magic Formula ROC and Earnings Yield are computed on the fly, so
    the ticker does not have to have cleared the screen - this is the way to analyse a
    name the gates rejected.

    Args:
        ticker: The symbol, e.g. "BKNG".
        company: Optional explicit company name, when the symbol is ambiguous.
        skip_sale_advisor: Drop Phase C.
        skip_buy_case: Drop Phase E.
        force: Re-analyse even if a recent report exists.
        confirm: Required. Without it you get a costed preview and nothing runs.
        requested_by: Optional label for whoever asked.
    """
    try:
        symbol = _ticker(ticker)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    argv = ["main.py", symbol]
    if company:
        argv.append(company)
    if skip_sale_advisor:
        argv.append("--skip-sale-advisor")
    if skip_buy_case:
        argv.append("--skip-buy-case")
    if force:
        argv.append("--force")

    return _launch(
        "ticker", argv, confirm=confirm, requested_by=requested_by, label=f"Analyse {symbol}"
    )


# ==========================================================================
# Phase D - critic review
# ==========================================================================

@mcp.tool()
def start_critic_review(
    ticker: str,
    company: Optional[str] = None,
    run_id: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    max_rounds: Optional[int] = None,
    confirm: bool = False,
    requested_by: str = "",
) -> dict:
    """Put a finished report through independent critic review. $0.10-2.45, 3-15 min.

    An independent Critic Agent - its own instructions, its own research tools, no
    sight of the analyst's prompt - hunts for fallacies in the analyst's judgement:
    a verdict that does not follow from the report's own body, a Buy resting on a
    rumoured deal, a "priced in" claim with nothing behind it. The analyst revises,
    the critic re-reviews, until they agree or the budget runs out. The refined report
    says on its face which of those happened.

    Costs several times what producing the report cost, which is why it is opt-in.
    Run it on the handful of names you are actually about to act on. The report it
    reviews is never overwritten - the refinement gets its own run id.

    Args:
        ticker: The symbol whose report should be reviewed.
        company: Optional explicit company name.
        run_id: Review a specific run's report instead of the latest.
        max_budget_usd: Raise or lower the spend ceiling for this review. Defaults to
            refinement.max_budget_usd ($2.45). Enforced between rounds.
        max_rounds: Cap the review rounds instead. The budget usually binds first.
        confirm: Required. Without it you get a costed preview and nothing runs.
        requested_by: Optional label for whoever asked.
    """
    try:
        symbol = _ticker(ticker)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    if max_budget_usd is not None and max_budget_usd <= 0:
        return {"status": "error", "error": "max_budget_usd must be greater than 0."}
    if max_rounds is not None and max_rounds < 1:
        return {"status": "error", "error": "max_rounds must be at least 1."}

    argv = ["refine.py", symbol]
    if company:
        argv.append(company)
    if run_id:
        argv += ["--run", run_id]
    if max_budget_usd is not None:
        argv += ["--max-budget", f"{max_budget_usd:.2f}"]
    if max_rounds is not None:
        argv += ["--max-rounds", str(max_rounds)]

    cost = (0.10, max_budget_usd) if max_budget_usd else None
    return _launch(
        "refine",
        argv,
        confirm=confirm,
        requested_by=requested_by,
        cost_override=cost,
        label=f"Critic review of {symbol}",
    )


# ==========================================================================
# Derived-document repair
# ==========================================================================

@mcp.tool()
def start_sale_advisory(
    ticker: str,
    company: Optional[str] = None,
    run_id: Optional[str] = None,
    confirm: bool = False,
    requested_by: str = "",
) -> dict:
    """Generate or repair one report's sale advisory. ~$0.08-0.10, 1-3 min.

    The sale advisory names the three measurable business events that would signal the
    investment case is broken. The pipeline writes one per run; this writes one for any
    stored report without re-running the pipeline - when a run has none, when a critic
    revision left one stale, or when old thresholds need re-anchoring to current
    figures. About a fifth the cost of re-running the analysis, and it does not rewrite
    the report you already read.

    The advisory is stored on the run you name, so a sell-check pinned to that run
    picks it up. The cost gets its own row, so that run's own totals are not rewritten.

    Args:
        ticker: The symbol whose report the advisory is derived from.
        company: Optional explicit company name.
        run_id: Derive from a specific run's report instead of the latest.
        confirm: Required. Without it you get a costed preview and nothing runs.
        requested_by: Optional label for whoever asked.
    """
    try:
        symbol = _ticker(ticker)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    argv = ["sale_advisory.py", symbol]
    if company:
        argv.append(company)
    if run_id:
        argv += ["--run", run_id]

    return _launch(
        "sale_advisory",
        argv,
        confirm=confirm,
        requested_by=requested_by,
        label=f"Sale advisory for {symbol}",
    )


@mcp.tool()
def start_buy_case(
    ticker: str,
    company: Optional[str] = None,
    run_id: Optional[str] = None,
    confirm: bool = False,
    requested_by: str = "",
) -> dict:
    """Generate or repair one Watch report's buy case. ~$0.12-0.18, 1-3 min.

    A buy case names the entry price range and the three to five observable events
    that would turn a Watch into a Buy. It ages faster than anything else here - its
    first trigger is a price range measured against a quote that moves every session -
    so refreshing one is ordinary watchlist maintenance, not a repair.

    Refuses on a report whose verdict is Buy or Avoid, and there is no override. A Buy
    needs no entry conditions; an Avoid should not be handed any. If you think the
    verdict is wrong, use `start_critic_review` and let the critic move it on evidence.

    Args:
        ticker: The symbol whose Watch report the buy case is derived from.
        company: Optional explicit company name.
        run_id: Derive from a specific run's report instead of the latest.
        confirm: Required. Without it you get a costed preview and nothing runs.
        requested_by: Optional label for whoever asked.
    """
    try:
        symbol = _ticker(ticker)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    argv = ["buy_case.py", symbol]
    if company:
        argv.append(company)
    if run_id:
        argv += ["--run", run_id]

    return _launch(
        "buy_case",
        argv,
        confirm=confirm,
        requested_by=requested_by,
        label=f"Buy case for {symbol}",
    )


# ==========================================================================
# Condition checks
# ==========================================================================

@mcp.tool()
def start_sell_check(
    ticker: str,
    company: Optional[str] = None,
    run_id: Optional[str] = None,
    confirm: bool = False,
    requested_by: str = "",
) -> dict:
    """Test a stored sale advisory's conditions and advise Sell or Hold. ~$0.06-0.10.

    For a stock you already own. Loads the stored sale conditions, pulls current
    fundamentals plus live news and web research, marks each condition MET / NOT MET /
    UNCLEAR with evidence, and advises SELL if any is clearly met, otherwise HOLD.

    Pin `run_id` to the run you bought under. Sale conditions are the exit criteria of
    one specific entry thesis, and the default - the latest stored advisory - is only
    the right one if you have not re-analysed since buying. Otherwise you are testing
    conditions attached to a thesis you never acted on.

    Creates no pipeline run, so it never appears in the viewer's run lists.

    Args:
        ticker: The symbol to check.
        company: Optional explicit company name.
        run_id: Test a specific run's sale conditions - the run you purchased under.
        confirm: Required. Without it you get a costed preview and nothing runs.
        requested_by: Optional label for whoever asked.
    """
    try:
        symbol = _ticker(ticker)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    argv = ["main.py", "--sell-check", symbol]
    if company:
        argv.append(company)
    if run_id:
        argv += ["--run", run_id]

    return _launch(
        "sell_check",
        argv,
        confirm=confirm,
        requested_by=requested_by,
        label=f"Sell check on {symbol}",
    )


@mcp.tool()
def start_buy_check(
    ticker: str,
    company: Optional[str] = None,
    run_id: Optional[str] = None,
    confirm: bool = False,
    requested_by: str = "",
) -> dict:
    """Test a stored buy case's triggers and advise Buy or Wait. ~$0.06-0.10.

    For a stock you do not own. Loads the stored buy case, fetches today's price plus
    current fundamentals and live research, marks each trigger MET / NOT MET / UNCLEAR,
    reports what has moved since (consensus estimates, analyst targets, any bid), and
    advises BUY or WAIT.

    Note the opposite default from the sell check, deliberately: a buy decision is
    being made today, so the newest buy case is the right one. Pin `run_id` only to ask
    what a particular past case would say now.

    Args:
        ticker: The symbol to check.
        company: Optional explicit company name.
        run_id: Test a specific run's buy case instead of the latest.
        confirm: Required. Without it you get a costed preview and nothing runs.
        requested_by: Optional label for whoever asked.
    """
    try:
        symbol = _ticker(ticker)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    argv = ["main.py", "--buy-check", symbol]
    if company:
        argv.append(company)
    if run_id:
        argv += ["--run", run_id]

    return _launch(
        "buy_check",
        argv,
        confirm=confirm,
        requested_by=requested_by,
        label=f"Buy check on {symbol}",
    )


# ==========================================================================
# Scheduled sweeps
# ==========================================================================

@mcp.tool()
def start_buy_check_sweep(
    dry_run: bool = False, confirm: bool = False, requested_by: str = ""
) -> dict:
    """Test every open buy case; alert on any that now reads Buy. Free to ~$0.40.

    A free arithmetic pre-filter runs first, using the price each buy case wrote down.
    Only names at or within 5% of their trigger, or that have reported since the last
    sweep, reach the model - so a quiet week costs nothing and a busy one costs cents.
    Notifies by push and email on Buy, once per standing signal rather than weekly.

    Args:
        dry_run: Run the pre-filter only and report who would be checked. No model
            calls, no alerts, no cost, nothing changed. Worth doing first.
        confirm: Required unless dry_run is set.
        requested_by: Optional label for whoever asked.
    """
    argv = ["cron/run_buy_check_sweep.py"]
    if dry_run:
        argv.append("--dry-run")
        # A dry run makes no model calls, so the confirmation gate would be asking
        # the caller to approve spending nothing.
        return _launch(
            "buy_check_sweep",
            argv,
            confirm=True,
            requested_by=requested_by,
            cost_override=None,
            label="Buy-check sweep (dry run)",
        )
    return _launch("buy_check_sweep", argv, confirm=confirm, requested_by=requested_by)


@mcp.tool()
def start_ledger_snapshot(requested_by: str = "") -> dict:
    """Record this week's prices for every subject in the verdict ledger. Free.

    Prices every verdict the system has reached AND every name that merely cleared the
    screen, then appends a dated row with its return and excess return against the
    benchmark. Arithmetic only - no model, no cost. The rejected names are priced
    deliberately: without them there is no control group, and the ledger's central
    question, whether filtering the screen beat not filtering it, cannot be asked.

    Reporting "No ledger subjects due for a snapshot" is a pass, not a failure - it
    means no analysis run has filled the ledger yet.

    Args:
        requested_by: Optional label for whoever asked.
    """
    return _launch(
        "ledger_snapshot", ["cron/run_outcome_snapshot.py"], confirm=True, requested_by=requested_by
    )


# ==========================================================================
# Offline tuning study
# ==========================================================================

@mcp.tool()
def start_growth_persistence_study(
    per_sector: Optional[int] = None, requested_by: str = ""
) -> dict:
    """Re-derive the evidence behind the three PEG thresholds. Free, 10-25 min.

    An offline instrument that nothing in the pipeline imports. It samples the largest
    companies in each allowed sector and prints three tables: whether fast growth
    actually persists, the distribution of realised growth (set the growth cap near the
    95th percentile), and base-window net margins (set the margin floor below the 25th).

    Run it before changing max_growth_rate_for_peg, max_peg or min_base_net_margin -
    and roughly once a year regardless, so a threshold tuned to one market does not
    quietly persist into a different one. It makes no LLM calls and writes nothing.

    Args:
        per_sector: How many companies to sample per sector. Defaults to 22; 40 gives
            a larger sample and takes proportionally longer.
        requested_by: Optional label for whoever asked.
    """
    argv = ["tools/analyze_growth_persistence.py"]
    if per_sector is not None:
        if per_sector < 1:
            return {"status": "error", "error": "per_sector must be at least 1."}
        argv += ["--per-sector", str(per_sector)]
    # The only operation that runs from the repository root rather than src/ - it is
    # invoked as `python tools/...` in the README, and resolves its paths from there.
    return _launch(
        "growth_persistence",
        argv,
        confirm=True,
        requested_by=requested_by,
        cwd=PROJECT_ROOT,
    )


# ==========================================================================
# Job control
# ==========================================================================

@mcp.tool()
def job_status(job_id: str) -> dict:
    """State, elapsed time and exit code for one job.

    States are RUNNING, SUCCEEDED, FAILED, CANCELLED, or ORPHANED (the server
    restarted mid-run, so the exit code was never observed - read the log tail).

    Args:
        job_id: The id a start_* tool returned.
    """
    meta = jobs.status(job_id)
    if meta is None:
        return {
            "error": f"No such job '{job_id}'.",
            "hint": "job_list shows recent jobs. Job records are pruned once old.",
        }
    if meta["state"] == jobs.FAILED:
        meta["hint"] = f"Read job_log('{job_id}') for the traceback or the failing step."
    elif meta["state"] == jobs.RUNNING:
        meta["hint"] = f"Still running. job_log('{job_id}') shows current progress."
    elif meta["state"] == jobs.SUCCEEDED:
        meta["hint"] = "Finished. Use list_reports / read_report to read what it produced."
    return meta


@mcp.tool()
def job_log(job_id: str, tail_lines: int = 80, grep: Optional[str] = None) -> dict:
    """Read the tail of a job's captured output.

    A pipeline run's log is megabytes, so this returns the end of it rather than the
    whole thing. Use `grep` to pull out one ticker or one kind of line.

    Args:
        job_id: The id a start_* tool returned.
        tail_lines: How many lines from the end. Default 80.
        grep: Case-insensitive substring filter applied before the tail, e.g. a ticker
            symbol, "ERROR", or "verdict".
    """
    if tail_lines < 1:
        return {"error": "tail_lines must be at least 1."}
    return jobs.read_log(job_id, tail_lines=tail_lines, grep=grep)


@mcp.tool()
def job_list(limit: int = 20, state: Optional[str] = None) -> dict:
    """Recent jobs, newest first.

    Args:
        limit: How many to return. Default 20.
        state: Filter to one state - RUNNING, SUCCEEDED, FAILED, CANCELLED, ORPHANED.
    """
    if state and state.upper() not in (
        jobs.RUNNING, jobs.SUCCEEDED, jobs.FAILED, jobs.CANCELLED, jobs.ORPHANED
    ):
        return {"error": f"Unknown state '{state}'."}
    found = jobs.list_jobs(limit=limit, state=state)
    billed = jobs.running_billed_job()
    return {
        "jobs": [
            {
                key: meta.get(key)
                for key in (
                    "job_id", "operation", "label", "state", "bills",
                    "started_at", "ended_at", "exit_code", "requested_by",
                )
            }
            for meta in found
        ],
        "count": len(found),
        "billed_slot": (
            {"in_use_by": billed["job_id"], "operation": billed["operation"]}
            if billed
            else "free - a billed operation can start now"
        ),
    }


@mcp.tool()
def job_cancel(job_id: str) -> dict:
    """Stop a running job and its whole process tree.

    Work already completed is kept: the pipeline writes each ticker's report and
    database rows as it finishes them, so a cancelled run leaves a shorter candidate
    list rather than nothing.

    Args:
        job_id: The id of the job to stop.
    """
    return jobs.cancel(job_id)


# ==========================================================================
# Read-only library
# ==========================================================================

@mcp.tool()
def list_reports(ticker: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> dict:
    """List stored reports, newest first. Free, immediate.

    Args:
        ticker: Filter to one symbol.
        kind: Filter to one kind - final, refined, sale_advisory, buy_case,
            sell_check, buy_check, critic_review, skeptical.
        limit: How many to return. Default 100.
    """
    try:
        return library.list_reports(ticker=ticker, kind=kind, limit=limit)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
def read_report(ticker: str, kind: str = "final", max_chars: int = 40000, offset: int = 0) -> dict:
    """Read the text of one stored report. Free, immediate.

    Reports run long, so the read is windowed. When the document exceeds `max_chars`
    the response returns `next_offset` to continue from rather than silently handing
    back a truncated document.

    Args:
        ticker: The symbol.
        kind: Which document - "final" (the Buy/Watch/Avoid report), "refined" (after
            critic review), "sale_advisory", "buy_case", "sell_check", "buy_check",
            "critic_review", or "skeptical" (legacy).
        max_chars: Window size. Default 40000.
        offset: Character offset to start from, for continuing a long read.
    """
    try:
        return library.read_report(ticker=ticker, kind=kind, max_chars=max_chars, offset=offset)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
def get_rankings(limit: int = 30, csv_path: Optional[str] = None) -> dict:
    """The ranked candidate list from the last screen. Free, immediate.

    Each row carries the Magic Formula measures - Return on Capital, Earnings Yield -
    and the PEG figures, in the combined-rank order the screen produced. Reading this
    before starting an analysis is how you decide what `top_n` is worth paying for.

    Args:
        limit: How many candidates to return. Default 30.
        csv_path: A specific rankings CSV. Defaults to the last screen's output.
    """
    return library.get_rankings(limit=limit, csv_path=csv_path)


@mcp.tool()
def get_screen_config() -> dict:
    """The gates, thresholds and spend ceilings the pipeline runs on. Free, immediate.

    Returns the eligibility gates, the three PEG thresholds (with the hard P/E ceiling
    their product implies for the whole screen), the budget ceilings, and the reuse
    window. Read it to explain why a name did or did not make the list, and what a run
    will refuse to spend.
    """
    return library.get_screen_config()


# ==========================================================================
# Transport
# ==========================================================================

class _BearerAuth:
    """Minimal bearer-token gate for the HTTP transports.

    The tools behind this server spend real money and reach a production database, so
    a network-reachable deployment must not be open. This is deliberately the smallest
    thing that is actually correct: a constant-time compare against a shared secret.
    For anything multi-tenant, put a real proxy in front and keep this as the backstop.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        presented = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {self.token}"
        # compare_digest on the whole header, so neither the scheme nor the token
        # length leaks through timing.
        if not hmac.compare_digest(presented, expected):
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="magic-stock-picker"'),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class _Health:
    """Unauthenticated liveness endpoint at /health.

    An always-on deployment needs something a service manager or an uptime check can
    poll without holding a credential. It reports liveness only - no job details, no
    counts - because it sits outside the token gate and a monitoring probe does not
    need to know what the box is working on. `job_list` gives that, authenticated.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/health":
            return await self.app(scope, receive, send)
        body = b'{"status":"ok","server":"magic-stock-picker"}'
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _serve_http(host: str, port: int, token: Optional[str], stateless: bool = False) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    if host not in _LOOPBACK and not token:
        raise SystemExit(
            f"Refusing to bind {host}:{port} without a token.\n\n"
            "These tools spend money and reach the database, so a network-reachable\n"
            "server must be authenticated. Set MSP_MCP_TOKEN (or pass --token), or\n"
            "bind 127.0.0.1 and reach it through an SSH tunnel instead."
        )

    # With rebinding protection on, an empty allowed_hosts rejects every request, so
    # the list has to be populated explicitly for a non-loopback bind. `:*` matches
    # any port on that host.
    configured = os.environ.get("MSP_MCP_ALLOWED_HOSTS", "")
    allowed = [h.strip() for h in configured.split(",") if h.strip()] or [
        f"{host}:*", host, "localhost:*", "localhost", "127.0.0.1:*", "127.0.0.1",
    ]
    mcp.settings.host = host
    mcp.settings.port = port
    # Stateless suits an always-on server with remote clients: every request stands
    # alone, so a restart does not strand a client holding a session id the server no
    # longer knows. Nothing is lost by it here - this layer keeps no per-session
    # state at all, only job files on disk.
    mcp.settings.stateless_http = stateless
    mcp._transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=[f"http://{h}" for h in allowed] + [f"https://{h}" for h in allowed],
    )

    app = mcp.streamable_http_app()
    if token:
        app = _BearerAuth(app, token)
    # Outside the token gate, so an uptime check needs no credential.
    app = _Health(app)

    endpoint = f"http://{host}:{port}{mcp.settings.streamable_http_path}"
    logger.info("magic-stock-picker MCP server on %s", endpoint)
    logger.info("Authentication: %s", "bearer token required" if token else "NONE (loopback only)")
    logger.info("Session mode: %s", "stateless" if stateless else "session-tracked")
    logger.info("Health check: http://%s:%d/health", host, port)
    uvicorn.run(app, host=host, port=port, log_level=mcp.settings.log_level.lower())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="MCP server exposing the magic-stock-picker CLI as tools",
        epilog=(
            "  python -m agent_mcp.server\n"
            "      stdio, for a client running on this machine (Claude Desktop, Claude Code)\n\n"
            "  python -m agent_mcp.server --transport http\n"
            "      HTTP on 127.0.0.1:8765 for a local client that speaks HTTP\n\n"
            "  MSP_MCP_TOKEN=... python -m agent_mcp.server --transport http --host 0.0.0.0\n"
            "      reachable from other machines. A token is REQUIRED for a non-loopback\n"
            "      bind: these tools spend money and reach the database.\n\n"
            "Environment:\n"
            "  MSP_MCP_TOKEN         bearer token clients must present (HTTP transports)\n"
            "  MSP_MCP_ALLOWED_HOSTS comma-separated Host header allowlist, if the default\n"
            "                        (the bind host plus loopback) is wrong for your proxy\n"
            "  MSP_MCP_JOB_DIR       where job metadata and logs are kept\n"
            "  MSP_PYTHON            interpreter used to run the pipeline, if not this one\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--transport", choices=("stdio", "http", "sse"), default="stdio",
        help="stdio (default) for a local client; http for streamable HTTP; sse is legacy",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for HTTP transports")
    parser.add_argument("--port", type=int, default=8765, help="Bind port for HTTP transports")
    parser.add_argument(
        "--token", default=None,
        help="Bearer token clients must present. Prefer MSP_MCP_TOKEN over this - an "
             "argument is visible to anyone who can list processes",
    )
    parser.add_argument(
        "--stateless", action="store_true",
        help="Treat every HTTP request as independent. Recommended for an always-on "
             "server with remote clients: a restart then never strands a client "
             "holding a session id the server has forgotten",
    )
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    args = parser.parse_args()

    # stdio speaks the protocol over stdout, so every diagnostic must go to stderr or
    # it corrupts the stream.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    mcp.settings.log_level = args.log_level

    if not SRC_DIR.exists():
        raise SystemExit(f"Expected the pipeline at {SRC_DIR}, but it is not there.")

    token = args.token or os.environ.get("MSP_MCP_TOKEN")

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "http":
        _serve_http(args.host, args.port, token, stateless=args.stateless)
    else:
        mcp.settings.host, mcp.settings.port = args.host, args.port
        if args.host not in _LOOPBACK and not token:
            raise SystemExit("Refusing a non-loopback SSE bind without a token. Set MSP_MCP_TOKEN.")
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
