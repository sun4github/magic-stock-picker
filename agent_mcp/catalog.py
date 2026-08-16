"""The catalogue of operations this MCP layer exposes.

This module is data, not behaviour. Every entry describes one command the CLI
already offers: what it costs, how long it takes, what it writes, and which MCP
tool wraps it. Both the tool wrappers in `server.py` and the `describe_capabilities`
discovery tool read from here, which is the point — a cost or a caveat written once
cannot drift out of step between what a tool charges and what the discovery tool
claims it charges.

Costs are the figures published in the root README and `src/specs/config.yaml`,
not fresh guesses. Where the README gives a range ("$11-13"), the range is kept:
an estimate that pretends to more precision than the source has is worse than one
that shows its width.
"""

from dataclasses import dataclass, field
from typing import Optional, Sequence

# --------------------------------------------------------------------------
# Unit costs, from the root README's "Running" section and config.yaml.
# Everything else here is arithmetic on these four numbers, so a change in
# per-ticker pricing is a one-line edit rather than a hunt through estimates.
# --------------------------------------------------------------------------
COST_PER_TICKER_USD = 0.37          # Phase B + C for one candidate
COST_PER_WATCH_BUY_CASE_USD = 0.18  # Phase E, only for a Watch verdict
WATCH_RATE = 0.5                    # rough share of verdicts that come back Watch
REFINEMENT_CEILING_USD = 2.45       # refinement.max_budget_usd

#: Phase B/C reuse window (`reuse.max_age_hours`). Re-analysis inside it is ~$0.
REUSE_WINDOW_HOURS = 24


@dataclass(frozen=True)
class Operation:
    """One CLI operation, and everything a caller needs to decide whether to run it."""

    key: str
    tool: str
    title: str
    cli: str
    summary: str
    #: True when the operation makes billed LLM calls. Billed operations require an
    #: explicit `confirm=True` and are serialised one at a time — see server.py.
    bills: bool
    #: (low, high) USD. None for operations that make no LLM calls at all.
    cost_usd: Optional[tuple] = None
    #: (low, high) wall-clock minutes, for setting a caller's polling interval.
    minutes: tuple = (1, 5)
    #: Files and tables the operation writes, relative to `src/`.
    writes: Sequence[str] = field(default_factory=tuple)
    #: Preconditions that will make the operation fail if unmet.
    requires: Optional[str] = None
    #: The trap worth knowing before running it.
    caveat: Optional[str] = None
    #: Grouping used by the discovery tool.
    group: str = "analysis"


OPERATIONS = (
    # ---------------------------------------------------------------- Phase A
    Operation(
        key="screen",
        tool="start_screen",
        title="Phase A only - refresh the rankings",
        cli="python main.py --screen-only",
        summary=(
            "Scans the FMP universe, applies Greenblatt's eligibility gates plus the "
            "PEG growth test, and rewrites the rankings CSV. No agents, no database "
            "writes, nothing billed."
        ),
        bills=False,
        cost_usd=None,
        minutes=(20, 30),
        writes=("magic_formula_rankings_live.csv", "rankings_history/"),
        caveat=(
            "Free but slow - FMP is subscription-metered, so this costs time only. "
            "Run it as often as you like; pay only when you run the analysis on top."
        ),
        group="screening",
    ),
    # ------------------------------------------------------- Phases A + B + C + E
    Operation(
        key="pipeline",
        tool="start_pipeline",
        title="Full pipeline - Phase A, then B/C/E on the top N",
        cli="python main.py",
        summary=(
            "The whole run: screen the universe, then put the top N candidates through "
            "the bear/bull/analyst chain, the sale advisory, and a buy case for every "
            "Watch verdict."
        ),
        bills=True,
        cost_usd=(11.0, 13.0),
        minutes=(50, 65),
        writes=(
            "reports/<TICKER>_Final_Report_<Verdict>.md",
            "reports/<TICKER>_Sale_Advisory.md",
            "reports/<TICKER>_Buy_Case.md (Watch only)",
            "pipeline_runs / verdict_log / run_universe rows",
        ),
        caveat=(
            "The most expensive operation here. `start_screen` then "
            "`start_analysis_from_csv` does the same work in two steps and lets you "
            "read the shortlist before spending anything."
        ),
    ),
    Operation(
        key="from_csv",
        tool="start_analysis_from_csv",
        title="Phase B/C/E from an existing rankings CSV",
        cli="python main.py --from-csv [PATH] [--top-n N]",
        summary=(
            "Skips the screener and analyses the top N candidates from a rankings CSV a "
            "previous screen produced. The billed half of a full pipeline run."
        ),
        bills=True,
        cost_usd=(11.0, 13.0),
        minutes=(25, 40),
        writes=(
            "reports/<TICKER>_Final_Report_<Verdict>.md",
            "reports/<TICKER>_Sale_Advisory.md",
            "reports/<TICKER>_Buy_Case.md (Watch only)",
        ),
        requires="A rankings CSV from a prior screen (`start_screen`, or a full pipeline run).",
        caveat=(
            f"Re-running with a larger top_n inside the {REUSE_WINDOW_HOURS}h reuse "
            "window bills only the NEW tickers, so it is safe to start shallow and "
            "deepen. Use force=True only when you actually want to pay twice."
        ),
    ),
    Operation(
        key="ticker",
        tool="start_ticker_analysis",
        title="On-demand analysis of a single ticker",
        cli="python main.py TICKER [Company]",
        summary=(
            "Bypasses the screener entirely and runs Phase B/C (and E on a Watch) for "
            "one named ticker. Magic Formula ROC and Earnings Yield are computed on the "
            "fly so the Bull Agent still has a value signal to argue from."
        ),
        bills=True,
        cost_usd=(0.37, 0.55),
        minutes=(2, 5),
        writes=(
            "reports/<TICKER>_Final_Report_<Verdict>.md",
            "reports/<TICKER>_Sale_Advisory.md",
            "reports/<TICKER>_Buy_Case.md (Watch only)",
        ),
        caveat=(
            "The ticker does not have to have cleared the screen - this is the escape "
            "hatch for analysing a name the gates rejected."
        ),
    ),
    # ---------------------------------------------------------------- Phase D
    Operation(
        key="refine",
        tool="start_critic_review",
        title="Phase D - independent critic review (opt-in)",
        cli="python refine.py TICKER [--run ID] [--max-budget USD] [--max-rounds N]",
        summary=(
            "Puts a finished report in front of an independent Critic Agent - its own "
            "instructions, its own research tools, no sight of the analyst's prompt - "
            "which hunts for fallacies in the analyst's judgement. The analyst revises, "
            "the critic re-reviews, until they agree or the budget runs out."
        ),
        bills=True,
        cost_usd=(0.10, REFINEMENT_CEILING_USD),
        minutes=(3, 15),
        writes=(
            "reports/<TICKER>_Refined_Report_<Verdict>.md",
            "reports/<TICKER>_Critic_Review.md",
            "its own pipeline_runs row + critic_memory findings",
        ),
        requires="A stored final report for the ticker.",
        caveat=(
            "Costs several times what producing the report cost. Deliberately not part "
            "of the pipeline - run it on the handful of names you are about to act on. "
            "The report it reviews is never overwritten."
        ),
        group="review",
    ),
    # ------------------------------------------------- Derived-document repair
    Operation(
        key="sale_advisory",
        tool="start_sale_advisory",
        title="Generate or repair one report's sale advisory",
        cli="python sale_advisory.py TICKER [Company] [--run ID]",
        summary=(
            "Writes a Phase C sale advisory for any stored report without re-running the "
            "pipeline. Use it when a run has none, when a critic revision left one "
            "stale, or when its thresholds need re-anchoring to current figures."
        ),
        bills=True,
        cost_usd=(0.08, 0.10),
        minutes=(1, 3),
        writes=("reports/<TICKER>_Sale_Advisory.md", "a SALE_CASE on the named run"),
        requires="A stored final report for the ticker.",
        caveat=(
            "The advisory is stored on the run you name, so a later sell-check pinned to "
            "that run picks it up; the cost gets its own row so the named run's totals "
            "are not rewritten."
        ),
        group="repair",
    ),
    Operation(
        key="buy_case",
        tool="start_buy_case",
        title="Generate or repair one Watch report's buy case",
        cli="python buy_case.py TICKER [Company] [--run ID]",
        summary=(
            "The mirror of the sale advisory: writes the entry price range and the "
            "triggers that would turn a Watch into a Buy. Refreshing one is ordinary "
            "watchlist maintenance - its price trigger ages every session."
        ),
        bills=True,
        cost_usd=(0.12, 0.18),
        minutes=(1, 3),
        writes=("reports/<TICKER>_Buy_Case.md", "a BUY_CASE on the named run"),
        requires="A stored report whose verdict is Watch.",
        caveat=(
            "Refuses on a Buy or Avoid verdict, and there is no override. If you think "
            "the verdict is wrong, run start_critic_review instead of writing entry "
            "conditions underneath a verdict that says no."
        ),
        group="repair",
    ),
    # ------------------------------------------------------ Condition checking
    Operation(
        key="sell_check",
        tool="start_sell_check",
        title="Test a stored sale advisory's conditions (Sell / Hold)",
        cli="python main.py --sell-check TICKER [Company] [--run ID]",
        summary=(
            "For a stock you already own. Loads a stored SALE_CASE, pulls current "
            "fundamentals and live research, marks each condition MET / NOT MET / "
            "UNCLEAR with evidence, and advises SELL or HOLD."
        ),
        bills=True,
        cost_usd=(0.06, 0.10),
        minutes=(1, 3),
        writes=("reports/<TICKER>_Sell_Check.md",),
        requires="A prior run that produced a SALE_CASE for the ticker.",
        caveat=(
            "Defaults to the LATEST sale case, which is only correct if you have not "
            "re-analysed since buying. Pin run_id to the run you purchased under, or you "
            "will be testing exit criteria for a thesis you never acted on. Creates no "
            "pipeline run, so it never appears in the viewer's run lists."
        ),
        group="checks",
    ),
    Operation(
        key="buy_check",
        tool="start_buy_check",
        title="Test a stored buy case's triggers (Buy / Wait)",
        cli="python main.py --buy-check TICKER [Company] [--run ID]",
        summary=(
            "For a stock you do NOT own. Loads a stored BUY_CASE, fetches today's price "
            "plus current fundamentals and live research, marks each trigger MET / NOT "
            "MET / UNCLEAR, and advises BUY or WAIT."
        ),
        bills=True,
        cost_usd=(0.06, 0.10),
        minutes=(1, 3),
        writes=("reports/<TICKER>_Buy_Check.md", "a BUY_CHECK record on the tested run"),
        requires="A prior run that reached a Watch verdict and produced a BUY_CASE.",
        caveat=(
            "Note the opposite default from the sell-check, deliberately: a buy decision "
            "is being made TODAY, so the newest buy case is the right one. Pin run_id "
            "only to ask what a particular past case would say now."
        ),
        group="checks",
    ),
    # ------------------------------------------------------- Scheduled sweeps
    Operation(
        key="buy_check_sweep",
        tool="start_buy_check_sweep",
        title="Weekly buy-check sweep over every open buy case",
        cli="python cron/run_buy_check_sweep.py [--dry-run]",
        summary=(
            "Tests every stored buy case still on Watch. A free arithmetic pre-filter "
            "runs first and only escalates names whose price is at or within 5% of the "
            "trigger, or that have reported since the last sweep - so most weeks cost "
            "little or nothing. Notifies on Buy."
        ),
        bills=True,
        cost_usd=(0.0, 0.40),
        minutes=(1, 10),
        writes=(
            "reports/<TICKER>_Buy_Check.md (overwritten each check)",
            "BUY_CHECK database records (durable, accumulating)",
            "logs/buy_check_sweep.log",
        ),
        caveat=(
            "dry_run=True runs the pre-filter only and reports who WOULD be checked - no "
            "model calls, no alerts, no cost, nothing changed. Always worth doing first."
        ),
        group="scheduled",
    ),
    Operation(
        key="ledger_snapshot",
        tool="start_ledger_snapshot",
        title="Weekly verdict-ledger price snapshot",
        cli="python cron/run_outcome_snapshot.py",
        summary=(
            "Prices every subject in the verdict ledger that is still inside its "
            "tracking window - every verdict AND every name that merely cleared the "
            "screen - and appends a dated row with its return and excess return."
        ),
        bills=False,
        cost_usd=None,
        minutes=(1, 5),
        writes=("verdict_outcome rows",),
        caveat=(
            "Arithmetic only, no model. Reporting 'No ledger subjects due for a "
            "snapshot' is a pass, not a failure - it means no analysis run has filled "
            "the ledger yet."
        ),
        group="scheduled",
    ),
    # ------------------------------------------------------------ Offline study
    Operation(
        key="growth_persistence",
        tool="start_growth_persistence_study",
        title="Re-derive the three PEG thresholds from current data",
        cli="python tools/analyze_growth_persistence.py [--per-sector N]",
        summary=(
            "An offline instrument, not part of the pipeline. Samples the largest "
            "companies in each allowed sector and prints three tables: growth "
            "persistence, the distribution of realised growth, and base-window net "
            "margins - the evidence behind max_growth_rate_for_peg, max_peg and "
            "min_base_net_margin."
        ),
        bills=False,
        cost_usd=None,
        minutes=(10, 25),
        writes=("nothing - it prints and exits",),
        caveat=(
            "Run it before changing any of the three thresholds, and roughly once a "
            "year regardless. Remember max_peg and max_growth_rate_for_peg are "
            "multiplicatively linked: they set a hard P/E ceiling for the whole screen "
            "(1.5 x 0.60 x 100 = 90), so neither moves alone."
        ),
        group="tuning",
    ),
)

BY_KEY = {op.key: op for op in OPERATIONS}
BY_TOOL = {op.tool: op for op in OPERATIONS}


def estimate_batch_cost(top_n: int) -> tuple:
    """Estimated (low, high) USD for analysing `top_n` candidates.

    Low assumes no ticker earns a buy case; high assumes every one does. The
    realistic middle is roughly half, since Watch is the most common verdict.
    """
    low = top_n * COST_PER_TICKER_USD
    high = top_n * (COST_PER_TICKER_USD + COST_PER_WATCH_BUY_CASE_USD)
    return (round(low, 2), round(high, 2))


def format_cost(cost: Optional[tuple]) -> str:
    """Render a cost range for humans, collapsing a zero-width range to one figure."""
    if cost is None:
        return "free (no LLM calls)"
    low, high = cost
    if abs(high - low) < 0.005:
        return f"~${low:.2f}"
    return f"${low:.2f}-${high:.2f}"


def format_duration(minutes: tuple) -> str:
    low, high = minutes
    return f"~{low} min" if low == high else f"~{low}-{high} min"


def as_dict(op: Operation) -> dict:
    """The catalogue entry as the discovery tool and the previews return it."""
    return {
        "operation": op.key,
        "tool": op.tool,
        "title": op.title,
        "cli_equivalent": op.cli,
        "summary": op.summary,
        "group": op.group,
        "bills": op.bills,
        "estimated_cost": format_cost(op.cost_usd),
        "estimated_cost_usd": list(op.cost_usd) if op.cost_usd else None,
        "typical_duration": format_duration(op.minutes),
        "writes": list(op.writes),
        "requires": op.requires,
        "caveat": op.caveat,
    }
