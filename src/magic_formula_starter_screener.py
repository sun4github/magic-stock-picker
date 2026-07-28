import os
import time
import requests
import pandas as pd
import yaml
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURATION & PARAMETERS
# ==========================================
API_KEY = os.getenv("FMP_API_KEY", "YOUR_FMP_API_KEY_HERE")

# Load config
config_path = os.path.join(os.path.dirname(__file__), "specs", "config.yaml")
with open(config_path, "r") as f:
    config = yaml.safe_load(f)

screening_params = config.get("screening_parameters", {})
MIN_MARKET_CAP = screening_params.get("min_market_cap", 100_000_000)
UNIVERSE_LIMIT = screening_params.get("universe_limit", 2500)
# Reject statements older than this many days so we never rank on stale filings.
# ~200 days ≈ two quarters of grace for late filers while still excluding dead data.
MAX_STATEMENT_AGE_DAYS = screening_params.get("max_statement_age_days", 200)
# Anchor outputs to this script's directory (src/) so they're written and read at
# ONE deterministic location regardless of the current working directory. A relative
# path silently produced multiple stray copies (one per cwd the script was run from).
SCRIPT_DIR = os.path.dirname(__file__)
# Stable "latest" file: always overwritten with the most recent run. mcp_server.py
# and the webapp import/read this constant, so it must keep a fixed name.
OUTPUT_FILENAME = os.path.join(SCRIPT_DIR, "magic_formula_rankings_live.csv")
# Timestamped archive of every run lives here, so past runs are never overwritten
# and you can see exactly when each was produced.
HISTORY_DIR = os.path.join(SCRIPT_DIR, "rankings_history")

# Excluded Sectors per Joel Greenblatt
EXCLUDED_SECTORS = screening_params.get("excluded_sectors", [
    "Financial Services",
    "Financial",
    "Utilities",
    "Banking"
])

headers = {"User-Agent": "Mozilla/5.0"}

# HTTP robustness knobs (override via screening_parameters in config.yaml).
HTTP_TIMEOUT = screening_params.get("http_timeout_seconds", 20)
HTTP_MAX_RETRIES = screening_params.get("http_max_retries", 3)
HTTP_BACKOFF_BASE = screening_params.get("http_backoff_seconds", 1.0)

# Transient HTTP statuses worth retrying (rate limit + gateway/server errors).
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FMPError(Exception):
    """An FMP request failed after exhausting retries, or returned a fatal API error.

    Carries a human-readable message so callers can log exactly what went wrong
    (network timeout, rate limit, invalid key, non-JSON body, ...).
    """


def fmp_get(url, params=None, context=""):
    """GET an FMP endpoint with a timeout, bounded retries, and clear errors.

    Retries transient failures (network timeouts/resets, HTTP 429/5xx, and FMP's
    quirk of signalling rate limits as a 200 response carrying an ``Error Message``
    dict) with exponential backoff. Returns parsed JSON on success. Raises
    :class:`FMPError` — with a descriptive message — once retries are exhausted or
    on a non-retryable client error (e.g. an invalid key).
    """
    label = context or url
    last_err = "unknown error"
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            res = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.exceptions.Timeout:
            last_err = f"timed out after {HTTP_TIMEOUT}s"
        except requests.exceptions.RequestException as e:
            last_err = f"network error: {e}"
        else:
            if res.status_code == 200:
                try:
                    data = res.json()
                except ValueError:
                    last_err = f"non-JSON response ({len(res.text)} chars)"
                else:
                    # FMP returns rate-limit / auth / plan problems as a 200 + dict.
                    if isinstance(data, dict) and data.get("Error Message"):
                        last_err = f"API error: {data['Error Message']}"
                    else:
                        return data  # success
            elif res.status_code in RETRYABLE_STATUS:
                last_err = f"HTTP {res.status_code}"
            else:
                # 4xx (bad request, forbidden, not found): retrying won't help.
                raise FMPError(f"{label}: HTTP {res.status_code} — {res.text[:200]}")

        if attempt < HTTP_MAX_RETRIES:
            backoff = HTTP_BACKOFF_BASE * (2 ** (attempt - 1))
            print(f"  [retry {attempt}/{HTTP_MAX_RETRIES}] {label}: {last_err} — waiting {backoff:.1f}s")
            time.sleep(backoff)

    raise FMPError(f"{label}: failed after {HTTP_MAX_RETRIES} attempts — {last_err}")


def fetch_screener_universe(api_key, min_market_cap, limit):
    """Fetches eligible US common stocks above min_market_cap."""
    print("Fetching pre-filtered stock universe from FMP Screener...")
    url = "https://financialmodelingprep.com/stable/company-screener"
    params = {
        "marketCapMoreThan": min_market_cap,
        "isEtf": "false",
        "isFund": "false",
        "isActivelyTrading": "true",
        "country": "US",
        "limit": limit,  # configurable via screening_parameters.universe_limit in config.yaml
        "apikey": api_key
    }
    data = fmp_get(url, params=params, context="screener universe")
    if not isinstance(data, list):
        raise FMPError(f"screener universe: unexpected response type {type(data).__name__}")
    # Exclude REITs by industry (mortgage AND equity). They're financial in nature —
    # a REIT has no meaningful "capital employed" in Greenblatt's sense, so its ROC
    # is distorted (e.g. mortgage-REIT LADR ranking top-5). Filtering by industry
    # rather than the whole "Real Estate" sector keeps legitimate operating
    # developers and homebuilders in the universe.
    valid_stocks = [
        item for item in data
        if item.get("sector") not in EXCLUDED_SECTORS
        and "reit" not in (item.get("industry") or "").lower()
        and item.get("symbol")
    ]
    print(f"Found {len(valid_stocks)} eligible companies.")
    return valid_stocks

def _is_stale(statement, max_age_days=MAX_STATEMENT_AGE_DAYS):
    """True if a statement's reporting date is older than max_age_days (or unparseable)."""
    date_str = statement.get("date") or statement.get("fillingDate") or statement.get("acceptedDate")
    if not date_str:
        return True
    try:
        d = datetime.fromisoformat(str(date_str)[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - d).days > max_age_days


def format_money(value):
    """Abbreviate a raw dollar figure (e.g. 1234567890 -> '$1.23B') for display.
    Returns 'Not available' for missing/unparseable values. Shared by the
    screener's skip messages and the final report's metrics section so dollar
    amounts read identically everywhere."""
    if value is None or value == "":
        return "Not available"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "Not available"
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1e9:
        s = f"{v / 1e9:.2f}B"
    elif v >= 1e6:
        s = f"{v / 1e6:.2f}M"
    elif v >= 1e3:
        s = f"{v / 1e3:.2f}K"
    else:
        s = f"{v:.2f}"
    return f"{sign}${s}"


def _skip(reason, message, **extra):
    """Structured 'this company cannot be ranked' result. `reason` is a stable
    machine slug; `message` is the plain-English explanation shown to readers of
    the final report."""
    return {"ok": False, "reason": reason, "message": message, **extra}


def compute_company_metrics_detailed(symbol, live_market_cap, api_key):
    """Computes Greenblatt Magic Formula ROC and Earnings Yield for one company,
    returning a STRUCTURED result that explains why a company was skipped.

    Returns {"ok": True, ...metrics} on success, or {"ok": False, "reason": slug,
    "message": plain-English why} when the company cannot be ranked. The bare
    `calculate_company_metrics` wrapper below preserves the None-on-failure
    contract for the screener's hot loop; the single-ticker path uses this
    version so the final report can say WHY a metric is missing rather than an
    unexplained 'Not available'.

    Period handling (matches magicformulainvesting.com):
      - EBIT: trailing twelve months (TTM) = sum of the last 4 quarters. Falls back
        to the latest annual filing when 4 quarters are unavailable.
      - Balance sheet (capital, cash, debt): the single most recent quarter snapshot,
        with an annual fallback.
      - Market cap: live, passed in by the caller.
    """
    try:
        inc_url = "https://financialmodelingprep.com/stable/income-statement"
        bal_url = "https://financialmodelingprep.com/stable/balance-sheet-statement"

        # --- EBIT: prefer TTM (last 4 quarters), fall back to latest annual ---
        inc_q = fmp_get(inc_url, params={
            "symbol": symbol, "period": "quarter", "limit": 4, "apikey": api_key
        }, context=f"{symbol} income (quarter)")

        if isinstance(inc_q, list) and len(inc_q) >= 4:
            inc_latest = inc_q[0]
            ttm_parts = [q.get("operatingIncome") for q in inc_q[:4]]
            if any(v is None for v in ttm_parts):
                # incomplete quarter → don't fabricate a TTM
                return _skip(
                    "incomplete_quarters",
                    "The data provider returned an incomplete set of quarterly results, "
                    "so a full twelve months of operating profit could not be assembled "
                    "without guessing at the missing quarter.",
                )
            ebit = sum(ttm_parts)
            ebit_basis = "TTM"
        else:
            inc_a = fmp_get(inc_url, params={
                "symbol": symbol, "limit": 1, "apikey": api_key
            }, context=f"{symbol} income (annual)")
            if not isinstance(inc_a, list) or not inc_a:
                return _skip(
                    "no_income_data",
                    "No income statement was available from the data provider for this "
                    "company, so its profit could not be measured.",
                )
            inc_latest = inc_a[0]
            ebit = inc_latest.get("operatingIncome")
            ebit_basis = "Annual"

        if ebit is None:
            return _skip(
                "no_ebit",
                "The data provider did not report an operating profit figure for this "
                "company, so the Magic Formula ratios could not be calculated.",
            )

        if ebit <= 0:
            return _skip(
                "negative_ebit",
                f"This company is currently losing money at the operating level: its "
                f"operating profit ({ebit_basis} basis) was {format_money(ebit)}. Both "
                f"Magic Formula ratios divide by this profit figure, so with no profit "
                f"to divide there is nothing meaningful to measure — a negative result "
                f"would look like a bargain when it is the opposite. Greenblatt's "
                f"strategy deliberately screens out unprofitable companies for this "
                f"reason.",
                EBIT=ebit,
                EBIT_Basis=ebit_basis,
            )

        # Reject stale earnings so we never rank on dead data.
        if _is_stale(inc_latest):
            return _skip(
                "stale_income",
                "The most recent earnings report available for this company is too old "
                "to rely on, so its figures were rejected rather than used to justify a "
                "decision on out-of-date data.",
            )

        # --- Balance sheet: latest quarter snapshot, annual fallback ---
        bal_q = fmp_get(bal_url, params={
            "symbol": symbol, "period": "quarter", "limit": 1, "apikey": api_key
        }, context=f"{symbol} balance (quarter)")
        if isinstance(bal_q, list) and bal_q:
            bal = bal_q[0]
        else:
            bal_a = fmp_get(bal_url, params={
                "symbol": symbol, "limit": 1, "apikey": api_key
            }, context=f"{symbol} balance (annual)")
            if not isinstance(bal_a, list) or not bal_a:
                return _skip(
                    "no_balance_sheet",
                    "No balance sheet was available from the data provider for this "
                    "company, so the money tied up in running the business could not "
                    "be measured.",
                    EBIT=ebit,
                    EBIT_Basis=ebit_basis,
                )
            bal = bal_a[0]

        if _is_stale(bal):
            return _skip(
                "stale_balance_sheet",
                "The most recent balance sheet available for this company is too old to "
                "rely on, so its figures were rejected rather than used to justify a "
                "decision on out-of-date data.",
                EBIT=ebit,
                EBIT_Basis=ebit_basis,
            )

        # --- Balance sheet items ---
        current_assets = bal.get("totalCurrentAssets", 0) or 0
        current_liabilities = bal.get("totalCurrentLiabilities", 0) or 0
        nfa = bal.get("propertyPlantEquipmentNet", 0) or 0
        cash = bal.get("cashAndShortTermInvestments", 0) or 0
        total_debt = bal.get("totalDebt", 0) or 0
        preferred = bal.get("preferredStock", 0) or 0
        minority = bal.get("minorityInterest", 0) or 0
        total_equity = bal.get("totalStockholdersEquity", 0) or 0
        total_assets = bal.get("totalAssets", 0) or 0
        goodwill_intangibles = bal.get("goodwillAndIntangibleAssets", 0) or 0

        # Capital Employed = Net Working Capital + Net Fixed Assets.
        # Greenblatt excludes *excess* cash from NWC — cash beyond what the business
        # needs to cover current liabilities. We keep enough cash to plug any
        # shortfall between non-cash current assets and current liabilities, and
        # strip only the surplus. This avoids both (a) overstating capital for
        # cash-rich firms and (b) driving capital negative and wrongly dropping
        # asset-light, net-cash companies (e.g. ADBE) that should rank as high ROC.
        non_cash_ca = current_assets - cash
        cash_needed = max(0, current_liabilities - non_cash_ca)
        excess_cash = max(0, cash - cash_needed)
        # Floor NWC at 0: a business financed by its suppliers/customers (negative
        # working capital, e.g. ADBE, HPQ) uses no net working capital — it neither
        # gets dropped for having "negative capital" nor an artificially inflated
        # ROC. Capital employed then collapses to net fixed assets. This is
        # Greenblatt's convention and why such names appear on his high-ROC list.
        nwc = max(0, (current_assets - excess_cash) - current_liabilities)
        capital_employed = nwc + nfa
        if capital_employed <= 0:
            return _skip(
                "no_capital_employed",
                "This company reports effectively no money tied up in running the "
                "business (no net working capital and no physical assets), so the "
                "return-on-capital calculation would divide by zero.",
                EBIT=ebit,
                EBIT_Basis=ebit_basis,
            )

        # Enterprise Value = Market Cap + Debt + Preferred + Minority Interest - Cash.
        enterprise_value = live_market_cap + total_debt + preferred + minority - cash
        if enterprise_value <= 0:
            return _skip(
                "negative_enterprise_value",
                "This company holds more cash than its market value plus debt combined, "
                "giving it a negative takeover price. That makes the earnings yield "
                "calculation meaningless, so it was excluded.",
                EBIT=ebit,
                EBIT_Basis=ebit_basis,
                CapitalEmployed=capital_employed,
            )

        roc = ebit / capital_employed
        earnings_yield = ebit / enterprise_value

        # --- Sanity companion to ROC: return on the capital ACTUALLY spent ---
        # Greenblatt's capital employed (NWC + net fixed assets) deliberately
        # excludes goodwill and intangibles, because he is measuring the economics
        # of putting the NEXT dollar into the business. That is the right question
        # for ranking a basket, but for a company assembled by acquisition it makes
        # the headline ROC enormous while saying nothing about the return earned on
        # the purchase price. Invested capital below includes that purchase price
        # (equity + debt - cash), so the two figures bracket the truth. Reported
        # alongside ROC so neither an agent nor a reader can mistake a definitional
        # artifact for elite operating efficiency (see specs/agent_architecture.md §3.C).
        invested_capital = total_equity + total_debt + minority - cash
        roic_incl_goodwill = ebit / invested_capital if invested_capital > 0 else None
        intangibles_share = (goodwill_intangibles / total_assets) if total_assets > 0 else None

        return {
            "ok": True,
            "Symbol": symbol,
            "CompanyName": inc_latest.get("companyName", symbol),
            "LiveMarketCap": live_market_cap,
            "EBIT": ebit,
            "EBIT_Basis": ebit_basis,
            "CapitalEmployed": capital_employed,
            "EnterpriseValue": enterprise_value,
            "ROC": roc,
            "EarningsYield": earnings_yield,
            # Balance-sheet provenance. These are the figures the reconciliation
            # gate in main.py checks agent-written prose against, so they must be
            # the same numbers that fed EV above — not a second lookup.
            "TotalDebt": total_debt,
            "Cash": cash,
            "TotalEquity": total_equity,
            "TotalAssets": total_assets,
            "GoodwillAndIntangibles": goodwill_intangibles,
            "InvestedCapital": invested_capital if invested_capital > 0 else None,
            "ROIC_InclGoodwill": roic_incl_goodwill,
            "IntangiblesShareOfAssets": intangibles_share,
            "BalanceSheetDate": bal.get("date"),
            "IncomeStatementDate": inc_latest.get("date"),
        }

    except FMPError:
        # Persistent fetch failure for this symbol (already retried inside fmp_get).
        # Propagate so the run loop can count and report it rather than hide it.
        raise
    except Exception as e:
        # Any other unexpected issue with one company's data must not abort the
        # whole run — skip it. (Fetch problems are handled above and surfaced.)
        return _skip(
            "unexpected_error",
            "An unexpected problem occurred while processing this company's financial "
            "data, so its Magic Formula ratios could not be calculated.",
            detail=str(e),
        )


def calculate_company_metrics(symbol, live_market_cap, api_key):
    """Back-compat wrapper over `compute_company_metrics_detailed`: returns the
    plain metrics dict on success, or None when the company cannot be ranked.
    The screener's universe loop only needs success/failure; the single-ticker
    path calls the detailed version so it can explain the failure."""
    result = compute_company_metrics_detailed(symbol, live_market_cap, api_key)
    if not result.get("ok"):
        return None
    # Strip the marker so the screener's DataFrame doesn't gain an 'ok' column.
    return {k: v for k, v in result.items() if k != "ok"}


def main():
    if API_KEY == "YOUR_FMP_API_KEY_HERE":
        print("Error: Please insert your actual FMP API Key into the script.")
        return

    # Step 1: Universe (fatal if this fails — nothing to rank without it)
    try:
        universe = fetch_screener_universe(API_KEY, MIN_MARKET_CAP, UNIVERSE_LIMIT)
    except FMPError as e:
        print(f"\nFATAL: could not fetch the stock universe — {e}")
        print("Aborting: check your FMP_API_KEY, plan limits, and network connection.")
        return

    # Step 2: Process Financial Statements. Market cap and price come straight from
    # the screener response — the /batch-quote endpoint is restricted on the Starter
    # plan (HTTP 402), and the screener's own marketCap/price are the same source the
    # magicformula site displays, so no extra per-symbol quote calls are needed.
    results = []
    error_count = 0          # symbols skipped due to persistent fetch failures
    ERROR_LOG_LIMIT = 15     # cap the per-symbol error spam; total is summarised below
    print("\nCalculating Magic Formula metrics across universe...")
    for idx, company in enumerate(universe):
        symbol = company["symbol"]

        market_cap = company.get("marketCap", 0) or 0

        try:
            metrics = calculate_company_metrics(symbol, market_cap, API_KEY)
        except FMPError as e:
            error_count += 1
            if error_count <= ERROR_LOG_LIMIT:
                print(f"  [skip] {e}")
            elif error_count == ERROR_LOG_LIMIT + 1:
                print("  [skip] ...further fetch errors suppressed (see summary at end)...")
            metrics = None

        if metrics:
            metrics["Price"] = company.get("price", 0) or 0
            results.append(metrics)

        # Pause to easily stay within Starter Limit (300 calls/min = ~5 calls/sec)
        time.sleep(0.15)

        if (idx + 1) % 50 == 0:
            print(f"Processed {idx + 1}/{len(universe)} stocks... ({len(results)} valid so far)")

    if error_count:
        pct = 100 * error_count / len(universe)
        print(f"\nNote: {error_count}/{len(universe)} symbols ({pct:.1f}%) skipped due to "
              f"repeated fetch errors after retries.")
        if pct >= 25:
            print("  WARNING: high failure rate suggests a systemic issue "
                  "(rate limit, plan restriction, or network) — results may be incomplete.")

    df = pd.DataFrame(results)
    if df.empty:
        print("No valid companies calculated.")
        return

    # Data-basis integrity check. The Starter plan officially guarantees only
    # ANNUAL fundamentals; quarterly (needed for TTM EBIT) works in practice but
    # is not contractually guaranteed. If FMP ever restricts it, companies will
    # silently fall back to annual, producing a mixed-basis ranking (TTM vs stale
    # annual) — apples vs oranges. Surface the split so any such drift is obvious.
    if "EBIT_Basis" in df.columns:
        basis_counts = df["EBIT_Basis"].value_counts().to_dict()
        ttm_n = basis_counts.get("TTM", 0)
        ann_n = basis_counts.get("Annual", 0)
        print(f"\nEBIT basis: {ttm_n} TTM (last 4 quarters), {ann_n} Annual fallback.")
        # A few annual stragglers (newly listed, thin filers) are benign. Only warn
        # when the annual share is material enough to distort a relative ranking —
        # the signal that FMP may have restricted quarterly access wholesale.
        total_basis = ttm_n + ann_n
        pct_annual = 100 * ann_n / total_basis if total_basis else 0
        if pct_annual >= 5:
            print(f"  WARNING: {pct_annual:.0f}% of the universe fell back to annual EBIT; "
                  f"rankings mix TTM and annual bases. Investigate quarterly access.")

    # Step 4: Ranking
    print("\nComputing Final Composite Ranks...")
    df["ROC_Rank"] = df["ROC"].rank(ascending=False, method="min")
    df["EY_Rank"] = df["EarningsYield"].rank(ascending=False, method="min")

    df["MagicFormula_Score"] = df["ROC_Rank"] + df["EY_Rank"]
    df["Final_Rank"] = df["MagicFormula_Score"].rank(ascending=True, method="min").astype(int)

    df = df.sort_values(by="Final_Rank", ascending=True)

    # Format Display
    df["ROC_Pct"] = (df["ROC"] * 100).round(2).astype(str) + "%"
    df["EY_Pct"] = (df["EarningsYield"] * 100).round(2).astype(str) + "%"
    df["Live_Price"] = "$" + df["Price"].round(2).astype(str)

    output_df = df[[
        "Final_Rank", "Symbol", "CompanyName", "Live_Price",
        "LiveMarketCap", "ROC_Pct", "EY_Pct", "EBIT_Basis", "MagicFormula_Score",
        "ROC_Rank", "EY_Rank",
        # Raw component dollar figures, kept alongside the Pct columns so the
        # final report can show the actual EY/ROC formula, not just the result.
        "EBIT", "CapitalEmployed", "EnterpriseValue",
        # Balance-sheet provenance + the goodwill-inclusive ROIC companion. Carried
        # through to the CSV so a --from-csv run gets the same authoritative figures
        # (and the same reconciliation gate) as an on-demand single-ticker run.
        "TotalDebt", "Cash", "TotalEquity", "TotalAssets", "GoodwillAndIntangibles",
        "InvestedCapital", "ROIC_InclGoodwill", "IntangiblesShareOfAssets",
        "BalanceSheetDate",
    ]]

    # Write a timestamped archive of this run, plus overwrite the stable "latest"
    # file that the MCP server / webapp always read.
    os.makedirs(HISTORY_DIR, exist_ok=True)
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_path = os.path.join(HISTORY_DIR, f"magic_formula_rankings_{run_stamp}.csv")
    output_df.to_csv(timestamped_path, index=False)
    output_df.to_csv(OUTPUT_FILENAME, index=False)
    print(f"\nSuccess! Processed {len(output_df)} valid candidates.")
    print(f"Archived run : '{timestamped_path}'")
    print(f"Latest (stable): '{OUTPUT_FILENAME}'")

    print("\n--- TOP 10 LIVE MAGIC FORMULA CANDIDATES ---")
    print(output_df.head(10).to_string(index=False))

if __name__ == "__main__":
    main()