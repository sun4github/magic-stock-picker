"""Critic/analyst refinement loop — the expensive, opt-in second opinion.

    python refine.py TICKER [Company] [--run SOURCE_RUN_ID] [--max-budget 2.25]

Takes a final report the pipeline has ALREADY produced and puts it through rounds
of independent review until the critic agrees with it or the money runs out. The
result is a refined report that says, on its face, which of those two happened.

## Why this is not part of the pipeline

Because it costs several times what producing the report cost in the first place,
and takes as long. A 30-ticker Phase B run is ~$11 and already the most expensive
thing here; putting a 2-4 round critic loop behind every one of those tickers would
multiply that by roughly the round count, for a batch where most reports are
'Watch' and will not be acted on anyway. Refinement is worth paying for on the
handful of names you are actually about to buy — which is a decision only the
operator can make, so it is a separate command taken deliberately, one ticker at a
time, with a spend ceiling named on the command line.

## Why a hand-written loop rather than ADK's LoopAgent

`LoopAgent` runs its sub-agents in order until `max_iterations` or an escalation
event. It would work, and it would cost more to work with:

- **The stopping condition is a spend decision, not an agent decision.** Committing
  to a revision commits to three things at once — the revision, the critique that
  must follow it, and the sale advisory that revision invalidates — so the loop
  stops when it can no longer afford all three together. Deciding that needs the
  running dollar total and a per-role estimate, figures that live in `main`'s cost
  accounting, outside any agent's context. Getting a LoopAgent to stop on it means
  an escalation callback reaching into the same accounting anyway.
- **Cost has to be attributed per role per round**, exactly as `_run_pipeline_async`
  attributes it per role per ticker, or a prompt change that doubles the critic's
  cost is invisible inside a moving loop total.
- **The hand-off is a rewrite, not an append.** Each revision REPLACES the report
  under review; state that shifts every round is clearer built in Python than
  threaded through session deltas.
- `main.py` already rejected `SequentialAgent` for the pipeline, for the
  neighbouring reason (per-agent 429 retry). Two orchestration idioms in one
  codebase would be worse than one.

## Never mid-round

Every ceiling in here is checked BETWEEN rounds. A round that is abandoned half way
has been billed for and produces nothing, which is strictly worse than the overspend
it would have prevented — the same rule `_check_budget` follows between tickers.
"""
import os
import re
import json
import uuid
from datetime import datetime, timezone

import main
import critic_agent
import buy_case_agent
from critic_agent import (
    critic_agent as critic,
    reviser_agent,
    parse_findings,
    extract_critic_verdict,
    split_revision,
    format_past_corrections,
    finding_summary,
    run_agent,
    BLOCKING_SEVERITIES,
)
from mcp_server import (
    compute_ticker_magic_metrics,
    fmp_quarterly_trends,
    db_create_pipeline_run,
    db_store_agent_output,
    db_store_final_report,
    db_store_ticker_run,
    db_get_agent_output,
    db_get_final_report,
    db_get_critic_memory,
    db_store_critic_findings,
    db_record_analyst_response,
    db_resolve_critic_findings,
)

logger = main.logger

_cfg = main.config.get("refinement", {}) or {}
MAX_BUDGET_USD = float(_cfg.get("max_budget_usd", 2.25))
MAX_ROUNDS = int(_cfg.get("max_rounds", 3))
# Seed estimates for the first ceiling check, before either role has been measured
# once. Only ever used to decide whether to START a round, so erring high means
# declining a round that might just have fitted — the safe direction.
SEED_CRITIQUE_USD = float(_cfg.get("seed_critique_usd", 0.30))
SEED_REVISION_USD = float(_cfg.get("seed_revision_usd", 0.20))
# Applied to a measured round when projecting the next one. Rounds are not identical:
# the prompts grow as findings and responses accumulate.
ESTIMATE_HEADROOM = float(_cfg.get("estimate_headroom", 1.25))
MEMORY_LIMIT = int(_cfg.get("memory_limit", 25))
MEMORY_MAX_AGE_DAYS = int(_cfg.get("memory_max_age_days", 365))
REGENERATE_SALE_ADVISORY = bool(_cfg.get("regenerate_sale_advisory", True))
SEED_ADVISORY_USD = float(_cfg.get("seed_advisory_usd", 0.12))
# The buy case is the same shape of problem as the sale advisory, one artefact along:
# it is derived FROM the report, the critic never sees it, and a revision leaves it
# describing a thesis that no longer exists. The one difference is that it is
# conditional — only a report that ENDS on 'Watch' has one — so the reservation below
# is held for every session even though roughly half of them will not spend it. That
# is the safe direction: the alternative is discovering after the revision that the
# verdict landed on Watch and the money for its buy case was never set aside.
REGENERATE_BUY_CASE = bool(_cfg.get("regenerate_buy_case", True))
SEED_BUY_CASE_USD = float(_cfg.get("seed_buy_case_usd", 0.18))

# Sections this tool appends to a report itself. Stripped before a report re-enters
# the loop so refining a refined report does not nest one critic section inside
# another, and the critic reviews the analyst's prose rather than its own.
CRITIC_SECTION_HEADING = "## Independent Critic Review"
_ANALYST_FIRST_SECTION = "## Recent Quarter Check"
_RUN_BANNER_RE = re.compile(r"\A(?:>[^\n]*\n)+\s*", re.MULTILINE)


# --- Preparing the report for review -------------------------------------------
def strip_generated_sections(markdown: str) -> str:
    """Reduce a STORED report to the analyst's own prose.

    A stored report is the analyst's text with three deterministic things wrapped
    around it: the run-id banner, the '## Magic Formula Metrics' section, and
    (sometimes) reconciliation warnings — plus, if it has been refined before, this
    tool's own critic section. All four are regenerated below from current data, so
    feeding them back in would mean the critic reviewing the pipeline's boilerplate
    and the reviser being invited to rewrite figures it must not touch.

    Anchors on '## Recent Quarter Check', the analyst's mandated first section. If it
    is absent (a report from an older prompt version), the text is returned with only
    the banner and the trailing appended sections removed — losing a little precision
    rather than mangling an unfamiliar shape.
    """
    text = markdown or ""
    text = _RUN_BANNER_RE.sub("", text, count=1)
    for trailer in (CRITIC_SECTION_HEADING, main._RECONCILIATION_HEADING):
        text = text.split(trailer)[0]
    idx = text.find(_ANALYST_FIRST_SECTION)
    if idx > 0:
        text = text[idx:]
    return text.strip()


def _load_candidate(ticker: str) -> dict:
    """Recompute the Magic Formula figures for the ticker, for VERIFIED_FIGURES.

    Recomputed rather than recovered from the stored report. The candidate dict the
    original run used was never persisted (only its rendered prose was), and the
    reconciliation gate needs the dict, not the prose. Deterministic and free: one
    FMP call, no tokens.

    A consequence worth naming: market cap and enterprise value are LIVE, so a report
    refined days later is checked against today's price rather than the price it was
    written under. That is the right basis for a critique — the reader is deciding
    today — but it means the critic can legitimately flag a valuation figure the
    analyst got right at the time.
    """
    try:
        candidate = json.loads(compute_ticker_magic_metrics(ticker))
    except Exception as e:
        logger.warning(f"[{ticker}] Could not recompute Magic Formula metrics ({e}); "
                       f"the critic will work without verified figures.")
        return {}
    if candidate.get("error") or "ROC_Pct" not in candidate:
        logger.warning(
            f"[{ticker}] Magic Formula metrics unavailable "
            f"(reason={candidate.get('reason') or 'unknown'}). The critic and the "
            f"reviser will see this stated rather than a figures block, and the "
            f"reconciliation gate cannot run on the revised report."
        )
    return main._normalize_candidate(candidate)


def _load_source_case(ticker: str, run_id: str, agent_type: str) -> str:
    """Load BEAR_CASE / BULL_CASE from the run under review.

    Missing is survivable, not fatal: without it the critic simply cannot check the
    report's summary of that case against the original, which is one check lost out
    of many. Says so in the prompt rather than passing an empty block, so the critic
    does not read silence as 'the analyst invented this'.
    """
    try:
        data = json.loads(db_get_agent_output(ticker, agent_type, run_id))
    except Exception as e:
        logger.warning(f"[{ticker}] Could not load {agent_type}: {e}")
        return f"Not available. The {agent_type} for this run could not be loaded."
    if not data.get("found"):
        logger.warning(f"[{ticker}] No {agent_type} stored for run {run_id[:8]}; the "
                       f"critic cannot check the report's summary of it.")
        return (f"Not available. The {agent_type} for this run is not on record, so "
                f"you cannot check the report's summary of it against the original. "
                f"Do not treat its absence as evidence that the summary is wrong.")
    return data.get("raw_content") or ""


# --- Spend control --------------------------------------------------------------
class _Estimator:
    """Running per-role cost estimate for the NEXT round.

    Seeded from config, then replaced by what each role actually cost, scaled by
    `ESTIMATE_HEADROOM`. Kept per-role because the two are not comparable: the critic
    makes tool calls and the reviser does not, so one measured 'round cost' would
    over-estimate a revision and under-estimate a critique.
    """

    def __init__(self):
        self.critique = SEED_CRITIQUE_USD
        self.revision = SEED_REVISION_USD
        # Never measured within a session — there is at most one advisory per
        # refinement, so there is nothing to learn from. The seed carries the
        # headroom instead (0.12 against a measured 0.072-0.095).
        self.advisory = SEED_ADVISORY_USD if REGENERATE_SALE_ADVISORY else 0.0
        # Same reasoning, and reserved unconditionally for the reason given at
        # SEED_BUY_CASE_USD: whether it will be needed is only known once the revised
        # report's verdict is read, which is after the money would have had to be set
        # aside. It is the dearer of the two — the buy-case advisor makes more tool
        # calls (forward estimates, the earnings calendar for the subject and for
        # every company it names, segments, M&A filings).
        self.buy_case = SEED_BUY_CASE_USD if REGENERATE_BUY_CASE else 0.0

    def observe(self, role: str, usd: float) -> None:
        if usd <= 0:
            return
        setattr(self, role, usd * ESTIMATE_HEADROOM)

    @property
    def full_round(self) -> float:
        """Everything a revision commits the session to: the revision, the critique
        that must follow it, and the two derived documents it will invalidate — the
        sale advisory and, when the verdict lands on Watch, the buy case.

        **The critique, always.** Shipping a revision nobody reviewed would attach the
        PREVIOUS round's critique to text that no longer says what the critique
        objects to — a reader would be shown objections that may already have been
        fixed, or worse, told nothing about text that was never checked.

        **The advisory, always too.** The moment a revision happens the existing sale
        advisory is describing a thesis that no longer exists, so re-deriving it stops
        being optional. Reserving it here rather than discovering the shortfall
        afterwards is what keeps the ceiling honest: the alternative — a second budget
        the advisory draws on — would mean the ceiling you name being quietly exceeded
        by the advisory's cost, and a ceiling that can be exceeded by design is not a
        ceiling. Reserving costs
        nothing in practice (~$0.12 against rounds of ~$0.20 on a $2.25 default) and
        only ever binds in the session that is about to need it: no revision has been
        committed to before this check, so a run that agrees first time never pays
        the reservation.

        **The buy case, whenever the feature is on.** Identical argument, with one
        wrinkle: it is only written when the revised report ends on 'Watch', and that
        is not knowable until the revision exists. Reserving conditionally is
        therefore impossible, so it is reserved unconditionally and the ceiling was
        raised to absorb it (see `refinement.max_budget_usd` in config.yaml). A
        session whose verdict lands on Buy or Avoid simply does not spend it.
        """
        return self.revision + self.critique + self.advisory + self.buy_case


def _affordable(spent: float, need: float, ceiling: float, prior_day: float) -> str:
    """Return a reason string when `need` more dollars cannot be spent, else ''."""
    if spent + need > ceiling:
        return (f"refinement ceiling ${ceiling:.2f} would be exceeded "
                f"(${spent:.2f} spent, next round needs about ${need:.2f})")
    if main.BUDGET_ENABLED:
        day_total = prior_day + spent + need
        if day_total >= main.BUDGET_PER_DAY_USD:
            return (f"the {main.BUDGET_DAY_WINDOW_HOURS}h ceiling "
                    f"${main.BUDGET_PER_DAY_USD:.2f} would be reached "
                    f"(${prior_day:.2f} spent before this session, ${spent:.2f} in it, "
                    f"next round needs about ${need:.2f})")
    return ""


# --- Report assembly ------------------------------------------------------------
def _agreed_banner(rounds: int, cost: float, findings: list) -> str:
    """The agreed case — including any minor points that were never acted on.

    A MINOR finding reaches this banner precisely BECAUSE it did not block
    agreement, which means no revision ran for it and its 'Required fix' was never
    applied. On the first live run the critic found that H&R Block's +17.4% quarterly
    net income growth was inflated by an $84.1M one-off tax settlement — correctly
    graded MINOR, since operating profit and cash flow carried the thesis without it,
    and correctly not worth another paid round. But a reader of the report still saw
    an unqualified +17.4%, with the correction visible only to someone who went
    looking in the critic's review. Listing them here costs nothing and is the
    difference between "the critic found nothing" and what actually happened.
    """
    unfixed = [f for f in findings if f["severity"] not in BLOCKING_SEVERITIES]
    lines = [
        CRITIC_SECTION_HEADING,
        "",
        f"**The independent critic agreed this report** after {rounds} round(s) of "
        f"review (${cost:.2f} of review cost).",
        "",
        "A separate agent — with its own instructions, its own news and web-search "
        "tools, and no sight of how this report was written — checked the report's "
        "claims against outside sources and against the two research cases behind it, "
        "and records no blocking or material objection to the reasoning or the verdict.",
        "",
    ]
    if unfixed:
        n = len(unfixed)
        lines += [
            f"**It did record {n} minor point{'' if n == 1 else 's'}.** None was "
            f"serious enough to withhold agreement, so the report above was **not "
            f"revised for {'it' if n == 1 else 'them'}** — read {'it' if n == 1 else 'them'} "
            f"alongside the report rather than as {'a correction' if n == 1 else 'corrections'} "
            f"already applied:",
            "",
        ]
        for f in unfixed:
            summary = finding_summary(f)
            lines.append(f"- **{f['title']}**" + (f" — {summary}" if summary else ""))
        lines.append("")
    lines += [
        "That is not a promise the call is right. Two agents agreeing means neither "
        "could find a fault in the argument; the business can still disappoint, and "
        "this remains one screened candidate rather than a standalone recommendation.",
        "",
        "",
    ]
    return "\n".join(lines)


def _not_agreed_banner(rounds: int, cost: float, findings: list, stop_reason: str) -> str:
    blocking = sum(1 for f in findings if f["severity"] == "BLOCKING")
    material = sum(1 for f in findings if f["severity"] == "MATERIAL")
    return (
        f"{CRITIC_SECTION_HEADING}\n\n"
        f"**The independent critic has NOT agreed this report.** After {rounds} "
        f"round(s) of review (${cost:.2f}), {blocking} blocking and {material} "
        f"material objection(s) were still standing when the review stopped: "
        f"{stop_reason}.\n\n"
        f"Treat the verdict above as unconfirmed. The critic's final review is "
        f"reproduced below in full — read it before acting, because the objections "
        f"in it are about the reasoning that produced that verdict, not about "
        f"presentation.\n\n"
    )


_HEADING_RE = re.compile(r"^(#{1,4})(?=\s)", re.MULTILINE)


# --- The sale advisory ----------------------------------------------------------
# The advisory is an OUTPUT of the report, not an input to it: the sale advisor reads
# the finished report and names the events that would break its thesis. The critic
# never sees it. So a review that CHANGES the report silently leaves the advisory
# describing a thesis that no longer exists — and because every sell trigger must be
# anchored to VERIFIED_FIGURES with the current value quoted beside it, a figure the
# critic corrected can leave a threshold calibrated against a number the pipeline
# itself now says was wrong.
_ADVISORY_NOTE = {
    "carried": (
        "> **Carried over from run `{src}`, unchanged.** The independent critic "
        "review made no revision to the report this advisory was derived from, so it "
        "applies to the refined report exactly as it did to the original.\n\n"
    ),
    "regenerated": (
        "> **Re-derived after independent critic review.** The review changed the "
        "report this advisory rests on, so it was regenerated against the revised "
        "text rather than carried over. The advisory itself was not critiqued.\n\n"
    ),
    "carried_stale": (
        "> **Carried over from run `{src}` — and it may be out of date.** The critic "
        "review revised the report this advisory was derived from, but there was not "
        "enough budget left to re-derive it. Check its thresholds against the refined "
        "report above before acting on them.\n\n"
    ),
}


def run_sale_advisor(ticker: str, company_name: str, report_body: str,
                     verified_figures: str, quarterly_data: str) -> tuple:
    """Run Phase C's sale advisor over a report body. Returns (text, usage).

    Shared with `sale_advisory.py`, the standalone regeneration command — the two
    callers decide *whether* to generate an advisory for very different reasons, but
    *how* to generate one must stay identical. Two copies of this would drift the
    moment `sale_advisor_agent` gains a templated key, and the symptom would be one
    entry point silently producing a worse advisory than the other.

    `report_body` must be the analyst's own prose (see `strip_generated_sections`),
    matching what the pipeline's own sale advisor receives in session state — not the
    assembled report with the deterministic Magic Formula section prepended.
    """
    return run_agent(
        main.sale_advisor_agent,
        {
            "ticker": ticker,
            "company_name": company_name,
            "verified_figures": verified_figures,
            "quarterly_data": quarterly_data,
            "final_report": report_body,
        },
        f"Write the sale advisory for {company_name} ({ticker}).",
    )


def _refresh_sale_advisory(ticker: str, company_name: str, src_run: str,
                           refine_run_id: str, report_body: str, verified_figures: str,
                           quarterly_data: str, revised: bool, usage: dict,
                           spent: float, ceiling: float, prior_day: float) -> tuple:
    """Give the refinement run its own SALE_CASE. Returns (text, origin).

    Two outcomes, because the cost is only worth paying in one of them:

    - **No revision ran** (the critic agreed first time): the analyst's prose is
      byte-identical to what the advisory was built from, so it is exactly as valid
      as before. Carried forward unchanged, for $0.
    - **A revision ran**: re-derived against the revised report.

    Carrying it forward is not just tidiness. `_with_run_header` stamps the
    refinement's run_id on the refined report and tells the reader that is the id to
    save against their lot — and `--sell-check TICKER --run <that id>` used to fail
    outright, because no SALE_CASE existed under the refinement run.
    """
    if not REGENERATE_SALE_ADVISORY:
        logger.info(f"[{ticker}] Sale advisory left untouched "
                    f"(refinement.regenerate_sale_advisory is false).")
        return "", "none"

    try:
        prior = json.loads(db_get_agent_output(ticker, "SALE_CASE", src_run))
    except Exception as e:
        logger.warning(f"[{ticker}] Could not load the reviewed run's SALE_CASE: {e}")
        prior = {"found": False}
    prior_text = prior.get("raw_content") or "" if prior.get("found") else ""

    origin = None
    text = ""
    if not revised:
        if not prior_text:
            logger.info(f"[{ticker}] The reviewed run stored no SALE_CASE, and the "
                        f"report was not revised, so there is nothing to carry forward.")
            return "", "none"
        origin, text = "carried", prior_text
    else:
        blocked = _affordable(spent, SEED_ADVISORY_USD, ceiling, prior_day)
        if blocked:
            # Defensive, and expected never to fire: `_Estimator.full_round` reserves
            # this cost before committing to the revision that made the advisory
            # stale, so by construction the money is already set aside. Reaching here
            # means the advisory cost materially more than its seed, or the rolling
            # daily ceiling moved underneath the session — both real, neither
            # something to paper over. Shipping the old advisory silently would be
            # the worst outcome available: it is anchored to figures the revision may
            # have corrected. Carry it with a visible staleness warning instead.
            logger.warning(
                f"[{ticker}] The report was revised but the sale advisory cannot be "
                f"re-derived: {blocked}. Carrying the previous one forward with a "
                f"staleness warning. (The reservation in _Estimator.full_round should "
                f"normally prevent this — worth investigating if it recurs.)"
            )
            if not prior_text:
                return "", "none"
            origin, text = "carried_stale", prior_text
        else:
            logger.info(f"[{ticker}] Report was revised — re-deriving the sale "
                        f"advisory against the refined text...")
            try:
                text, adv_usage = run_sale_advisor(
                    ticker, company_name, report_body, verified_figures, quarterly_data
                )
            except Exception as e:
                logger.error(f"[{ticker}] Sale advisory regeneration failed: {e}. "
                             f"Carrying the previous one forward with a warning.")
                if not prior_text:
                    return "", "none"
                origin, text = "carried_stale", prior_text
            else:
                cost = main._log_usage(f"{ticker} sale advisor (post-review)", adv_usage)
                main._merge_usage(usage, adv_usage)
                logger.info(f"[{ticker}] Sale advisory re-derived (${cost:.4f}).")
                if not text.strip():
                    logger.warning(f"[{ticker}] Sale advisor produced no output; "
                                   f"carrying the previous advisory forward.")
                    if not prior_text:
                        return "", "none"
                    origin, text = "carried_stale", prior_text
                else:
                    origin = "regenerated"

    # Strip any banner/note the carried text already carries, so notes don't stack up
    # across successive refinements of the same ticker.
    body = _RUN_BANNER_RE.sub("", text, count=1).lstrip()
    for note in _ADVISORY_NOTE.values():
        marker = note.split("**")[1]
        if body.startswith("> **" + marker):
            body = body.split("\n\n", 1)[-1]
    stamped = _ADVISORY_NOTE[origin].format(src=src_run) + body
    return main._with_run_header(stamped, refine_run_id, ticker), origin


# --- The buy case ---------------------------------------------------------------
# Everything said above about the sale advisory applies here, plus one thing that does
# not apply there: this document exists ONLY for a 'Watch', so a review can create the
# need for one where there was none (Buy -> Watch), and can destroy the need for one
# that exists (Watch -> Buy or Avoid). Those two transitions are the whole reason this
# cannot be a copy of `_refresh_sale_advisory` with the nouns changed.
_BUY_CASE_NOTE = {
    "carried": (
        "> **Carried over from run `{src}`, unchanged.** The independent critic review "
        "made no revision to the report this buy case was derived from, so it applies "
        "to the refined report exactly as it did to the original. Its price range is "
        "still anchored to the price on the day it was written — check that before "
        "acting on it.\n\n"
    ),
    "regenerated": (
        "> **Re-derived after independent critic review.** The review changed the "
        "report this buy case rests on, so its triggers were rewritten against the "
        "revised text and re-anchored to the current price. The buy case itself was "
        "not critiqued.\n\n"
    ),
    "created": (
        "> **Written after independent critic review.** The reviewed run had no buy "
        "case — either the review moved the verdict to 'Watch', or none was written "
        "at the time — so this one was derived from the refined report.\n\n"
    ),
    "carried_stale": (
        "> **Carried over from run `{src}` — and it may be out of date.** The critic "
        "review revised the report this buy case was derived from, but there was not "
        "enough budget left to re-derive it. Its triggers, and especially its price "
        "range, were written against the report as it stood BEFORE the review. Check "
        "them against the refined report above before acting on them.\n\n"
    ),
}


def _refresh_buy_case(ticker: str, company_name: str, src_run: str, refine_run_id: str,
                      report_body: str, verified_figures: str, quarterly_data: str,
                      candidate: dict, verdict: str, revised: bool, usage: dict,
                      spent: float, ceiling: float, prior_day: float,
                      price: dict = None) -> str:
    """Give the refinement run its buy case, if the refined report has earned one.

    Returns the origin ('regenerated', 'created', 'carried', 'carried_stale', 'none'),
    having already stored whatever it decided on. Storing here rather than returning
    text for the caller to store keeps the four outcomes' bookkeeping in one place —
    two of them write a freshly generated document through `buy_case_agent`, and two
    write a carried one with a warning label.

    The decision table, verdict first:

    | Refined verdict | Reviewed run had a buy case | What happens |
    | :--- | :--- | :--- |
    | not Watch | either | nothing is written; if one existed it is left behind with its run, where it still correctly describes that report |
    | Watch | no | one is WRITTEN — the review either moved the verdict onto Watch or repaired a gap |
    | Watch | yes, and a revision ran | re-derived against the revised text |
    | Watch | yes, no revision ran | carried forward unchanged, for $0 |

    Carrying forward is not tidiness. `_with_run_header` stamps the refinement's
    run_id on the refined report and tells the reader that is the id to record; a
    refinement that left no BUY_CASE under its own id would make
    `--buy-check TICKER --run <that id>` fail outright.
    """
    if not REGENERATE_BUY_CASE:
        logger.info(f"[{ticker}] Buy case left untouched "
                    f"(refinement.regenerate_buy_case is false).")
        return "none"

    try:
        prior = json.loads(db_get_agent_output(ticker, buy_case_agent.BUY_CASE_TYPE, src_run))
    except Exception as e:
        logger.warning(f"[{ticker}] Could not load the reviewed run's buy case: {e}")
        prior = {"found": False}
    prior_text = prior.get("raw_content") or "" if prior.get("found") else ""

    if not buy_case_agent.is_watch(verdict):
        # Not an omission, and worth saying out loud: a reader who saw a buy case on
        # the reviewed run and none on the refinement should be told the review is
        # the reason, not a failure.
        if prior_text:
            logger.info(
                f"[{ticker}] The review moved the verdict to {verdict}, so this run "
                f"gets no buy case. The one on run {src_run[:8]} still describes that "
                f"report and is left where it is; it no longer describes this one."
            )
        else:
            logger.info(f"[{ticker}] Verdict is {verdict}, not Watch — no buy case.")
        return "none"

    # From here the refined report is a Watch, so it should end this session with a
    # buy case under the refinement's run id one way or another.
    if not revised and prior_text:
        origin, text = "carried", prior_text
    else:
        blocked = _affordable(spent, SEED_BUY_CASE_USD, ceiling, prior_day)
        if blocked:
            # Defensive, and expected never to fire: `_Estimator.full_round` reserves
            # this before committing to the revision. Reaching here means the buy case
            # costs materially more than its seed, or the rolling daily ceiling moved
            # underneath the session. Shipping the old one silently would be the worst
            # outcome available — its price range predates the revision — so it is
            # carried with a visible warning, or omitted if there is nothing to carry.
            logger.warning(
                f"[{ticker}] The refined report is a Watch but its buy case cannot be "
                f"derived: {blocked}. (The reservation in _Estimator.full_round should "
                f"normally prevent this — worth investigating if it recurs.)"
            )
            if not prior_text:
                logger.warning(f"[{ticker}] Nothing to carry forward either, so this "
                               f"run has no buy case. Repair it with "
                               f"`python buy_case.py {ticker} --run {refine_run_id}`.")
                return "none"
            origin, text = "carried_stale", prior_text
        else:
            origin = "regenerated" if prior_text else "created"
            if origin == "created":
                logger.info(f"[{ticker}] The refined report is a Watch and the "
                            f"reviewed run had no buy case — writing one.")
            stored = buy_case_agent.write_buy_case(
                refine_run_id, ticker, company_name, report_body, verified_figures,
                quarterly_data, candidate, usage,
                origin_note=_BUY_CASE_NOTE[origin],
                metadata={"origin": f"refinement_{origin}", "source_run_id": src_run,
                          "report_revised": revised, "verdict": verdict},
                price_data=buy_case_agent.price_data_block(ticker, price),
            )
            if stored:
                return origin
            # The generator produced nothing. Fall back to the carried text rather
            # than leaving the run with no buy case at all.
            if not prior_text:
                return "none"
            origin, text = "carried_stale", prior_text

    # The carried paths. Strip any banner or note the text already has, so notes do
    # not stack up across successive refinements of the same ticker.
    body = _RUN_BANNER_RE.sub("", text, count=1).lstrip()
    for note in _BUY_CASE_NOTE.values():
        marker = note.split("**")[1]
        if body.startswith("> **" + marker):
            body = body.split("\n\n", 1)[-1]
    stamped = main._with_run_header(
        _BUY_CASE_NOTE[origin].format(src=src_run) + body, refine_run_id, ticker)
    main._check_db(
        db_store_agent_output(
            refine_run_id, ticker, buy_case_agent.BUY_CASE_TYPE, stamped,
            json.dumps({"ticker": ticker, "origin": f"refinement_{origin}",
                        "source_run_id": src_run, "report_revised": revised,
                        "verdict": verdict}),
        ),
        f"{ticker} buy case ({origin})",
    )
    with open(os.path.join("reports", f"{ticker}_Buy_Case.md"), "w", encoding="utf-8") as f:
        f.write(stamped)
    return origin


def _demote_headings(markdown: str, levels: int = 2) -> str:
    """Push every heading down `levels` so an inlined document nests correctly.

    The critic writes a standalone review with '## Findings' at the top level. Pasted
    unchanged under a '###' container inside the report, those headings outrank their
    own container: a reader's outline shows the critic's '## Findings' as a top-level
    section of the REPORT, sitting between the analyst's sections, which reads as
    though the analyst wrote it.
    """
    return _HEADING_RE.sub(lambda m: "#" * min(len(m.group(1)) + levels, 6), markdown or "")


def _assemble(candidate: dict, report_body: str, recon_findings: list,
              critic_section: str, final_review: str, agreed: bool,
              price: dict = None, ticker: str = "") -> str:
    """Deterministic sections + the analyst's prose + the critic's standing.

    The price section is re-rendered from a quote taken at REVIEW time, not copied
    from the report under review. That is the same choice `_load_candidate` makes for
    the figures and for the same reason: a refined report is a document about today,
    and the price at the top of it should be the one the critic's objections were
    weighed against — not the one from the run being reviewed, which may be days old.
    """
    parts = [
        main._format_price_section(price or {}, ticker),
        "",
        main._format_magic_formula_section(candidate),
        "",
        report_body,
        main._format_reconciliation_section(recon_findings),
        "",
        critic_section,
    ]
    if not agreed and final_review:
        parts += ["### The critic's final review, in full", "",
                  _demote_headings(final_review), ""]
    return "\n".join(p for p in parts if p is not None)


# --- The loop -------------------------------------------------------------------
def run_refinement_loop(ticker: str, company_name: str = None, source_run_id: str = None,
                        max_budget_usd: float = None, max_rounds: int = None) -> None:
    """Review and revise one ticker's existing final report until agreement or budget.

    `source_run_id` names the report to refine; omitted, the ticker's most recent
    report is used. The refinement itself always gets its OWN run id — including for
    an ad-hoc invocation — so its cost lands on its own `pipeline_runs` row instead of
    silently inflating the completed run it is reviewing, and so its output is
    traceable to the review that produced it rather than overwriting the original.
    """
    ticker = ticker.strip().upper()
    company_name = company_name or ticker
    ceiling = float(max_budget_usd if max_budget_usd is not None else MAX_BUDGET_USD)
    rounds_allowed = int(max_rounds if max_rounds is not None else MAX_ROUNDS)

    # 1. The report under review.
    try:
        source = json.loads(db_get_final_report(ticker, source_run_id or ""))
    except Exception as e:
        logger.error(f"[{ticker}] Could not load a report to refine: {e}")
        return
    if not source.get("found"):
        where = f"under run {source_run_id}" if source_run_id else "on record"
        logger.error(
            f"[{ticker}] No final report {where}. Refinement reviews an existing "
            f"report — run `python main.py {ticker}` first, then refine it."
        )
        return

    src_run = source["run_id"]
    refine_run_id = str(uuid.uuid4())
    if CRITIC_SECTION_HEADING in (source["markdown_report"] or ""):
        # Supported, and sometimes what you want — a second session picks up where
        # the budget cut the first one off, and the critic memory keeps it from
        # re-running settled ground. Said out loud because with no --run the loop
        # takes the LATEST report, which after one refinement is the refined one, so
        # this can happen without the operator intending it.
        logger.info(
            f"[{ticker}] The report being reviewed is itself a refinement (run "
            f"{src_run[:8]}). Its critic section is stripped before review; pass "
            f"--run to review the original pipeline report instead."
        )
    logger.info(
        f"Starting Refinement Run: {refine_run_id} for {ticker} ({company_name}). "
        f"Reviewing the report from run {src_run} "
        f"(verdict {source.get('verdict')}, {source.get('age_hours')}h old). "
        f"Ceiling ${ceiling:.2f}, at most {rounds_allowed} round(s)."
    )

    # 2. Everything the two agents read, gathered without an LLM in the loop.
    candidate = _load_candidate(ticker)
    quarterly_data = fmp_quarterly_trends(ticker)
    # One quote for the whole session: the critic and the reviser argue against the
    # same price the refined report will print at the top, and the buy case (if the
    # verdict lands on Watch) sets its triggers against that one too.
    price = main._price_snapshot(ticker)
    verified_figures = main._format_verified_figures(candidate, price)
    screen_context = (
        main._format_screen_context(candidate) if candidate.get("ROC_Pct")
        else ("Magic Formula ROC / Earnings Yield could not be recomputed for this "
              "ticker at review time, so no value/quality signal is available to "
              "check the report's use of one against.")
    )
    # How old the report is, in the agents' own words. Both of them are shown figures
    # recomputed TODAY next to prose written days ago, and without this they cannot
    # tell a stale price from a wrong one — the critic raises "the market cap is not
    # $164B" as a factual error and the reviser dutifully edits a number that was
    # correct when written. See the market-derived-figure rules in critic_agent.py.
    _age = source.get("age_hours")
    report_vintage = (
        f"The report under review was written on {(source.get('created_at') or '')[:16]}"
        f"{f' — about {_age:.0f} hours ago' if isinstance(_age, (int, float)) else ''}. "
        f"The VERIFIED_FIGURES, the quarterly data and any price shown to you were "
        f"computed TODAY ({datetime.now().strftime('%Y-%m-%d')}). Balance-sheet figures "
        f"will normally be identical across that gap; market prices and everything "
        f"derived from them will not be."
    )
    bear_data = _load_source_case(ticker, src_run, "BEAR_CASE")
    bull_data = _load_source_case(ticker, src_run, "BULL_CASE")

    # Long-term memory: everything the critic has ever found about this company,
    # including sessions against other runs. Loaded once — nothing outside this loop
    # writes to it while the loop is running.
    try:
        memory_rows = json.loads(db_get_critic_memory(ticker, MEMORY_LIMIT, MEMORY_MAX_AGE_DAYS))
    except Exception as e:
        logger.warning(f"[{ticker}] Could not load critic memory ({e}); reviewing "
                       f"without it. Findings settled in past sessions may be raised again.")
        memory_rows = []
    if memory_rows:
        logger.info(f"[{ticker}] Loaded {len(memory_rows)} prior critic finding(s) "
                    f"from long-term memory.")
    # This session's own findings accumulate in front of the stored ones and are
    # re-rendered after every round. Without that, round 2's critic would be shown
    # the analyst's REPLY to round 1 without the findings it answers — it runs with
    # `include_contents='none'` and has no memory of its own turn, so it would be
    # reading "1. FIXED — removed the rumoured partnership" with no idea what was
    # rumoured, and could neither confirm the fix nor fill in Prior Findings Status.
    session_rows = []

    def _corrections() -> str:
        return format_past_corrections(session_rows + memory_rows)

    past_corrections = _corrections()

    # 3. Parent row, so the refinement's cost is recorded like any other run.
    # `refines_run_id` is what makes this a refinement rather than a one-off
    # single-ticker analysis, both to a reader of the table and to the web UI, which
    # joins on it to pair the two runs and to borrow the bear/bull/sale cases this
    # run never re-ran.
    main._check_db(db_create_pipeline_run(refine_run_id, [ticker], src_run),
                   "create refinement run")

    usage = main._new_usage()
    prior_day_usd = main._prior_day_spend() if main.BUDGET_ENABLED else 0.0
    est = _Estimator()

    blocked = _affordable(0.0, est.critique, ceiling, prior_day_usd)
    if blocked:
        logger.error(
            f"[{ticker}] Cannot start: {blocked}. Raise --max-budget (a first review "
            f"round costs roughly ${est.critique:.2f}) or wait for the rolling window."
        )
        main._finalize_run(refine_run_id, usage, "Refinement Run", status="BUDGET_EXCEEDED")
        return

    report_body = strip_generated_sections(source["markdown_report"])
    analyst_response = ""
    final_review = ""
    final_findings = []
    agreed = False
    revised = False   # did the analyst ever actually rewrite the report?
    stop_reason = f"the {rounds_allowed}-round limit was reached"
    rounds_done = 0

    for rnd in range(1, rounds_allowed + 1):
        state = {
            "ticker": ticker,
            "company_name": company_name,
            "report_vintage": report_vintage,
            "screen_context": screen_context,
            "verified_figures": verified_figures,
            "quarterly_data": quarterly_data,
            "bear_data": bear_data,
            "bull_data": bull_data,
            "past_corrections": past_corrections,
            "analyst_response": analyst_response or "No prior response — this is the first review round.",
            "report_under_review": report_body,
        }

        # --- Critique -----------------------------------------------------------
        logger.info(f"[{ticker}] Round {rnd}/{rounds_allowed}: independent critic reviewing...")
        try:
            review, critique_usage = run_agent(
                critic, state,
                f"Review the research report for {company_name} ({ticker}).",
            )
        except Exception as e:
            logger.error(f"[{ticker}] Critic failed in round {rnd}: {e}")
            stop_reason = f"the critic failed in round {rnd}"
            break
        critique_cost = main._log_usage(f"{ticker} critic r{rnd}", critique_usage)
        main._merge_usage(usage, critique_usage)
        est.observe("critique", critique_cost)
        rounds_done = rnd

        if not review.strip():
            logger.error(f"[{ticker}] Critic produced no output in round {rnd}; stopping.")
            stop_reason = f"the critic produced no output in round {rnd}"
            break

        findings = parse_findings(review)
        verdict, note = extract_critic_verdict(review, findings)
        if note:
            logger.warning(f"[{ticker}] Critic verdict adjusted: {note}.")
        unresolved = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]
        logger.info(
            f"[{ticker}] Round {rnd} critic verdict: {verdict} "
            f"({len(findings)} finding(s), {len(unresolved)} blocking/material). "
            f"Round cost ${critique_cost:.4f}."
        )
        for f in findings:
            logger.info(f"[{ticker}]   {f['severity']}: {f['title']}")

        final_review = review
        final_findings = findings
        main._check_db(
            db_store_agent_output(
                refine_run_id, ticker, "CRITIC_REVIEW",
                main._with_run_header(review, refine_run_id, ticker),
                json.dumps({"ticker": ticker, "iteration": rnd, "verdict": verdict,
                            "source_run_id": src_run}),
            ),
            f"{ticker} critic review r{rnd}",
        )
        main._check_db(
            db_store_critic_findings(ticker, src_run, refine_run_id, rnd, json.dumps(findings)),
            f"{ticker} critic memory r{rnd}",
        )
        # Same shape db_get_critic_memory returns, so one renderer serves both.
        now = datetime.now(timezone.utc).isoformat()
        this_round = [{
            "created_at": now, "iteration": rnd, "severity": f["severity"],
            "finding_type": f["type"], "title": f["title"], "finding": f["finding"],
            "analyst_response": None, "status": "OPEN",
        } for f in findings]
        session_rows = this_round + session_rows
        past_corrections = _corrections()

        if verdict == "AGREE":
            agreed = True
            stop_reason = "the critic agreed"
            break

        # --- Can another full round be paid for? --------------------------------
        if rnd == rounds_allowed:
            stop_reason = f"the {rounds_allowed}-round limit was reached"
            logger.warning(f"[{ticker}] {stop_reason}; the critic's objections stand.")
            break
        spent = main._total_cost(usage)
        blocked = _affordable(spent, est.full_round, ceiling, prior_day_usd)
        if blocked:
            stop_reason = blocked
            logger.warning(
                f"[{ticker}] Stopping before the revision: {blocked}. A revision is "
                f"only started when the review that must follow it is affordable too, "
                f"so the report a reader sees has always been checked as it stands."
            )
            break

        # --- Revision -----------------------------------------------------------
        logger.info(f"[{ticker}] Round {rnd}: analyst revising against "
                    f"{len(unresolved)} blocking/material finding(s)...")
        revise_state = dict(state)
        revise_state["critic_review"] = review
        try:
            raw, revise_usage = run_agent(
                reviser_agent, revise_state,
                f"Revise the report for {company_name} ({ticker}) against the critic's findings.",
            )
        except Exception as e:
            logger.error(f"[{ticker}] Reviser failed in round {rnd}: {e}")
            stop_reason = f"the analyst failed to revise in round {rnd}"
            break
        revise_cost = main._log_usage(f"{ticker} reviser r{rnd}", revise_usage)
        main._merge_usage(usage, revise_usage)
        est.observe("revision", revise_cost)

        revised_text, analyst_response = split_revision(raw)
        if not revised_text.strip():
            logger.error(f"[{ticker}] Reviser produced no report in round {rnd}; "
                         f"keeping the previous version.")
            stop_reason = f"the analyst produced no revised report in round {rnd}"
            break
        if not analyst_response:
            # Recoverable but expensive: without the reply the critic cannot tell a
            # rejected finding from an ignored one and will raise it again.
            logger.warning(
                f"[{ticker}] Reviser omitted the '{critic_agent.RESPONSE_MARKER}' "
                f"trailer in round {rnd}. The next round will not see which findings "
                f"were rebutted and may repeat them."
            )
        else:
            main._check_db(
                db_record_analyst_response(refine_run_id, ticker, rnd, analyst_response),
                f"{ticker} analyst response r{rnd}",
            )
            for row in this_round:
                row["analyst_response"] = analyst_response
            past_corrections = _corrections()
        report_body = strip_generated_sections(revised_text)
        revised = True
        logger.info(f"[{ticker}] Round {rnd} revision complete (${revise_cost:.4f}). "
                    f"Verdict now: {main._extract_verdict(report_body)}.")

    # 4. Assemble and persist whatever the loop ended on.
    # Drained here, before the cost is quoted in the report's own banner, so the
    # figure a reader sees includes the embeddings the stored critiques already
    # billed. Drained again at the end for the final report's own vector.
    main._collect_embedding_usage(usage)
    # Quoted in the report's own banner, so it must be the cost of the REVIEW —
    # captured before the sale advisory below, which is a separate piece of work and
    # would otherwise inflate the figure a reader is told the review cost.
    # `_finalize_run` still records the true session total.
    review_cost = main._total_cost(usage)
    if not report_body.strip():
        logger.error(f"[{ticker}] No report survived the refinement; nothing stored.")
        main._finalize_run(refine_run_id, usage, "Refinement Run", status="FAILED")
        return

    # The verdict of the report as it now stands. Read before the derived documents
    # below rather than after, because one of them — the buy case — exists only for a
    # 'Watch' and a review is entirely capable of moving the verdict onto or off it.
    verdict = main._extract_verdict(report_body)

    # 4a. Give this run its own sale advisory — re-derived if the report changed,
    # carried forward if it did not. See _refresh_sale_advisory for why both cases
    # matter and why leaving it absent broke `--sell-check --run`.
    sale_data, sale_origin = _refresh_sale_advisory(
        ticker, company_name, src_run, refine_run_id, report_body, verified_figures,
        quarterly_data, revised, usage, review_cost, ceiling, prior_day_usd,
    )

    # 4b. And its buy case, on the same principle and with one extra condition: only
    # a report that ENDS on 'Watch' gets one. Spend so far is re-read from `usage`
    # rather than reusing `review_cost`, so the advisory just written is counted
    # against the ceiling this check applies.
    buy_origin = _refresh_buy_case(
        ticker, company_name, src_run, refine_run_id, report_body, verified_figures,
        quarterly_data, candidate, verdict, revised, usage,
        main._total_cost(usage), ceiling, prior_day_usd, price,
    )

    recon_findings = []
    for label, body in (("Refined report", report_body), ("Critic review", final_review),
                        ("Sale advisory", sale_data)):
        recon_findings.extend(main._reconcile_agent_figures(body, candidate, label))
    if recon_findings:
        for f in recon_findings:
            logger.warning(
                main.reconciliation_warning(ticker, f)
            )
    else:
        logger.info(f"[{ticker}] Reconciliation gate passed on the refined report.")

    critic_section = (
        _agreed_banner(rounds_done, review_cost, final_findings) if agreed
        else _not_agreed_banner(rounds_done, review_cost, final_findings, stop_reason)
    )
    report_stored = main._with_run_header(
        _assemble(candidate, report_body, recon_findings, critic_section, final_review,
                  agreed, price, ticker),
        refine_run_id, ticker,
    )

    # analysis_key is deliberately empty: a refined report must never be served by the
    # pipeline's duplicate-run skip in place of a fresh analysis. Its provenance is a
    # review session, not a filing, and reuse keys on filings.
    main._check_db(db_store_final_report(refine_run_id, ticker, verdict, report_stored, ""),
                   f"{ticker} refined report")
    main._check_db(
        db_store_ticker_run(refine_run_id, ticker, company_name, verdict,
                            main._present(candidate.get("Final_Rank")),
                            price.get("price"), price.get("as_of")),
        f"{ticker} ticker_run (refinement)",
    )
    main._check_db(
        db_resolve_critic_findings(refine_run_id, ticker,
                                   "RESOLVED" if agreed else "UNRESOLVED"),
        f"{ticker} settle critic findings",
    )
    if sale_data:
        main._check_db(
            db_store_agent_output(
                refine_run_id, ticker, "SALE_CASE", sale_data,
                json.dumps({"ticker": ticker, "origin": sale_origin,
                            "source_run_id": src_run, "report_revised": revised}),
            ),
            f"{ticker} sale advisory ({sale_origin})",
        )
        sale_path = os.path.join("reports", f"{ticker}_Sale_Advisory.md")
        with open(sale_path, "w", encoding="utf-8") as f:
            f.write(sale_data)

    report_path = os.path.join("reports", f"{ticker}_Refined_Report_{verdict.title()}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_stored)
    if final_review:
        review_path = os.path.join("reports", f"{ticker}_Critic_Review.md")
        with open(review_path, "w", encoding="utf-8") as f:
            f.write(main._with_run_header(final_review, refine_run_id, ticker))

    main._collect_embedding_usage(usage)
    status = "COMPLETED" if agreed else ("BUDGET_EXCEEDED" if "ceiling" in stop_reason
                                         else "NOT_AGREED")
    verdict_moved = (source.get("verdict") or "").upper() != verdict.upper()
    # The two derived documents are named in the closing line because a refinement is
    # usually run one ticker at a time and read immediately: 'buy case regenerated' is
    # the difference between `--buy-check --run <this id>` working and not.
    derived = f"Sale advisory: {sale_origin}. Buy case: {buy_origin}."
    logger.info(
        f"[{ticker}] Refinement finished after {rounds_done} round(s): "
        f"{'AGREED' if agreed else 'NOT AGREED'} ({stop_reason}). "
        f"Verdict {source.get('verdict')} -> {verdict}"
        f"{' (CHANGED by review)' if verdict_moved else ' (unchanged)'}. "
        f"{derived} Saved to {report_path}."
    )
    main._finalize_run(refine_run_id, usage, "Refinement Run", status=status)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Independent critic review of an existing final report",
        epilog=(
            "Reviews a report the pipeline has ALREADY produced, using a separate\n"
            "critic agent with its own instructions and its own research tools, and\n"
            "has the analyst revise it until the critic agrees or the budget runs out.\n"
            "\n"
            "  python refine.py CROX                          refine CROX's latest report\n"
            "  python refine.py CROX \"Crocs Inc\"               with an explicit company name\n"
            "  python refine.py CROX --run <RUN_ID>           refine a SPECIFIC run's report\n"
            "  python refine.py CROX --max-budget 3.00        raise the ceiling for this run\n"
            "  python refine.py CROX --max-rounds 2           cap the review rounds instead\n"
            "\n"
            "Deliberately NOT part of the pipeline: it costs several times what the\n"
            "report cost to produce. Run it on the names you are about to act on.\n"
            "The refinement gets its own run id and its own pipeline_runs cost row;\n"
            "the report it reviewed is left untouched."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("ticker", help="Ticker whose final report should be refined")
    parser.add_argument("company", nargs="?", help="Optional company name")
    parser.add_argument("--run", metavar="RUN_ID", default=None,
                        help="Refine a SPECIFIC run's report instead of the latest")
    parser.add_argument("--max-budget", type=float, metavar="USD", default=None,
                        help=f"Spend ceiling for this refinement (default "
                             f"${MAX_BUDGET_USD:.2f} from refinement.max_budget_usd)")
    parser.add_argument("--max-rounds", type=int, metavar="N", default=None,
                        help=f"Maximum review rounds (default {MAX_ROUNDS}); the "
                             f"budget usually binds first")
    args = parser.parse_args()

    if args.max_budget is not None and args.max_budget <= 0:
        parser.error("--max-budget must be greater than 0")
    if args.max_rounds is not None and args.max_rounds < 1:
        parser.error("--max-rounds must be at least 1")

    run_refinement_loop(args.ticker, args.company, args.run, args.max_budget, args.max_rounds)
