"""Everything the pipeline reads from the outside world.

    screener.py      the Magic Formula screen, whole-universe and single-ticker
    sec.py           SEC EDGAR 10-K text
    fundamentals.py  FMP profile, ratios, and realised quarterly trends
    news.py          ticker-tagged news, and open-web search
    forward.py       price, analyst estimates, calendar, segments, pending M&A

Importing this package registers its MCP tools as a side effect — that is what the
`@mcp.tool()` decorators do — so `mcp_server` importing it is not incidental.

Nothing here touches the database. The split between this package and
`persistence/` is the one that was doing real work inside the old 2,458-line
module: a cron script that only wants a price paid for psycopg2 and the entire
schema DDL, and a backfill that only writes rows pulled in edgartools and pandas.
"""
from .forward import (
    fmp_earnings_calendar,
    fmp_forward_estimates,
    fmp_pending_ma_filings,
    fmp_price_history,
    fmp_price_snapshot,
    fmp_revenue_segments,
)
from .fundamentals import (
    fmp_company_profile,
    fmp_metrics_extractor,
    fmp_quarterly_trends,
)
from .news import fmp_stock_news, web_search_tool
from .screener import (
    check_ticker_screen_gates,
    compute_ticker_magic_metrics,
    run_magic_formula_screener,
)
from .sec import fetch_sec_10k_data

__all__ = [
    "check_ticker_screen_gates",
    "compute_ticker_magic_metrics",
    "fetch_sec_10k_data",
    "fmp_company_profile",
    "fmp_earnings_calendar",
    "fmp_forward_estimates",
    "fmp_metrics_extractor",
    "fmp_pending_ma_filings",
    "fmp_price_history",
    "fmp_price_snapshot",
    "fmp_quarterly_trends",
    "fmp_revenue_segments",
    "fmp_stock_news",
    "run_magic_formula_screener",
    "web_search_tool",
]
