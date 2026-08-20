"""Embedding vectors for semantic search over stored reports, and their billing.

The failure history recorded in `get_embedding` is the reason this is its own
module rather than a helper inside a writer: every embedding in the database was
silently zeros for a period, and the fix was as much about making failure loud as
about calling the right SDK.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import os
from typing import List

import requests

from mcp_app import logger


# NOT called here. This module is imported by the CLI, every test file, both cron
# jobs and the backfill scripts, and running the schema DDL at import charged all of
# them a remote round trip they mostly did not need. `get_db_connection` now runs it
# on first use instead; see the guard there.

# Embedding calls are billed but were never counted anywhere in the cost estimate.
# Four vectors are generated per ticker (BEAR_CASE, BULL_CASE, SALE_CASE, final
# report), and a fifth (BUY_CASE) for a ticker whose verdict lands on 'Watch'. The
# orchestrator drains this counter after each ticker rather than threading a usage
# object through every db_* tool signature.
EMBEDDING_USAGE = {"chars": 0, "requests": 0}


def drain_embedding_usage() -> dict:
    """Return counts accumulated since the last drain, and reset."""
    snapshot = dict(EMBEDDING_USAGE)
    EMBEDDING_USAGE["chars"] = 0
    EMBEDDING_USAGE["requests"] = 0
    return snapshot


EMBED_MODEL = "nomic-embed-text"


EMBED_DIMS = 768          # must match vector(768) in sql-schema.sql; nomic-embed-text's
                          # native output size, so no dimensionality pin is needed


# Deliberately NOT `OLLAMA_API_BASE` — that variable is already claimed, in this
# project's .env, for pointing LiteLLM at Ollama's *cloud* service
# (`https://ollama.com`, with `OLLAMA_API_KEY`) for the critic's glm-5.2:cloud
# model (see agents/runtime.py's resolve_model). Embeddings need the local
# instance instead: no key, nothing leaves the machine. Reusing the chat
# variable was tried and silently routed embedding calls to the cloud endpoint,
# which 401s without a model pulled there — hence a separate name.
OLLAMA_EMBED_API_BASE = os.getenv("OLLAMA_EMBED_API_BASE", "http://localhost:11434")


_EMBED_FAILURES = {"n": 0}


def get_embedding(text: str) -> List[float]:
    """Embed text for semantic search over stored reports.

    Runs locally against Ollama's `nomic-embed-text` rather than a paid API: no
    key, no per-call cost, no network dependency on a third party. Previously this
    called Gemini's `embed_content`, and before that imported `google.generativeai`
    (the legacy SDK, not installed here) inside a bare `except: pass`, so every call
    fell through to a zero vector in complete silence — every embedding written to
    the database was zeros and similarity search was returning noise. That history
    is why failures here stay LOUD rather than degrading quietly.

    `truncate: true` asks Ollama to cut the input down to what the model's context
    window (2048 tokens) actually holds rather than erroring on overflow — measured
    empirically: dense numeric/ticker text can hit that limit well under 4000
    characters, so a client-side char cap alone isn't a reliable guard.
    """
    try:
        payload = text[:20000]
        response = requests.post(
            f"{OLLAMA_EMBED_API_BASE}/api/embed",
            json={"model": EMBED_MODEL, "input": payload, "truncate": True},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        values = list(result["embeddings"][0])
        if len(values) != EMBED_DIMS:
            raise ValueError(f"expected {EMBED_DIMS} dims, got {len(values)}")
        # Ollama is free and local, so this is for observability (request/volume
        # counts in the usage log), not billing — EMBED_PRICE_INPUT_PER_1M is 0.
        EMBEDDING_USAGE["chars"] += len(payload)
        EMBEDDING_USAGE["requests"] += 1
        return values
    except Exception as e:
        _EMBED_FAILURES["n"] += 1
        # Log the first few in full, then stay quiet to avoid flooding a 30-ticker run.
        if _EMBED_FAILURES["n"] <= 3:
            logger.error(
                f"Embedding failed ({type(e).__name__}: {str(e)[:200]}). Storing a "
                f"ZERO vector — this row will not be findable by semantic search."
            )
        elif _EMBED_FAILURES["n"] == 4:
            logger.error("Further embedding failures suppressed for this run.")
        return [0.0] * EMBED_DIMS
