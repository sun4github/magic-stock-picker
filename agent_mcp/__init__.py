"""MCP tool layer over the magic-stock-picker CLI.

A separate package from the pipeline in `src/`, deliberately. Nothing in `src/`
imports anything here, and nothing here imports the pipeline - this layer reaches it
only by running the same commands a person would type. The CLI therefore stays the
single implementation of every behaviour, and works exactly as before whether this
server is running or not.

Entry point: `python -m agent_mcp.server`.
"""

__all__ = ["catalog", "jobs", "library"]
