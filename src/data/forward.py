"""Forward-looking feeds: the price, the estimates, the calendar, the events.

Everything in the other data modules is BACKWARD-looking — filings, realised
quarters, annual ratios. That is the right bias for a verdict and the wrong bias
for the question a 'Watch' leaves open: at what price, or on what evidence, does
this become a Buy? These feeds answer that one.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import json
import os
import time
from datetime import datetime

import requests

from mcp_app import logger, mcp

from .fundamentals import _yoy_pct


# ==========================================
# TOOL 5: FORWARD-LOOKING FEEDS (what the buy case is built from)
# ==========================================
# Everything above this point is BACKWARD-looking: filings, realised quarters, annual
# ratios. That is the right bias for a verdict, and it is the wrong bias for the one
# question a 'Watch' leaves open — *at what price, or on what evidence, does this
# become a Buy?* Answering that needs the market's forward expectations (what
# earnings analysts expect and what they will pay for them), the calendar of events
# that could confirm or break those expectations, and the current price the range is
# measured from.
#
# These feeds are deliberately kept separate from `fmp_metrics_extractor` rather than
# folded into it. Four agents read that blob on every turn and none of them should be
# reasoning from analyst projections: the bear/bull/analyst chain weighs realised
# results, and a `Buy` may not rest on a speculative catalyst (see main.py's analyst
# instruction). Only the buy-case and buy-check agents get these.
def _stable_get(path: str, context: str, **params):
    """One GET against FMP's /stable API. Returns parsed JSON, or None on failure.

    Returns None rather than raising because every caller here is assembling a
    best-effort picture: a missing price-target feed should cost the report that one
    field, not the whole tool call. Callers say so in the payload rather than
    presenting a hole as a zero.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return None
    params["apikey"] = api_key
    try:
        time.sleep(0.20)  # 300 calls/min Starter rate limit
        resp = requests.get(f"https://financialmodelingprep.com/stable/{path}",
                            params=params, timeout=25)
        data = resp.json()
    except Exception as e:
        logger.warning(f"FMP {path} failed ({context}): {e}")
        return None
    # A plan-gated endpoint answers 402 with a plain-text upgrade notice, which json()
    # may still parse as a string. Treat anything that is not a list/dict as absent.
    if isinstance(data, dict) and data.get("Error Message"):
        logger.warning(f"FMP {path} error ({context}): {data['Error Message']}")
        return None
    return data if isinstance(data, (list, dict)) else None


def _quote_row(ticker: str) -> dict:
    """Current quote for one symbol, or {} — shared by the price and estimate tools."""
    data = _stable_get("quote", f"{ticker} quote", symbol=ticker)
    return data[0] if isinstance(data, list) and data else {}


def _pct(part, whole):
    """`part` as a percentage of `whole`, or None when that would be meaningless."""
    if part is None or whole in (None, 0):
        return None
    return round(100.0 * part / whole, 1)


@mcp.tool()
def fmp_price_snapshot(ticker: str) -> str:
    """
    Current price for a company, with the levels a buy range is measured against:
    previous close, the day's range, the 52-week high/low, and the 50-day and 200-day
    moving averages.

    Use this whenever a price threshold is being written or tested. A buy trigger such
    as "buy below $38" is only meaningful next to what the stock costs today and where
    that sits in its own recent range — a target 60% below the 52-week low is not a
    plan, it is a way of never buying.

    Returns JSON with symbol, price, previousClose, changePercentage, dayLow, dayHigh,
    yearHigh, yearLow, priceAvg50, priceAvg200, marketCap, exchange, plus computed
    `pct_from_52w_high` / `pct_above_52w_low` and `as_of` — or {"error": ...}.
    """
    row = _quote_row(ticker)
    if not row.get("price"):
        return json.dumps({"error": f"No current quote available for {ticker}."})
    price = row.get("price")
    out = {k: row.get(k) for k in (
        "symbol", "name", "price", "previousClose", "change", "changePercentage",
        "dayLow", "dayHigh", "yearHigh", "yearLow", "priceAvg50", "priceAvg200",
        "marketCap", "exchange", "volume",
    )}
    hi, lo = row.get("yearHigh"), row.get("yearLow")
    out["pct_from_52w_high"] = _yoy_pct(price, hi)      # negative = below the high
    out["pct_above_52w_low"] = _yoy_pct(price, lo)
    ts = row.get("timestamp")
    out["as_of"] = (datetime.fromtimestamp(ts).isoformat(timespec="minutes")
                    if isinstance(ts, (int, float)) else datetime.now().isoformat(timespec="minutes"))
    out["note"] = ("Price is live and moves every session. Any threshold written "
                   "against it must state the price it was written at, or a reader "
                   "cannot tell a 10% discount from a stale number.")
    return json.dumps(out, separators=(",", ":"))


def fmp_price_history(ticker: str, sessions: int = 220) -> list:
    """Daily closes for the last `sessions` trading days, OLDEST FIRST, or [].

    Deliberately NOT an MCP tool. It feeds the entry gate (`momentum.py`), which is
    computed in Python and printed beside the verdict; no agent reasons from it, and
    registering it would put a chart in front of the bear/bull/analyst chain, which
    is meant to judge the business, not the trend.
    """
    # ~1.45 calendar days per trading day, plus slack for holidays.
    start = datetime.now().date().toordinal() - int(sessions * 1.5) - 10
    data = _stable_get("historical-price-eod/light", f"{ticker} price history",
                       symbol=ticker,
                       **{"from": datetime.fromordinal(start).date().isoformat()})
    if not isinstance(data, list):
        return []
    rows = sorted((str(r.get("date"))[:10], r.get("price")) for r in data
                  if r.get("date") and isinstance(r.get("price"), (int, float))
                  and r["price"] > 0)
    return [float(p) for _, p in rows][-sessions:]


# FMP's Starter plan serves `analyst-estimates` for ANNUAL periods only — the
# quarterly variant answers 402. That is enough for a forward P/E, which is
# conventionally quoted on a fiscal year, but it means no quarter-by-quarter
# estimate path exists here; do not add one without re-checking the plan.
_ESTIMATE_FIELDS = ("date", "revenueAvg", "revenueLow", "revenueHigh", "ebitAvg",
                    "netIncomeAvg", "epsAvg", "epsLow", "epsHigh",
                    "numAnalystsRevenue", "numAnalystsEps")


@mcp.tool()
def fmp_forward_estimates(ticker: str) -> str:
    """
    What analysts expect this company to EARN and SELL in future fiscal years, what
    that implies about the multiple you would be paying today (forward P/E), and how
    many analysts stand behind each figure.

    Use this to answer "what would I be paying for the future, not the past?". It
    returns, for each future fiscal year on record: consensus revenue, EBIT, net
    income and EPS (mean, low, high), the number of contributing analysts, the implied
    forward P/E at today's price (and its range across the low/high EPS estimates),
    and the growth each year implies over the last ACTUAL fiscal year. It also returns
    the consensus price target, the most recent individual analyst target changes
    (with the price at the time), and the buy/hold/sell grade split.

    The coverage figures are not decoration. A forward P/E derived from a single
    analyst's EPS estimate is one person's opinion wearing the clothes of a market
    consensus, and the payload says so explicitly when the count is thin or the
    low/high spread is wide. Quote the basis whenever you quote the multiple.

    Returns JSON, or {"error": ...}.
    """
    quote = _quote_row(ticker)
    price = quote.get("price")

    rows = _stable_get("analyst-estimates", f"{ticker} estimates",
                       symbol=ticker, period="annual", limit=10)
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if not rows and price is None:
        return json.dumps({"error": f"No forward estimates or quote available for {ticker}."})

    # FMP returns furthest-out first and includes estimates for years already
    # reported. Split on today's date rather than trusting the order: a past-year
    # "estimate" is a record of what analysts thought, not a forecast, and mixing the
    # two is how a forward P/E ends up quoted off a year that has already happened.
    today = datetime.now().strftime("%Y-%m-%d")
    future = sorted([r for r in rows if (r.get("date") or "") > today],
                    key=lambda r: r.get("date") or "")
    past = sorted([r for r in rows if (r.get("date") or "") <= today],
                  key=lambda r: r.get("date") or "", reverse=True)

    # The base the growth rates are measured from: the last ANNUAL filing, not the
    # last estimate. Comparing an estimate against an estimate would report the
    # analysts' own internal consistency as if it were company growth.
    actual = _stable_get("income-statement", f"{ticker} annual actual",
                         symbol=ticker, period="annual", limit=1)
    actual = actual[0] if isinstance(actual, list) and actual else {}
    base_rev = actual.get("revenue")
    base_eps = actual.get("epsDiluted") or actual.get("eps")

    def _fwd(row):
        eps, eps_lo, eps_hi = row.get("epsAvg"), row.get("epsLow"), row.get("epsHigh")
        item = {k: row.get(k) for k in _ESTIMATE_FIELDS if row.get(k) is not None}
        if price and isinstance(eps, (int, float)) and eps > 0:
            item["forward_pe"] = round(price / eps, 1)
            # Widest defensible reading of the same price: the cheapest multiple comes
            # from the most optimistic EPS. Printing the band next to the point
            # estimate is what stops a 14x headline from hiding a 9x-31x disagreement.
            if isinstance(eps_hi, (int, float)) and eps_hi > 0:
                item["forward_pe_at_high_eps"] = round(price / eps_hi, 1)
            if isinstance(eps_lo, (int, float)) and eps_lo > 0:
                item["forward_pe_at_low_eps"] = round(price / eps_lo, 1)
        item["revenue_growth_vs_last_actual_pct"] = _yoy_pct(row.get("revenueAvg"), base_rev)
        item["eps_growth_vs_last_actual_pct"] = _yoy_pct(eps, base_eps)
        # How many fiscal years that growth is spread over. A forward year two or
        # three years out compounds, and quoting its cumulative growth as though it
        # were annual is the easiest way to make an ordinary forecast look like a
        # transformation. The caller annualises; this makes it possible to.
        try:
            item["fiscal_years_from_last_actual"] = (
                int(row["date"][:4]) - int(actual["date"][:4])) or None
        except Exception:
            pass
        n_eps = row.get("numAnalystsEps") or 0
        if n_eps and n_eps <= 2:
            item["coverage_warning"] = (
                f"Only {n_eps} analyst estimate(s) for EPS. This is one or two "
                f"opinions, not a consensus — do not describe it as 'the market expects'.")
        if (isinstance(eps_lo, (int, float)) and isinstance(eps_hi, (int, float))
                and eps_lo > 0 and eps_hi / eps_lo >= 1.5):
            item["spread_warning"] = (
                f"Estimates disagree widely (EPS {eps_lo} to {eps_hi}). The mean is a "
                f"midpoint between materially different views of this business.")
        return {k: v for k, v in item.items() if v is not None}

    payload = {
        "symbol": ticker,
        "current_price": price,
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "last_actual_fiscal_year": (
            {"date": actual.get("date"), "revenue": base_rev, "epsDiluted": base_eps}
            if actual else None
        ),
        "forward_fiscal_years": [_fwd(r) for r in future],
        # Kept because it is the only cheap check on whether this company's analysts
        # have historically been anywhere near right. Compare these to the actual
        # results in QUARTERLY_DATA / the filings before leaning on the forward year.
        "past_fiscal_year_estimates": [
            {k: r.get(k) for k in ("date", "revenueAvg", "epsAvg", "numAnalystsEps")
             if r.get(k) is not None}
            for r in past[:2]
        ],
        "price_target_consensus": (_stable_get("price-target-consensus",
                                               f"{ticker} price target", symbol=ticker) or None),
        "recent_analyst_target_changes": [
            {"date": (a.get("publishedDate") or "")[:10],
             "firm": a.get("analystCompany"), "analyst": a.get("analystName"),
             "priceTarget": a.get("priceTarget"), "priceWhenPosted": a.get("priceWhenPosted"),
             "headline": a.get("newsTitle")}
            for a in (_stable_get("price-target-news", f"{ticker} target news",
                                  symbol=ticker, limit=8) or [])
            if isinstance(a, dict)
        ],
        "grades_consensus": (_stable_get("grades-consensus", f"{ticker} grades",
                                         symbol=ticker) or None),
        "basis_note": (
            "forward_pe = current_price / that fiscal year's CONSENSUS MEAN EPS. These "
            "are ESTIMATES, not results: they are revised constantly, they are "
            "systematically optimistic at long horizons, and on this data plan they "
            "are available for FISCAL YEARS ONLY (no quarterly estimates). Fiscal "
            "years do not align with calendar years for many companies — quote the "
            "period-end date shown in `date`, never 'next year'. Analyst price targets "
            "are opinions about price, not about the business; report who set one and "
            "when, and never treat a consensus target as a valuation."
        ),
    }
    if not future:
        payload["coverage_note"] = (
            f"No FUTURE fiscal-year estimates are on record for {ticker}. That is "
            f"common for small and thinly-covered companies and is itself a finding: "
            f"there is no analyst consensus here to build a forward multiple on. Say "
            f"so plainly rather than substituting a trailing multiple for a forward one.")
    return json.dumps(payload, separators=(",", ":"))


@mcp.tool()
def fmp_earnings_calendar(ticker: str) -> str:
    """
    When a company next reports, what analysts expect it to report, and how its last
    four reports landed against expectations.

    Works for ANY symbol, which is the point: a buy case for a supplier is often
    decided by what its largest CUSTOMERS say, so call this for the customer's ticker
    as well as the subject's. If NVDA's case rests on hyperscaler capital spending,
    the dates MSFT and META report are dates on which the case can be checked.

    Returns JSON with `upcoming` (scheduled date + EPS/revenue estimates) and
    `recent` (reported date, actual vs. estimated EPS and revenue, plus a computed
    surprise percentage), or {"error": ...}.

    Scheduled dates before a company confirms them are PROVISIONAL and move by days —
    never write a trigger that turns on the exact date without saying so.
    """
    ticker = (ticker or "").strip().upper()
    rows = _stable_get("earnings", f"{ticker} earnings calendar", symbol=ticker, limit=12)
    rows = [r for r in rows if isinstance(r, dict) and r.get("date")] if isinstance(rows, list) else []
    if not rows:
        return json.dumps({"error": f"No earnings calendar available for {ticker}."})

    today = datetime.now().strftime("%Y-%m-%d")
    # `epsActual` is backfilled with a lag, so a report from two days ago can still
    # show a null actual. Split on the DATE (the same rule the screener's recent-
    # earnings gate uses) rather than on whether an actual has arrived.
    upcoming = sorted([r for r in rows if r["date"] > today], key=lambda r: r["date"])
    recent = sorted([r for r in rows if r["date"] <= today], key=lambda r: r["date"], reverse=True)

    def _reported(r):
        item = {
            "date": r.get("date"),
            "epsActual": r.get("epsActual"), "epsEstimated": r.get("epsEstimated"),
            "revenueActual": r.get("revenueActual"), "revenueEstimated": r.get("revenueEstimated"),
        }
        item["eps_surprise_pct"] = _yoy_pct(r.get("epsActual"), r.get("epsEstimated"))
        item["revenue_surprise_pct"] = _yoy_pct(r.get("revenueActual"), r.get("revenueEstimated"))
        return {k: v for k, v in item.items() if v is not None}

    return json.dumps({
        "symbol": ticker,
        "as_of": today,
        "upcoming": [{"date": r["date"], "epsEstimated": r.get("epsEstimated"),
                      "revenueEstimated": r.get("revenueEstimated")} for r in upcoming[:3]],
        "recent": [_reported(r) for r in recent[:4]],
        "note": ("Upcoming dates are the provider's schedule and are provisional until "
                 "the company confirms them; treat them as a week, not a day. A null "
                 "actual on a date that has passed usually means the provider has not "
                 "backfilled it yet, not that the company failed to report."),
    }, separators=(",", ":"))


@mcp.tool()
def fmp_revenue_segments(ticker: str) -> str:
    """
    Where a company's revenue actually comes from: its product/segment split and its
    geographic split, for the last two fiscal years, with each line as a share of
    total and its year-over-year change.

    Use this before naming a customer, an end market, or an upstream consumer as
    something that matters to the revenue line. A segment worth 3% of sales does not
    move the thesis however exciting its end market is, and "who buys this" is a
    question the segment table usually answers better than a press release does.

    Returns JSON with `product_segments` and `geographic_segments`, or {"error": ...}.
    """
    def _series(path, label):
        rows = _stable_get(path, f"{ticker} {label}", symbol=ticker, period="annual")
        rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("data"), dict)] \
            if isinstance(rows, list) else []
        rows = sorted(rows, key=lambda r: r.get("date") or "", reverse=True)[:2]
        if not rows:
            return None
        latest, prior = rows[0], (rows[1] if len(rows) > 1 else {"data": {}})
        total = sum(v for v in latest["data"].values() if isinstance(v, (int, float)))
        out = {
            "fiscal_year_end": latest.get("date"),
            "prior_fiscal_year_end": prior.get("date"),
            "total": total or None,
            "lines": sorted(
                [{"segment": k, "revenue": v,
                  "pct_of_total": _pct(v, total),
                  "yoy_pct": _yoy_pct(v, prior["data"].get(k))}
                 for k, v in latest["data"].items() if isinstance(v, (int, float))],
                key=lambda d: d["revenue"], reverse=True,
            ),
        }
        # Companies stop disclosing a breakdown when it stops being material, and the
        # provider simply keeps serving the last year it has. H&R Block's geographic
        # split ends in FY2016 — a decade stale, and perfectly capable of being read
        # as current by anyone who does not check the date. Say it in the payload.
        try:
            age = datetime.now().year - int((latest.get("date") or "")[:4])
        except Exception:
            age = 0
        if age >= 3:
            out["stale_warning"] = (
                f"This is the most recent {label} breakdown on record and it is about "
                f"{age} years old (fiscal year ending {latest.get('date')}). The "
                f"company has not disclosed one since. Do not present these shares as "
                f"the current mix.")
        return out

    product = _series("revenue-product-segmentation", "product segments")
    geo = _series("revenue-geographic-segmentation", "geographic segments")
    if not product and not geo:
        return json.dumps({"error": f"No segment disclosure available for {ticker}."})
    return json.dumps({
        "symbol": ticker,
        "product_segments": product,
        "geographic_segments": geo,
        "note": ("Segments are as the company chooses to report them and are ANNUAL. "
                 "They tell you what it sells and where, not who it sells to — a "
                 "named customer needs a filing (the SEC data carries >10% customer "
                 "concentration) or a source, not an inference from a segment name."),
    }, separators=(",", ":"))


# Pages of the M&A feed scanned by `fmp_pending_ma_filings`. Two pages of 250 covered
# roughly two years of filings when this was written, which is far enough back for a
# transaction still awaiting completion. The endpoint has no symbol filter on this
# plan (`mergers-acquisitions-search` answers 402), so the filtering is done here.
_MA_PAGES = 2


_MA_PAGE_SIZE = 250


@mcp.tool()
def fmp_pending_ma_filings(ticker: str) -> str:
    """
    SEC merger/acquisition filings naming this company, as either acquirer or target.

    A pending transaction is the single largest thing that can invalidate a price-based
    buy trigger: a company under an agreed bid trades on the offer, not on its
    earnings, and a buy range derived from a forward multiple becomes meaningless.
    Check this before writing one.

    STRICTLY LIMITED, and the limitation matters: this covers registration statements
    filed with the SEC (S-4 and similar) over roughly the last two years. It does NOT
    cover rumoured deals, cash tender offers that file differently, private-side talks,
    or anything reported in the press but not yet filed. An empty result means "no
    such filing was found", never "no deal is happening" — use `web_search_tool` and
    `fmp_stock_news` for the rest.

    Returns JSON with any matching filings (acquirer, target, transaction date, SEC
    link) plus the coverage caveat.
    """
    ticker = (ticker or "").strip().upper()
    hits, scanned = [], 0
    for page in range(_MA_PAGES):
        rows = _stable_get("mergers-acquisitions-latest", f"{ticker} M&A page {page}",
                           page=page, limit=_MA_PAGE_SIZE)
        if not isinstance(rows, list) or not rows:
            break
        scanned += len(rows)
        for r in rows:
            if not isinstance(r, dict):
                continue
            if ticker in {(r.get("symbol") or "").upper(),
                          (r.get("targetedSymbol") or "").upper()}:
                hits.append({
                    "role": "acquirer" if (r.get("symbol") or "").upper() == ticker else "target",
                    "acquirer": r.get("companyName"), "acquirerSymbol": r.get("symbol"),
                    "target": r.get("targetedCompanyName"), "targetSymbol": r.get("targetedSymbol"),
                    "transactionDate": r.get("transactionDate"),
                    "filingAccepted": r.get("acceptedDate"), "link": r.get("link"),
                })
    return json.dumps({
        "symbol": ticker,
        "filings": hits,
        "filings_scanned": scanned,
        "coverage": ("SEC registration statements (S-4 and similar) only, roughly the "
                     "last two years. Rumoured, unannounced, and differently-filed "
                     "transactions do not appear. An empty list is not evidence that "
                     "no transaction is pending."),
    }, separators=(",", ":"))
