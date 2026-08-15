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


EMBED_MODEL = "gemini-embedding-001"


EMBED_DIMS = 768          # must match vector(768) in sql-schema.sql


_EMBED_FAILURES = {"n": 0}


def get_embedding(text: str) -> List[float]:
    """Embed text for semantic search over stored reports.

    Previously this imported `google.generativeai` (the legacy SDK, not installed
    here) inside a bare `except: pass`, so every call fell through to a zero vector
    in complete silence. Every embedding written to the database was zeros and
    similarity search was returning noise. Two fixes: use the SDK the project
    actually depends on, and make a failure LOUD — a degraded fallback that says
    nothing is how that went unnoticed.

    `text-embedding-004` is retired (404 on the current API). `gemini-embedding-001`
    replaces it and defaults to 3072 dimensions, so output_dimensionality is pinned
    to 768 to match the schema.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.error("GOOGLE_API_KEY not set — storing a ZERO embedding. "
                     "Semantic search over this row will not work.")
        return [0.0] * EMBED_DIMS
    try:
        from google import genai
        from google.genai import types as genai_types

        payload = text[:10000]
        client = genai.Client(api_key=api_key)
        result = client.models.embed_content(
            model=EMBED_MODEL,
            contents=payload,
            config=genai_types.EmbedContentConfig(output_dimensionality=EMBED_DIMS),
        )
        values = list(result.embeddings[0].values)
        if len(values) != EMBED_DIMS:
            raise ValueError(f"expected {EMBED_DIMS} dims, got {len(values)}")
        # The embed response carries no usage metadata, so bill from the payload
        # actually sent; the caller converts characters to tokens.
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
