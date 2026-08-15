"""Reading the status string a db_* tool hands back.

The db_* tools are MCP tools, and an MCP tool returns a string — it does not raise.
So a failed insert comes back as ordinary-looking text, and for a while these return
values were simply ignored: every persistence failure was completely silent. This is
the one place that reads them.

It logs to the PIPELINE logger, not to this package's "mcp_server" one. That is
deliberate and load-bearing: the pipeline logger writes to logs/run_<stamp>.log, so
these lines land in the run log beside the work that provoked them. The mcp_server
logger goes to the console only, and moving these lines there would quietly drop
every record of a persistence failure out of the file anyone reads afterwards.
"""
import logging

logger = logging.getLogger("InvestmentAgentPipeline")


def _check_db(result: str, context: str) -> None:
    """DB MCP tools return a status string instead of raising. Previously these
    return values were ignored, so failed inserts were completely silent. Log
    them so persistence failures are visible."""
    if isinstance(result, str) and result.startswith("Error"):
        logger.error(f"DB persistence failed ({context}): {result}")
    else:
        logger.info(f"DB ok ({context}): {result}")
