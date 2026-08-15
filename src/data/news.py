"""The two research feeds: ticker-tagged news, and open-web search.

Kept together because they are chosen between rather than used together — one is
for hard company-specific facts, the other for the qualitative bear case. Their
docstrings say which is which, and the agents pick on that basis.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import json
import os
import time

import requests

from mcp_app import mcp


# ==========================================
# TOOL 4a: FMP Stock News (recent factual, ticker-tagged financial news)
# ==========================================
@mcp.tool()
def fmp_stock_news(ticker: str) -> str:
    """
    Fetches recent, ticker-tagged financial news for a company from Financial
    Modeling Prep. Use this for HARD, factual, company-specific developments:
    earnings, guidance changes, product launches, M&A, downgrades/upgrades,
    lawsuits, and other concrete catalysts. Returns dated headlines with source
    and a text snippet.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return "FMP_API_KEY not configured."

    time.sleep(0.20)  # 300 calls/min Starter rate limit
    url = "https://financialmodelingprep.com/stable/news/stock"
    try:
        res = requests.get(url, params={"symbols": ticker, "limit": 15, "apikey": api_key}, timeout=20)
        data = res.json()
        if not isinstance(data, list):
            return json.dumps({"note": "No FMP news returned", "raw": data})
        articles = [
            {
                "publishedDate": a.get("publishedDate"),
                "publisher": a.get("publisher") or a.get("site"),
                "title": a.get("title"),
                "snippet": (a.get("text") or "")[:500],
                "url": a.get("url"),
            }
            for a in data
        ]
        return json.dumps(articles, indent=2)
    except Exception as e:
        return f"Error fetching FMP stock news for {ticker}: {str(e)}"


# ==========================================
# TOOL 4b: Web Search Tool (Tavily) — qualitative bear-case / risk research
# ==========================================
@mcp.tool()
def web_search_tool(query: str) -> str:
    """
    Runs an open-web search via Tavily for QUALITATIVE research: bear-case
    theses, valuation-drop risks, competitive/regulatory threats, analyst
    criticism, and short-seller arguments that go beyond factual news headlines.
    Returns ranked results (title, url, content) plus a synthesized answer.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "TAVILY_API_KEY not configured."

    time.sleep(0.5)  # gentle throttle
    try:
        res = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "query": query,
                "search_depth": "advanced",   # highest relevance (2 credits/search)
                "topic": "general",
                "time_range": "year",         # news/analysis within the last year
                "max_results": 5,
                "include_answer": True,
            },
            timeout=30,
        )
        res.raise_for_status()
        data = res.json()
        payload = {
            "answer": data.get("answer"),
            "results": [
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "content": item.get("content"),
                    "score": item.get("score"),
                }
                for item in data.get("results", [])
            ],
        }
        return json.dumps(payload, indent=2)
    except Exception as e:
        return f"Error executing web search: {str(e)}"
