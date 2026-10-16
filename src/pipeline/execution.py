"""Driving the agents: one shared session, one Runner per role, retries per role.

`_run_pipeline_async` walks PIPELINE_AGENTS in order over a single ADK session, so
each role's `output_key` is visible to the next. The sell-condition check is the
same machinery for one standalone agent.

Retries are per AGENT, not per pipeline: a 429 on the analyst re-runs the analyst,
not the bear and bull that already succeeded and were already paid for.
"""
import asyncio
import logging
import time

from google.adk.sessions import InMemorySessionService
from google.genai import types

from agents import (
    APP_NAME,
    PIPELINE_AGENTS,
    _build_runner,
    _is_rate_limit_error,
    sale_advisor_agent,
    sell_check_agent,
)
from runstamp import RUN_STAMP
from settings import (
    BASE_BACKOFF_SECONDS,
    INTER_AGENT_DELAY_SECONDS,
    INTER_CALL_DELAY_SECONDS,
    MAX_AGENT_RETRIES,
)
from usage import (
    _add_event_usage,
    _check_metered,
    _log_usage,
    _merge_usage,
    _new_usage,
)

logger = logging.getLogger("InvestmentAgentPipeline")


async def _run_pipeline_async(run_id: str, ticker: str, company_name: str,
                              sec_data: str, metrics_data: str, screen_context: str,
                              quarterly_data: str = "", verified_figures: str = "",
                              skip_sale_advisor: bool = False):
    """Run the analysis graph for one ticker. Returns (state, usage).

    The four roles are run as SEPARATE runners over ONE shared session rather than
    as a single SequentialAgent, so a 429 can be retried at the agent that failed.
    Retrying the whole graph (the previous behaviour) discarded completed bear and
    bull work and paid for it again — at ~$0.39/ticker a single mid-sequence rate
    limit cost a full extra run, and those wasted tokens were never even counted.

    Handoff still happens through session state: each agent's `output_key` writes
    into the shared session, and the next agent templates that key. That is exactly
    what SequentialAgent did internally, so behaviour is unchanged."""
    session_service = InMemorySessionService()
    session_id = f"{run_id}:{ticker}"

    # Seed per-ticker context. sec_data/metrics_data/quarterly_data come from direct
    # tool calls (100% fidelity); screen_context carries the Magic Formula bull
    # signal; verified_figures carries the deterministic balance-sheet ground truth
    # every agent is forbidden to contradict. All are read by the agents via
    # {sec_data}/{metrics_data}/{quarterly_data}/{screen_context}/{verified_figures}.
    await session_service.create_session(
        app_name=APP_NAME,
        user_id="orchestrator",
        session_id=session_id,
        state={
            "ticker": ticker,
            "company_name": company_name or ticker,
            "sec_data": sec_data,
            "metrics_data": metrics_data,
            "quarterly_data": quarterly_data,
            "verified_figures": verified_figures,
            "screen_context": screen_context,
        },
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=f"Run the skeptical decomposer analysis for {company_name} ({ticker}).")],
    )

    usage = _new_usage()
    # Phase C (the sale advisor) is the last role and nothing downstream in the graph
    # reads its output, so dropping it is a clean truncation rather than a hole in the
    # middle. It is ~1/4 of the per-ticker cost.
    agents = [a for a in PIPELINE_AGENTS if a is not sale_advisor_agent] \
        if skip_sale_advisor else PIPELINE_AGENTS
    for agent in agents:
        # Tokens are attributed per ROLE as well as per ticker. Without this split a
        # regression in one agent's prompt is invisible: the ticker total moves and
        # there is nothing to say which of the four roles moved it. It also makes the
        # cost of history replay legible, since that lands on the LATER agents only.
        agent_usage = _new_usage()
        # Per-agent retry. Tokens spent on a failed attempt ARE counted: they were
        # billed, and hiding them is how the previous accounting understated the
        # true cost of a rate-limited run.
        for attempt in range(MAX_AGENT_RETRIES):
            runner = _build_runner(agent, session_service)
            try:
                async for event in runner.run_async(
                    user_id="orchestrator", session_id=session_id, new_message=message
                ):
                    _add_event_usage(agent_usage, event)
                 # An agent turn that ends without producing its output_key is a
                # FAILURE, not a result. Observed 2026-10-15 on SITM/AXTI: the
                # analyst 'succeeded', left session state without a
                # final_report, the sale advisor then ran blind, and the run was
                # recorded COMPLETED with no report. Treated here exactly like a
                # rate limit: retry this agent, and raise when the retries are
                # spent, so the run FAILS visibly instead of drifting into
                # downstream agents with empty inputs.
                state_now = await session_service.get_session(
                    app_name=APP_NAME, user_id="orchestrator", session_id=session_id
                )
                output_key = getattr(agent, "output_key", None)
                produced = bool(state_now and state_now.state and
                                (state_now.state.get(output_key) or "").strip()) \
                    if output_key else True
                if not produced:
                    raise ValueError(
                        f"{agent.name} completed without producing '{output_key}' "
                        f"(empty output on attempt {attempt + 1}/{MAX_AGENT_RETRIES})"
                    )
                break
            except Exception as e:
                if _is_rate_limit_error(e) and attempt < MAX_AGENT_RETRIES - 1:
                    wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                    logger.warning(
                        f"[{ticker}] {agent.name} rate limited (429). Backing off {wait}s "
                        f"(attempt {attempt + 1}/{MAX_AGENT_RETRIES}). Earlier agents in "
                        f"this ticker are NOT re-run."
                    )
                    await asyncio.sleep(wait)
                    continue
                # No _merge_usage here on purpose: `analyze_ticker` discards this
                # accumulator on a pipeline failure and returns zero usage, so
                # anything merged in would be thrown away regardless. A ticker that
                # dies mid-pipeline therefore reports $0 even though earlier roles
                # were billed. Known and accepted — the path has never fired.
                logger.error(f"[{ticker}] {agent.name} failed: {e}")
                raise
        _check_metered(f"{ticker} {agent.name}", agent_usage)
        _log_usage(f"{ticker} {agent.name}", agent_usage)
        _merge_usage(usage, agent_usage)
        # Gentle throttle between agents, replacing the old per-ticker sleep.
        await asyncio.sleep(INTER_AGENT_DELAY_SECONDS)

    final = await session_service.get_session(
        app_name=APP_NAME, user_id="orchestrator", session_id=session_id
    )
    return (dict(final.state) if final else {}), usage


def run_pipeline(run_id: str, ticker: str, company_name: str, sec_data: str,
                 metrics_data: str, screen_context: str,
                 quarterly_data: str = "", verified_figures: str = "",
                 skip_sale_advisor: bool = False):
    """Synchronous wrapper around the Phase-B graph. Returns (state, usage).

    Rate-limit retries happen INSIDE `_run_pipeline_async`, per agent — there is
    deliberately no retry loop here. Wrapping this call in a second retry would
    re-run every completed role on a late 429 and re-pay for it, which is the waste
    the per-agent retry exists to remove. Tokens burned on a failed attempt are
    counted rather than silently dropped, so a rate-limited run reports what it
    actually cost.
    """
    state, usage = asyncio.run(_run_pipeline_async(
        run_id, ticker, company_name, sec_data, metrics_data, screen_context,
        quarterly_data, verified_figures, skip_sale_advisor,
    ))
    time.sleep(INTER_CALL_DELAY_SECONDS)  # gentle throttle between tickers
    return state, usage


async def _run_sell_check_async(ticker: str, company_name: str,
                                sale_conditions: str, metrics_data: str,
                                quarterly_data: str = ""):
    """Run the standalone sell_check_agent for one ticker. Returns (state, usage);
    state['sell_check'] holds the evaluation report."""
    session_service = InMemorySessionService()
    runner = _build_runner(sell_check_agent, session_service)
    session_id = f"sellcheck:{ticker}:{RUN_STAMP}"

    await session_service.create_session(
        app_name=APP_NAME,
        user_id="orchestrator",
        session_id=session_id,
        state={
            "ticker": ticker,
            "company_name": company_name or ticker,
            "sale_conditions": sale_conditions,
            "metrics_data": metrics_data,
            "quarterly_data": quarterly_data,
        },
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=f"Check whether the prior sale conditions for {company_name} ({ticker}) are now met.")],
    )

    usage = _new_usage()
    async for event in runner.run_async(
        user_id="orchestrator", session_id=session_id, new_message=message
    ):
        _add_event_usage(usage, event)

    final = await session_service.get_session(
        app_name=APP_NAME, user_id="orchestrator", session_id=session_id
    )
    return (dict(final.state) if final else {}), usage


def _run_sell_check(ticker: str, company_name: str, sale_conditions: str,
                    metrics_data: str, quarterly_data: str = ""):
    """Synchronous wrapper around the sell-condition check with 429 backoff."""
    for attempt in range(MAX_AGENT_RETRIES):
        try:
            return asyncio.run(_run_sell_check_async(
                ticker, company_name, sale_conditions, metrics_data, quarterly_data,
            ))
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < MAX_AGENT_RETRIES - 1:
                wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    f"[{ticker}] Rate limited (429). Backing off {wait}s "
                    f"(attempt {attempt + 1}/{MAX_AGENT_RETRIES})."
                )
                time.sleep(wait)
                continue
            logger.error(f"[{ticker}] Sell-condition check failed: {e}")
            raise
