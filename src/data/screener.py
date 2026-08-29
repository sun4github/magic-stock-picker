"""The Magic Formula screen, as MCP tools.

One tool runs the whole ranked screen; the other computes the same metrics for a
single ticker on demand. The heavy lifting is in magic_formula_starter_screener —
this is the thin layer that makes it callable and returns JSON.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import json
import os
import time

import pandas as pd

from mcp_app import logger, mcp


# ==========================================
# TOOL 1: Magic Formula Screener
# ==========================================
from magic_formula_starter_screener import (
    main as run_screener_main,
    OUTPUT_FILENAME,
    compute_company_metrics_detailed,
    attach_growth_metrics,
    fmp_get,
    FMPError,
    _universe_exclusion_reason,
    ratio_gate_reason,
    price_gate_reason,
    growth_gate_reason,
)


@mcp.tool()
def run_magic_formula_screener() -> str:
    """
    Executes the magic formula screener tool and outputs the top 30 ranked companies.
    Returns JSON array of the top 30 candidates.
    """
    logger.info("Executing Magic Formula Screener...")
    # Run the screener which writes to CSV
    run_screener_main()

    # Read the CSV
    try:
        df = pd.read_csv(OUTPUT_FILENAME)
        # Top 30
        top_30 = df.head(30).to_dict(orient="records")
        return json.dumps(top_30, indent=2)
    except Exception as e:
        logger.error(f"Error reading screener output: {e}")
        return json.dumps([])


@mcp.tool()
def compute_ticker_magic_metrics(ticker: str) -> str:
    """
    Computes Magic Formula Return on Capital (ROC) and Earnings Yield for a SINGLE
    ticker, reusing the screener's own math. Used for on-demand runs where the full
    screener is not executed, so the bull-case agent still gets the value/quality
    signal. Returns a candidate-shaped JSON object (or {"error": ...}).
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return json.dumps({"error": "FMP_API_KEY not configured"})
    try:
        # /stable/batch-quote is restricted on the Starter plan; use the
        # market-capitalization endpoint (available on Starter) for live market cap.
        # Routed through fmp_get for timeout + retry + clear errors.
        time.sleep(0.20)
        mc = fmp_get(
            "https://financialmodelingprep.com/stable/market-capitalization",
            params={"symbol": ticker, "apikey": api_key},
            context=f"{ticker} market-cap",
        )
        live_cap = mc[0].get("marketCap", 0) if isinstance(mc, list) and mc else 0
        metrics = compute_company_metrics_detailed(ticker, live_cap, api_key)
        if not metrics.get("ok"):
            # Not a failure to look up — usually a real, reportable fact about the
            # company (e.g. it is losing money, so the ratios are undefined). Pass
            # the plain-English reason through so the final report can explain the
            # gap instead of showing a bare 'Not available'.
            return json.dumps({
                "error": f"Could not compute ROC/Earnings Yield for {ticker}",
                "reason": metrics.get("reason"),
                "message": metrics.get("message"),
                "EBIT": metrics.get("EBIT"),
                "EBIT_Basis": metrics.get("EBIT_Basis"),
                "CapitalEmployed": metrics.get("CapitalEmployed"),
                "LiveMarketCap": live_cap,
            })
        # Lynch's growth figures. Costs one extra request, spent unconditionally here
        # (the screener spends it only on gate survivors) because an on-demand run is
        # asking about ONE named company and the PEG is part of the answer.
        attach_growth_metrics(ticker, metrics, api_key)

        roic = metrics.get("ROIC_InclGoodwill")
        intang_share = metrics.get("IntangiblesShareOfAssets")
        roa = metrics.get("ROA")
        roa_ni = metrics.get("ROA_NetIncome")
        pe = metrics.get("PE")
        growth = metrics.get("EPSGrowth")
        peg = metrics.get("PEG")
        return json.dumps({
            "Symbol": ticker,
            "CompanyName": metrics.get("CompanyName", ticker),
            "ROC_Pct": f"{round(metrics['ROC'] * 100, 2)}%",
            "EY_Pct": f"{round(metrics['EarningsYield'] * 100, 2)}%",
            # Greenblatt's step-by-step gate figures. Reported, never enforced here:
            # an on-demand run is asking about ONE named company, so refusing to
            # compute its ratios because the screen would have rejected it would
            # answer a question nobody asked. The report says which side of the
            # 25% / P/E-5 cuts it falls on and lets the reader weigh that.
            "ROA_Pct": f"{round(roa * 100, 2)}%" if roa is not None else None,
            "ROA_NetIncome_Pct": f"{round(roa_ni * 100, 2)}%" if roa_ni is not None else None,
            "PE_Ratio": round(pe, 2) if pe is not None else None,
            # Lynch's PEG test. Reported, never enforced here, for the same reason as
            # the two gate figures above: the report says which side of the "PEG at or
            # below 1.2, and growing at all" cuts this company falls on. Both the raw
            # ratio and the display string go out, because the batch and single-ticker
            # candidate shapes must agree (agent_architecture.md §2.I).
            "EPSGrowth": growth,
            "EPSGrowth_Pct": f"{round(growth * 100, 2)}%" if growth is not None else None,
            # What the PEG was actually divided by: the same rate unless the
            # sustainability cap bit (see MAX_GROWTH_FOR_PEG in the screener).
            "EPSGrowth_ForPEG": metrics.get("EPSGrowth_ForPEG"),
            "EPSGrowth_Capped": metrics.get("EPSGrowth_Capped"),
            "PEG": peg,
            "PEG_Ratio": round(peg, 2) if peg is not None else None,
            "EPS_Current": metrics.get("EPS_Current"),
            "EPS_Base": metrics.get("EPS_Base"),
            "EPS_Current_Date": metrics.get("EPS_Current_Date"),
            "EPS_Base_Date": metrics.get("EPS_Base_Date"),
            "EPS_Basis": metrics.get("EPS_Basis"),
            # Which window the growth was measured over ("sums" = N-year totals vs
            # the prior N years; "endpoint" = the two-point CAGR), and whether the
            # base window was a real trading period or a breakeven one.
            "EPS_Window": metrics.get("EPS_Window"),
            "EPS_Window_Years": metrics.get("EPS_Window_Years"),
            "BaseNetMargin": metrics.get("BaseNetMargin"),
            "BaseNetMargin_Pct": (
                f"{round(metrics['BaseNetMargin'] * 100, 2)}%"
                if metrics.get("BaseNetMargin") is not None else None
            ),
            "EPSGrowth_Years": metrics.get("EPSGrowth_Years"),
            # Why the growth rate is missing, when it is, so the report can explain it
            # instead of printing a bare "Not available" (see ROIC_Unavailable_Reason).
            "EPSGrowth_Unavailable_Reason": metrics.get("EPSGrowth_Unavailable_Reason"),
            "NetIncome": metrics.get("NetIncome"),
            "EBIT_Basis": metrics.get("EBIT_Basis"),
            "EBIT": metrics.get("EBIT"),
            "CapitalEmployed": metrics.get("CapitalEmployed"),
            "EnterpriseValue": metrics.get("EnterpriseValue"),
            "LiveMarketCap": live_cap,
            "Final_Rank": None,
            "MagicFormula_Score": None,
            "Composite_Score": None,
            # Authoritative balance-sheet figures + goodwill-inclusive ROIC. These
            # are what the reconciliation gate checks agent prose against, and what
            # keeps a rollup's headline ROC from being read as operating efficiency.
            "TotalDebt": metrics.get("TotalDebt"),
            "Cash": metrics.get("Cash"),
            "TotalEquity": metrics.get("TotalEquity"),
            "TotalAssets": metrics.get("TotalAssets"),
            "GoodwillAndIntangibles": metrics.get("GoodwillAndIntangibles"),
            "InvestedCapital": metrics.get("InvestedCapital"),
            "ROIC_InclGoodwill_Pct": f"{round(roic * 100, 2)}%" if roic is not None else None,
            # Why the companion is missing, so the report can say so instead of
            # printing a bare "Not available" next to a four-digit ROC.
            "ROIC_Unavailable_Reason": metrics.get("ROIC_Unavailable_Reason"),
            "IntangiblesShareOfAssets_Pct": f"{round(intang_share * 100, 1)}%" if intang_share is not None else None,
            "BalanceSheetDate": metrics.get("BalanceSheetDate"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def check_ticker_screen_gates(ticker: str) -> str:
    """
    Check whether a SINGLE ticker passes the Magic Formula screen's eligibility
    gates, without running the full universe screen. Returns a verdict of PASS or
    FAIL with the exact gate that rejected it (or None if it passes).

    This is the on-demand single-ticker counterpart to the whole-universe screen:
    it applies the SAME gates in the SAME order the screener does —
      1. universe exclusion (sector / industry / ADR / fund-ETF),
      2. Greenblatt's ROA and P/E cuts,
      3. the 52-week-high proximity gate (not within max_pct_from_52w_high of the
         high),
      4. Lynch's PEG / growth cuts.
    So a ticker that passes here is one the full screen would have kept, and a
    ticker that fails reports the precise reason (e.g. "roa_below_min").

    Use this to pre-screen a candidate before spending on a full analysis, or to
    explain why a name did not make the rankings. Returns JSON with the verdict,
    the failing gate (if any), and the underlying metrics.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return json.dumps({"error": "FMP_API_KEY not configured"})
    try:
        # 1. Universe exclusion — needs the company profile (sector/industry/country).
        time.sleep(0.20)
        prof = fmp_get(
            "https://financialmodelingprep.com/stable/profile",
            params={"symbol": ticker, "apikey": api_key},
            context=f"{ticker} profile",
        )
        row = prof[0] if isinstance(prof, list) and prof else prof
        if not isinstance(row, dict) or not row.get("symbol"):
            return json.dumps({"error": f"No profile found for {ticker}"})
        company = {
            "symbol": row.get("symbol"),
            "companyName": row.get("companyName"),
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "country": row.get("country"),
            "isEtf": row.get("isEtf"),
            "isFund": row.get("isFund"),
        }
        universe_reason = _universe_exclusion_reason(company)
        if universe_reason:
            return json.dumps({
                "Symbol": ticker,
                "CompanyName": row.get("companyName", ticker),
                "Verdict": "FAIL",
                "Gate": "universe_exclusion",
                "Reason": universe_reason,
                "Message": f"Rejected before financials: {universe_reason}.",
            })

        # 2. Live market cap (Starter plan: market-capitalization endpoint).
        time.sleep(0.20)
        mc = fmp_get(
            "https://financialmodelingprep.com/stable/market-capitalization",
            params={"symbol": ticker, "apikey": api_key},
            context=f"{ticker} market-cap",
        )
        live_cap = mc[0].get("marketCap", 0) if isinstance(mc, list) and mc else 0

        # 3. Magic Formula metrics (ROC, EY, ROA, P/E).
        metrics = compute_company_metrics_detailed(ticker, live_cap, api_key)
        if not metrics.get("ok"):
            return json.dumps({
                "Symbol": ticker,
                "CompanyName": row.get("companyName", ticker),
                "Verdict": "FAIL",
                "Gate": "metrics_unavailable",
                "Reason": metrics.get("reason"),
                "Message": metrics.get("message"),
            })

        # 4. Greenblatt's ROA / P/E gates.
        ratio_reason = ratio_gate_reason(metrics)
        if ratio_reason:
            return json.dumps({
                "Symbol": ticker,
                "CompanyName": row.get("companyName", ticker),
                "Verdict": "FAIL",
                "Gate": "ratio",
                "Reason": ratio_reason,
                "Message": f"Rejected by Greenblatt's ROA/P/E gate: {ratio_reason}.",
                "ROA_Pct": metrics.get("ROA"),
                "PE_Ratio": metrics.get("PE"),
            })

        # 4b. 52-week-high proximity gate (same order as the full screen: after the
        # ROA/P/E gates, before the growth gate). The 52-week high/low are not in the
        # screener universe response, so this costs one /quote call — same as the
        # full screen, which spends it on ratio-gate survivors only.
        quote = fmp_get(
            "https://financialmodelingprep.com/stable/quote",
            params={"symbol": ticker, "apikey": api_key},
            context=f"{ticker} quote",
        )
        qrow = quote[0] if isinstance(quote, list) and quote else {}
        price_reason = price_gate_reason({
            "price": qrow.get("price"),
            "yearHigh": qrow.get("yearHigh"),
            "yearLow": qrow.get("yearLow"),
        })
        if price_reason:
            return json.dumps({
                "Symbol": ticker,
                "CompanyName": row.get("companyName", ticker),
                "Verdict": "FAIL",
                "Gate": "price",
                "Reason": price_reason,
                "Message": f"Rejected by the 52-week-high proximity gate: {price_reason}.",
                "Price": qrow.get("price"),
                "YearHigh": qrow.get("yearHigh"),
                "YearLow": qrow.get("yearLow"),
            })

        # 5. Lynch's PEG / growth gates (costs one extra request, same as the screen).
        attach_growth_metrics(ticker, metrics, api_key)
        growth_reason = growth_gate_reason(metrics)
        if growth_reason:
            return json.dumps({
                "Symbol": ticker,
                "CompanyName": row.get("companyName", ticker),
                "Verdict": "FAIL",
                "Gate": "growth",
                "Reason": growth_reason,
                "Message": f"Rejected by Lynch's PEG/growth gate: {growth_reason}.",
                "EPSGrowth": metrics.get("EPSGrowth"),
                "PEG": metrics.get("PEG"),
            })

        # Passed every gate.
        return json.dumps({
            "Symbol": ticker,
            "CompanyName": row.get("companyName", ticker),
            "Verdict": "PASS",
            "Gate": None,
            "Reason": None,
            "Message": "Passes all Magic Formula screen gates.",
            "ROA_Pct": metrics.get("ROA"),
            "PE_Ratio": metrics.get("PE"),
            "EPSGrowth": metrics.get("EPSGrowth"),
            "PEG": metrics.get("PEG"),
            "ROC_Pct": metrics.get("ROC"),
            "EarningsYield_Pct": metrics.get("EarningsYield"),
        })
    except FMPError as e:
        return json.dumps({"error": f"Could not check {ticker}: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e)})
