"""Everything read from specs/config.yaml, and the constants tuned alongside it.

One module so there is one answer to "where does this number come from?". Every
value here is either loaded from the YAML or is a tuning constant whose rationale
is recorded in the comment above it — the retry backoff, the budget ceilings, the
reuse window. The comments are the point: several of them record what a figure was
before it was changed and what that cost.

Imported by `main`, which re-exports the lot, because `refine`, `buy_case`,
`sale_advisory`, `critic_agent`, `buy_case_agent` and four test modules read them
as `main.NAME`.

This module does NOT configure logging. `main` sets up the handlers, and it does so
before importing this, so the one error below lands in the run log like any other.
Doing it the other way round would mean importing settings creates a log file,
which is not what a config module should do.
"""
import logging
import os

import yaml

logger = logging.getLogger("InvestmentAgentPipeline")


# Load Configuration
config_path = os.path.join(os.path.dirname(__file__), "specs", "config.yaml")
with open(config_path, "r") as f:
    config = yaml.safe_load(f)

top_n = config.get("agents", {}).get("screener_agent", {}).get("top_n_candidates", 30)

# Verdict ledger settings. The factor vocabulary is spliced into the analyst's
# instruction at import (not templated from session state) because it is static
# config, and because an ADK `{placeholder}` that no one seeds resolves to an
# empty string — which would silently ship a prompt telling the model to pick
# from a list that is not there.
LEDGER = config.get("verdict_ledger", {}) or {}
LEDGER_ENABLED = bool(LEDGER.get("enabled", False))
LEDGER_BENCHMARK = str(LEDGER.get("benchmark_symbol", "SPY")).strip().upper()
LEDGER_FACTOR_TAXONOMY = [str(k).strip() for k in (LEDGER.get("factor_taxonomy") or []) if str(k).strip()]
if LEDGER_ENABLED and not LEDGER_FACTOR_TAXONOMY:
    # Loud, because the failure is silent otherwise: the analyst would be asked
    # to choose from an empty vocabulary and would invent keys, which is the one
    # outcome the controlled list exists to prevent.
    logger.error("verdict_ledger.enabled is true but factor_taxonomy is empty in "
                 "config.yaml — the analyst has no vocabulary to choose from.")

# PINNED model version, deliberately not the `gemini-flash-latest` alias.
#
# That alias is one Google moves at each new Flash release. It moved from Gemini
# 2.5 Flash to gemini-3.6-flash without any change here, and per-run cost rose
# ~3.5x silently (input $0.30 -> $1.50/1M, output $2.50 -> $7.50/1M). Pinning makes
# a model change a decision taken here, reviewed against specs/config.yaml pricing,
# rather than one made for us. Read from config so there is a single place to
# change it; the runtime still prices whatever `event.model_version` reports, so a
# mismatch between this pin and the billed model is caught by the cost accounting.
AGENT_MODEL = config.get("agents", {}).get("model", "gemini-3.6-flash")

# The critic may run on a different model from everything else — see the long note
# in specs/config.yaml for why, and for what a replacement has to support. Empty
# means "the same model as every other agent", which is the default: falling back to
# AGENT_MODEL keeps a single pin until someone deliberately splits it.
_critic_model = str(config.get("agents", {}).get("critic_model", "") or "").strip()
CRITIC_MODEL = _critic_model or AGENT_MODEL

# LLM pricing (USD per 1M tokens), keyed by the RESOLVED model version the API
# reports — see the commentary in specs/config.yaml. Pricing the alias instead of
# the resolved model is what caused per-run spend to be understated ~2.4x once
# `gemini-flash-latest` moved from Gemini 2.5 Flash to gemini-3.6-flash.
_pricing = config.get("llm_pricing", {})
MODEL_PRICES = _pricing.get("models", {}) or {}
_embed_cfg = _pricing.get("embedding", {}) or {}
EMBED_MODEL = _embed_cfg.get("model", "text-embedding-004")
EMBED_PRICE_INPUT_PER_1M = float(_embed_cfg.get("input_usd_per_1m", 0.0))

# Models seen at runtime with no configured price, and unconfirmed prices already
# warned about. Both are reported once per process rather than per call.
_UNPRICED_MODELS = set()
_UNCONFIRMED_WARNED = set()

# Web search (Tavily) pricing (USD per 1,000 requests) for per-run cost estimates.
TAVILY_PRICE_PER_1K = float(config.get("search_pricing", {}).get("tavily_usd_per_1k", 16.00))


# Gemini API returns HTTP 429 (RESOURCE_EXHAUSTED) when the per-minute request
# or token quota is exceeded — especially for gemini-2.5-pro, whose free/standard
# tier limits are low and which the analysis agent hits once per ticker with a
# large prompt. We retry with exponential backoff and throttle between calls.
MAX_AGENT_RETRIES = 5
BASE_BACKOFF_SECONDS = 20
INTER_CALL_DELAY_SECONDS = 2
INTER_AGENT_DELAY_SECONDS = 1


# --- Budget guard -------------------------------------------------------------
_budget = config.get("budget", {}) or {}
BUDGET_ENABLED = bool(_budget.get("enabled", True))
BUDGET_PER_RUN_USD = float(_budget.get("per_run_usd", 15.0))
BUDGET_PER_DAY_USD = float(_budget.get("per_day_usd", 25.0))
BUDGET_DAY_WINDOW_HOURS = int(_budget.get("day_window_hours", 24))
BUDGET_ON_EXCEED = str(_budget.get("on_exceed", "halt")).lower()


class BudgetExceeded(Exception):
    """Raised to stop a run cleanly when a spend ceiling is hit. Work already
    completed is persisted; only the remaining tickers are skipped."""


# --- Duplicate-run skip --------------------------------------------------------
_reuse = config.get("reuse", {}) or {}
REUSE_ENABLED = bool(_reuse.get("enabled", True))
REUSE_MAX_AGE_HOURS = int(_reuse.get("max_age_hours", 24))

# --- Entry gate (momentum.py) --------------------------------------------------
# The thresholds are passed to `momentum.momentum_state`, which fills anything
# missing from its own DEFAULTS.
MOMENTUM = config.get("momentum_gate", {}) or {}
MOMENTUM_ENABLED = bool(MOMENTUM.get("enabled", False))
