"""Critic/analyst refinement loop — the expensive, opt-in second opinion.

    python refine.py TICKER [Company] [--run SOURCE_RUN_ID] [--max-budget 2.00]

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

- **The stopping condition is a spend decision, not an agent decision.** This loop
  stops when it can no longer afford CRITIC + REVISION + ANOTHER CRITIQUE, which
  needs the running dollar total and an estimate of the next round — figures that
  live in `main`'s cost accounting, outside any agent's context. Getting a
  LoopAgent to stop on that means an escalation callback that reaches into the same
  accounting anyway.
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
MAX_BUDGET_USD = float(_cfg.get("max_budget_usd", 2.00))
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

    def observe(self, role: str, usd: float) -> None:
        if usd <= 0:
            return
        setattr(self, role, usd * ESTIMATE_HEADROOM)

    @property
    def full_round(self) -> float:
        """A revision plus the critique that must follow it.

        Both, always. Shipping a revision nobody reviewed would attach the PREVIOUS
        round's critique to text that no longer says what the critique objects to —
        a reader would be shown objections that may already have been fixed, or
        worse, told nothing about text that was never checked. So a revision is only
        started when the round after it is affordable too.
        """
        return self.revision + self.critique


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
              critic_section: str, final_review: str, agreed: bool) -> str:
    """Deterministic sections + the analyst's prose + the critic's standing."""
    parts = [
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
    verified_figures = main._format_verified_figures(candidate)
    screen_context = (
        main._format_screen_context(candidate) if candidate.get("ROC_Pct")
        else ("Magic Formula ROC / Earnings Yield could not be recomputed for this "
              "ticker at review time, so no value/quality signal is available to "
              "check the report's use of one against.")
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
    stop_reason = f"the {rounds_allowed}-round limit was reached"
    rounds_done = 0

    for rnd in range(1, rounds_allowed + 1):
        state = {
            "ticker": ticker,
            "company_name": company_name,
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

        revised, analyst_response = split_revision(raw)
        if not revised.strip():
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
        report_body = strip_generated_sections(revised)
        logger.info(f"[{ticker}] Round {rnd} revision complete (${revise_cost:.4f}). "
                    f"Verdict now: {main._extract_verdict(report_body)}.")

    # 4. Assemble and persist whatever the loop ended on.
    # Drained here, before the cost is quoted in the report's own banner, so the
    # figure a reader sees includes the embeddings the stored critiques already
    # billed. Drained again at the end for the final report's own vector.
    main._collect_embedding_usage(usage)
    total_cost = main._total_cost(usage)
    if not report_body.strip():
        logger.error(f"[{ticker}] No report survived the refinement; nothing stored.")
        main._finalize_run(refine_run_id, usage, "Refinement Run", status="FAILED")
        return

    recon_findings = []
    for label, body in (("Refined report", report_body), ("Critic review", final_review)):
        recon_findings.extend(main._reconcile_agent_figures(body, candidate, label))
    if recon_findings:
        for f in recon_findings:
            logger.warning(
                f"[{ticker}] RECONCILIATION: {f['source']} states {f['field']} of "
                f"{main.format_money(f['stated'])}, but the filings show "
                f"{main.format_money(f['verified'])} (off by {f['deviation_pct']}%)."
            )
    else:
        logger.info(f"[{ticker}] Reconciliation gate passed on the refined report.")

    critic_section = (
        _agreed_banner(rounds_done, total_cost, final_findings) if agreed
        else _not_agreed_banner(rounds_done, total_cost, final_findings, stop_reason)
    )
    verdict = main._extract_verdict(report_body)
    report_stored = main._with_run_header(
        _assemble(candidate, report_body, recon_findings, critic_section, final_review, agreed),
        refine_run_id, ticker,
    )

    # analysis_key is deliberately empty: a refined report must never be served by the
    # pipeline's duplicate-run skip in place of a fresh analysis. Its provenance is a
    # review session, not a filing, and reuse keys on filings.
    main._check_db(db_store_final_report(refine_run_id, ticker, verdict, report_stored, ""),
                   f"{ticker} refined report")
    main._check_db(
        db_store_ticker_run(refine_run_id, ticker, company_name, verdict,
                            main._present(candidate.get("Final_Rank"))),
        f"{ticker} ticker_run (refinement)",
    )
    main._check_db(
        db_resolve_critic_findings(refine_run_id, ticker,
                                   "RESOLVED" if agreed else "UNRESOLVED"),
        f"{ticker} settle critic findings",
    )

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
    logger.info(
        f"[{ticker}] Refinement finished after {rounds_done} round(s): "
        f"{'AGREED' if agreed else 'NOT AGREED'} ({stop_reason}). "
        f"Verdict {source.get('verdict')} -> {verdict}"
        f"{' (CHANGED by review)' if verdict_moved else ' (unchanged)'}. "
        f"Saved to {report_path}."
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
