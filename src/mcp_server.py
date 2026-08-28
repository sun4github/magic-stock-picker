"""The MCP server: every tool the agents can call, in one namespace.

Run it directly (`python mcp_server.py`) and it serves all 34 tools over MCP. Import
it and you get the same functions as plain callables, which is how the rest of this
codebase uses them — `main`, `refine`, `buy_case`, `sale_advisory` and the cron
scripts all call them directly, with no MCP round trip.

The tools themselves moved out into two packages, split by what they talk to:

    data/         the outside world — the screen, SEC, FMP, web search
    persistence/  the database — runs, reports, critic memory, the verdict ledger

This module is now the facade over both. It stays because it is the name in every
import statement and in the MCP client configuration, and because the flat
namespace is genuinely convenient at the call sites; what it no longer is, is 2,458
lines of screener, HTTP, and DDL in one file.

Importing the two packages is what REGISTERS the tools — `@mcp.tool()` runs at
import — so the imports below are load-bearing, not conveniences. The re-exports
that follow are the plain-Python surface.
"""
from mcp_app import logger, mcp  # noqa: F401  (`mcp` is the server; `mcp.run()` below)

from data import (  # noqa: F401  (re-exported: callers import these from mcp_server)
    check_ticker_screen_gates,
    compute_ticker_magic_metrics,
    fetch_sec_10k_data,
    fmp_company_profile,
    fmp_earnings_calendar,
    fmp_forward_estimates,
    fmp_metrics_extractor,
    fmp_pending_ma_filings,
    fmp_price_snapshot,
    fmp_quarterly_trends,
    fmp_revenue_segments,
    fmp_stock_news,
    run_magic_formula_screener,
    web_search_tool,
)
from persistence import (  # noqa: F401  (same)
    EMBED_DIMS,
    EMBED_MODEL,
    EMBEDDING_USAGE,
    _num,
    db_copy_inherited_outputs,
    db_copy_ticker_outputs,
    db_create_pipeline_run,
    db_finalize_pipeline_run,
    db_find_reusable_report,
    db_get_agent_output,
    db_get_buy_case,
    db_get_critic_memory,
    db_get_final_report,
    db_get_sale_case,
    db_log_run_universe,
    db_log_verdict,
    db_record_analyst_response,
    db_resolve_critic_findings,
    db_search_historical_reports,
    db_spend_since,
    db_store_agent_output,
    db_store_critic_findings,
    db_store_final_report,
    db_store_ticker_run,
    db_update_pipeline_status,
    drain_embedding_usage,
    get_db_connection,
    get_embedding,
    initialize_database,
)


if __name__ == "__main__":
    mcp.run()
