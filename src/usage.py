"""What a run costs, and the ceiling on what it may cost.

Two halves that belong together: the accounting (tokens counted per resolved model
version, priced, and summed into a per-run total) and the guard that stops a run
when that total crosses a configured ceiling.

Pricing is by RESOLVED model version, not by the alias requested — the comments
below record why, and what pricing the alias instead cost. The three cost streams
are kept apart (LLM tokens, embeddings, Tavily searches) because they are billed by
different vendors on different units, and a single blended number hides which one
grew.

Extracted verbatim from main.py, which re-exports it: `refine`, `buy_case`,
`buy_case_agent`, `sale_advisory`, `critic_agent` and two test modules call these
as `main._new_usage`, `main._log_usage`, `main._check_budget` and so on.
"""
import json
import logging

from mcp_server import (
    db_finalize_pipeline_run,
    db_spend_since,
    drain_embedding_usage,
)
from persistence import _check_db

# The budget ceilings are read as `settings.NAME` at call time — see `_check_budget`
# — so the module itself is imported, not just names out of it. Everything else here
# is a constant nothing reassigns, so a plain from-import is fine.
import settings
from settings import (
    BudgetExceeded,
    EMBED_MODEL,
    EMBED_PRICE_INPUT_PER_1M,
    MODEL_PRICES,
    TAVILY_PRICE_PER_1K,
    _UNCONFIRMED_WARNED,
    _UNPRICED_MODELS,
)

logger = logging.getLogger("InvestmentAgentPipeline")


# --- Token usage / cost accounting ---------------------------------------


def _prior_day_spend() -> float:
    """Spend already recorded in the rolling window, from the DB. Excludes the
    current run, whose row is only finalized at the end."""
    try:
        return float(json.loads(
            db_spend_since(settings.BUDGET_DAY_WINDOW_HOURS)).get("total_usd", 0.0))
    except Exception as e:
        # A guard that fails open is worse than no guard being claimed, so say so.
        logger.warning(f"Could not read recent spend for the budget guard: {e}. "
                       f"Proceeding with the per-run ceiling only.")
        return 0.0


def _check_budget(run_usage: dict, prior_day_usd: float, next_label: str) -> None:
    """Enforce the spend ceilings before starting another ticker.

    Checked between tickers rather than mid-ticker: stopping half way through a
    ticker would leave a partial analysis and still have paid for it.

    The ceilings are read off the `settings` MODULE on every call, not bound at
    import. `from settings import BUDGET_ENABLED` would copy the value into this
    module, giving the setting two homes — and then `settings.BUDGET_ENABLED = False`
    would disable nothing while appearing to, which is the one failure mode a
    guard must not have. One live read, one home."""
    if not settings.BUDGET_ENABLED:
        return
    run_cost = _total_cost(run_usage)
    day_cost = prior_day_usd + run_cost

    breach = None
    if run_cost >= settings.BUDGET_PER_RUN_USD:
        breach = (f"per-run ceiling reached: ${run_cost:.2f} of "
                  f"${settings.BUDGET_PER_RUN_USD:.2f}")
    elif day_cost >= settings.BUDGET_PER_DAY_USD:
        breach = (f"{settings.BUDGET_DAY_WINDOW_HOURS}h ceiling reached: ${day_cost:.2f} of "
                  f"${settings.BUDGET_PER_DAY_USD:.2f} (${prior_day_usd:.2f} before this run)")
    if not breach:
        return

    if settings.BUDGET_ON_EXCEED == "halt":
        raise BudgetExceeded(f"{breach}. Stopping before {next_label}. "
                             f"Raise budget.per_run_usd / budget.per_day_usd in "
                             f"specs/config.yaml to continue.")
    logger.warning(f"BUDGET: {breach}. Continuing because budget.on_exceed is "
                   f"'{settings.BUDGET_ON_EXCEED}' — set it to 'halt' to stop instead.")


def _new_usage() -> dict:
    """Usage accumulator. `by_model` splits tokens by the resolved model version so
    each is priced at its own rate; the flat input/output/total counters are kept
    for the existing DB columns and log lines. `embed_input` counts embedding
    tokens, which are billed separately and were previously not counted at all."""
    return {
        "input": 0, "output": 0, "total": 0, "requests": 0, "search_requests": 0,
        "cached_input": 0, "embed_input": 0, "embed_requests": 0,
        "by_model": {},
    }


def _model_bucket(usage: dict, model: str) -> dict:
    return usage["by_model"].setdefault(
        model or "unknown",
        {"input": 0, "cached_input": 0, "output": 0, "total": 0, "requests": 0},
    )


def _add_event_usage(usage: dict, event) -> None:
    """Accumulate token counts and web-search (Tavily) invocations from an ADK
    event. Partial (streaming) events are skipped to avoid double counting. Output
    tokens include `thoughts` tokens (billed at the output rate on thinking models).
    Note: fmp_stock_news calls are not counted here — they are FMP (Starter) calls,
    not billed web searches."""
    if getattr(event, "partial", False):
        return

    # Count Tavily web-search calls (each web_search_tool invocation = 1 request).
    content = getattr(event, "content", None)
    if content and getattr(content, "parts", None):
        for part in content.parts:
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None) == "web_search_tool":
                usage["search_requests"] += 1

    # Token usage (present only on model response events).
    um = getattr(event, "usage_metadata", None)
    if um is None:
        return
    tokens_in = um.prompt_token_count or 0
    # Thinking tokens bill at the output rate. gemini-3.6-flash spends the large
    # majority of its output budget here -- a probe showed 388 of 478 output
    # tokens were thoughts -- so dropping them would badly understate cost.
    tokens_out = (um.candidates_token_count or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
    tokens_total = um.total_token_count or 0
    # Repeated prompt prefixes bill as a separate, much cheaper SKU. prompt_token_count
    # INCLUDES these, so full-rate input is (prompt - cached). Ignoring the split
    # overstates cost -- on a real day's billing, cached was 21% of prompt tokens at
    # one tenth the rate.
    tokens_cached = getattr(um, "cached_content_token_count", 0) or 0

    usage["input"] += tokens_in
    usage["cached_input"] += tokens_cached
    usage["output"] += tokens_out
    usage["total"] += tokens_total
    usage["requests"] += 1

    # The alias configured on the agent is not what gets billed; the resolved
    # version is. Bucket by it so each model is priced at its own rate.
    bucket = _model_bucket(usage, getattr(event, "model_version", None))
    bucket["input"] += tokens_in
    bucket["cached_input"] += tokens_cached
    bucket["output"] += tokens_out
    bucket["total"] += tokens_total
    bucket["requests"] += 1


# Embedding responses carry no usage metadata, so tokens are approximated from the
# payload length. 4 chars/token is the usual rule of thumb for English prose; this
# line is a small fraction of total spend, so the approximation is not material —
# but counting it approximately beats the previous behaviour of not counting it.
_CHARS_PER_TOKEN = 4


def _collect_embedding_usage(usage: dict) -> None:
    """Fold embedding calls made since the last collection into `usage`."""
    drained = drain_embedding_usage()
    usage["embed_input"] += drained["chars"] // _CHARS_PER_TOKEN
    usage["embed_requests"] += drained["requests"]


def _merge_usage(acc: dict, other: dict) -> None:
    """Fold `other`'s usage into `acc`.

    `cached_input` MUST be merged along with the rest: `_model_cost` reads it from
    the per-model bucket to bill the cheaper cached-prefix rate, so dropping it here
    silently re-priced every cached token at the full rate once per-ticker usage was
    rolled up into the run total. The per-ticker log lines were right and the RUN
    TOTAL (and the pipeline_runs row, and the budget guard that reads it back) was
    too high by the whole cached discount."""
    for k in acc:
        if k == "by_model":
            for model, b in other.get("by_model", {}).items():
                target = _model_bucket(acc, model)
                for field in ("input", "cached_input", "output", "total", "requests"):
                    target[field] += b.get(field, 0)
        else:
            acc[k] += other.get(k, 0)


def _model_price(model: str):
    """(input_per_1m, cached_input_per_1m, output_per_1m) for a resolved model
    version, or None when the model has no configured price. Returning None rather
    than a default is deliberate: silently falling back to another model's rate is
    exactly how the original 2.4x understatement went unnoticed for so long."""
    entry = MODEL_PRICES.get(model)
    if not entry:
        if model not in _UNPRICED_MODELS:
            _UNPRICED_MODELS.add(model)
            logger.error(
                f"No price configured for model '{model}'. Its tokens are EXCLUDED "
                f"from the cost estimate, so reported spend is too low. Add it under "
                f"llm_pricing.models in specs/config.yaml."
            )
        return None
    if not entry.get("confirmed", False) and model not in _UNCONFIRMED_WARNED:
        _UNCONFIRMED_WARNED.add(model)
        logger.warning(
            f"Price for '{model}' is marked unconfirmed in specs/config.yaml "
            f"(${entry.get('input_usd_per_1m')}/1M in, ${entry.get('output_usd_per_1m')}/1M out). "
            f"Verify against the Cloud Billing SKU lines and set confirmed: true."
        )
    return (
        float(entry.get("input_usd_per_1m", 0.0)),
        float(entry.get("cached_input_usd_per_1m", 0.0)),
        float(entry.get("output_usd_per_1m", 0.0)),
    )


def _model_cost(bucket: dict, price) -> float:
    """Cost for one model's tokens. `prompt_token_count` includes cached tokens, so
    the full rate applies only to (input - cached); the remainder bills at the
    cheaper cached-input rate."""
    p_in, p_cached, p_out = price
    cached = min(bucket.get("cached_input", 0), bucket["input"])
    full = bucket["input"] - cached
    return (full / 1e6) * p_in + (cached / 1e6) * p_cached + (bucket["output"] / 1e6) * p_out


def _llm_cost(usage: dict) -> float:
    """Sum per-model cost. Models without a configured price contribute nothing and
    are logged as errors by _model_price -- the estimate is then a known lower
    bound rather than a wrong number presented as fact."""
    total = 0.0
    for model, b in usage.get("by_model", {}).items():
        price = _model_price(model)
        if price is None:
            continue
        total += _model_cost(b, price)
    return total + _embedding_cost(usage)


def _embedding_cost(usage: dict) -> float:
    """Embedding spend. Four vectors are generated per ticker (bear, bull, sale,
    final report); before this was added they were billed but never counted."""
    return (usage.get("embed_input", 0) / 1e6) * EMBED_PRICE_INPUT_PER_1M


def _search_cost(usage: dict) -> float:
    return (usage["search_requests"] / 1000.0) * TAVILY_PRICE_PER_1K


def _total_cost(usage: dict) -> float:
    """Combined LLM + embedding + web-search spend for a usage accumulator."""
    return _llm_cost(usage) + _search_cost(usage)


def _check_metered(label: str, usage: dict) -> None:
    """Complain if an agent that definitely ran reported no tokens at all.

    `_add_event_usage` returns silently when an event carries no `usage_metadata`,
    which is correct — most events (tool calls, state deltas) have none. The failure
    it cannot distinguish is a model whose responses never carry usage metadata AT
    ALL: every event is skipped, the accumulator stays at zero, and the run reports
    itself as free. That is not hypothetical. `usage_metadata` is the Gemini event
    shape, and a model reached through a different provider may not populate it.

    So the check belongs here, one level up, where "this agent ran and produced
    output" is known. An agent that completed a turn made at least one model call;
    zero means the meter is broken, not that the work was free. Loud, because a
    silent zero disables the budget guard — it can only stop a run whose cost it can
    see — and because the cost accounting being wrong by 3.5x for a while, unnoticed,
    is the reason these guards exist at all.
    """
    if usage.get("requests", 0) == 0:
        logger.error(
            f"[{label}] ran but reported ZERO model calls. Token usage is not "
            f"reaching the accounting, so this agent's cost is invisible and the "
            f"budget guard cannot see it. If this agent was recently pointed at a "
            f"non-Gemini model, its responses are probably not carrying "
            f"`usage_metadata` in the shape `_add_event_usage` reads."
        )


def _log_usage(label: str, usage: dict) -> float:
    """Log token/search usage and estimated cost. Returns combined USD cost."""
    llm = _llm_cost(usage)
    search = _search_cost(usage)
    total = llm + search
    logger.info(
        f"[{label}] Tokens: {usage['requests']} model calls | "
        f"input={usage['input']:,} output={usage['output']:,} total={usage['total']:,} "
        f"| Tavily: {usage['search_requests']} searches "
        f"| est. cost: LLM=${llm:.4f} Tavily=${search:.4f} TOTAL=${total:.4f}"
    )
    # Per-model breakdown. Printed because the agents are configured with a moving
    # alias -- seeing the resolved version in the log is what makes a repointed
    # alias (and the stale price that follows) noticeable instead of silent.
    for model, b in sorted(usage.get("by_model", {}).items()):
        price = _model_price(model)
        cost = ("$%.4f" % _model_cost(b, price)) if price else "UNPRICED"
        cached = b.get("cached_input", 0)
        cached_note = f" (of which {cached:,} cached)" if cached else ""
        logger.info(
            f"[{label}]   {model}: {b['requests']} calls "
            f"in={b['input']:,}{cached_note} out={b['output']:,} -> {cost}"
        )
    if usage.get("embed_input"):
        logger.info(
            f"[{label}]   {EMBED_MODEL}: {usage['embed_requests']} embeddings "
            f"in={usage['embed_input']:,} -> ${_embedding_cost(usage):.4f}"
        )
    return total


def _finalize_run(run_id: str, usage: dict, run_label: str, status: str = "COMPLETED") -> None:
    """Log the run's usage/cost and persist it (with terminal status) to
    pipeline_runs. `status` is BUDGET_EXCEEDED when a spend ceiling stopped the run
    early, so a short run is distinguishable from a completed one in the history."""
    total_cost = _log_usage(f"RUN {run_id} TOTAL", usage)
    _check_db(
        db_finalize_pipeline_run(
            run_id, status,
            usage["requests"], usage["input"], usage["output"], usage["total"],
            usage["search_requests"],
            round(_llm_cost(usage), 6), round(_search_cost(usage), 6), round(total_cost, 6),
        ),
        "finalize pipeline run",
    )
    logger.info(f"{run_label} {run_id} {status}. Estimated spend (LLM+Tavily): ${total_cost:.4f}")
