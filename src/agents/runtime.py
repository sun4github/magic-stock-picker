"""Running an agent: the Runner, its cache configuration, and rate-limit detection.

Separate from definitions.py because these are decisions about EXECUTION — how a
turn is billed and retried — rather than about what any agent is told. The context
-caching commentary below is the more valuable half: it records an optimisation
that was implemented, measured, and found to make things worse.
"""
from google.adk.apps import App
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.runners import Runner

from settings import config

from .definitions import APP_NAME


# --- 2b. AGENT EXECUTION HELPER ---
# --- Context caching ----------------------------------------------------------
# Gemini bills a repeated prompt prefix at $0.15/1M instead of $1.50/1M. Two ways to
# get it: IMPLICIT (automatic prefix matching) and EXPLICIT (a pinned CachedContent).
#
# Explicit caching is DISABLED by default, on evidence. An explicit cache cannot be
# built from a system instruction alone — the API rejects it with "contents are
# required" — and all of our data lives in the agent instruction, so ADK's every
# attempt failed silently on every turn.
#
# The obvious fix, moving the data block into the first conversation message so it
# lands in `contents`, was implemented and measured on FISV. It was WORSE: cached
# share fell 32% -> 6% and cost rose $0.32 -> $0.43 per run. The long, stable
# system instruction turned out to be exactly what implicit caching had been
# matching on; relocating it into `contents` (which grows as each agent appends to
# the session) destroyed that stable prefix and bought no explicit cache either.
#
# So: leave the data in the instruction, let implicit caching do its job (~32% of
# prompt tokens), and keep this switch here for when ADK or the API supports
# caching a system instruction directly. See specs/agent_architecture.md §5B.
#
# ttl_seconds: cache storage bills per token-hour, so this covers one ticker only.
# min_tokens: below this, storage costs more than it saves.
# cache_intervals: invocations reusing one cache before it is refreshed.
CONTEXT_CACHE = ContextCacheConfig(
    ttl_seconds=int(config.get("context_cache", {}).get("ttl_seconds", 900)),
    min_tokens=int(config.get("context_cache", {}).get("min_tokens", 4096)),
    cache_intervals=int(config.get("context_cache", {}).get("cache_intervals", 10)),
)
CONTEXT_CACHE_ENABLED = bool(config.get("context_cache", {}).get("enabled", True))


def _build_runner(agent, session_service) -> Runner:
    """Runner for one agent, with context caching attached via App.

    Caching is configured on the App rather than the Runner, so each agent gets an
    App wrapper. Falls back to a plain Runner if caching is disabled in config, so
    the feature can be switched off without touching code."""
    if not CONTEXT_CACHE_ENABLED:
        return Runner(app_name=APP_NAME, agent=agent, session_service=session_service)
    app = App(name=APP_NAME, root_agent=agent, context_cache_config=CONTEXT_CACHE)
    return Runner(app=app, session_service=session_service)


def _is_rate_limit_error(err: Exception) -> bool:
    text = str(err).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "RATE" in text and "LIMIT" in text
