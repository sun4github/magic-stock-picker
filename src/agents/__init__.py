"""The agents, and what it takes to run one.

    definitions.py  the five LlmAgents, and the prompt fragments they share
    runtime.py      the Runner, context caching, rate-limit detection

Note for anyone reading an import: `from google.adk.agents import LlmAgent` still
reaches Google's package, not this one. Python 3 resolves absolute imports against
the full dotted path, so a local `agents` at the sys.path root does not shadow it.
"""
from .definitions import (
    APP_NAME,
    PIPELINE_AGENTS,
    PIPELINE_INCLUDE_CONTENTS,
    RECENCY_MANDATE,
    REFERENCE_DATA_BLOCKS,
    RESEARCH_SOURCE_BLOCKS,
    VERDICT_BASIS_MANDATE,
    VERIFIED_FIGURES_MANDATE,
    analyst_agent,
    bear_agent,
    bull_agent,
    sale_advisor_agent,
    sell_check_agent,
)
from .runtime import (
    CONTEXT_CACHE,
    CONTEXT_CACHE_ENABLED,
    _build_runner,
    _is_rate_limit_error,
)

__all__ = [
    "APP_NAME",
    "CONTEXT_CACHE",
    "CONTEXT_CACHE_ENABLED",
    "PIPELINE_AGENTS",
    "PIPELINE_INCLUDE_CONTENTS",
    "RECENCY_MANDATE",
    "REFERENCE_DATA_BLOCKS",
    "RESEARCH_SOURCE_BLOCKS",
    "VERDICT_BASIS_MANDATE",
    "VERIFIED_FIGURES_MANDATE",
    "_build_runner",
    "_is_rate_limit_error",
    "analyst_agent",
    "bear_agent",
    "bull_agent",
    "sale_advisor_agent",
    "sell_check_agent",
]
