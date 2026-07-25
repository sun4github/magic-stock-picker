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


def calculate_company_metrics(symbol, live_market_cap, api_key):
    """Computes Greenblatt Magic Formula ROC and Earnings Yield for one company.

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
                return None  # incomplete quarter → don't fabricate a TTM
            ebit = sum(ttm_parts)
            ebit_basis = "TTM"
        else:
            inc_a = fmp_get(inc_url, params={
                "symbol": symbol, "limit": 1, "apikey": api_key
            }, context=f"{symbol} income (annual)")
            if not isinstance(inc_a, list) or not inc_a:
                return None
            inc_latest = inc_a[0]
            ebit = inc_latest.get("operatingIncome")
            ebit_basis = "Annual"

        if ebit is None or ebit <= 0:
            return None

        # Reject stale earnings so we never rank on dead data.
        if _is_stale(inc_latest):
            return None

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
                return None
            bal = bal_a[0]

        if _is_stale(bal):
            return None

        # --- Balance sheet items ---
        current_assets = bal.get("totalCurrentAssets", 0) or 0
        current_liabilities = bal.get("totalCurrentLiabilities", 0) or 0
        nfa = bal.get("propertyPlantEquipmentNet", 0) or 0
        cash = bal.get("cashAndShortTermInvestments", 0) or 0
        total_debt = bal.get("totalDebt", 0) or 0
        preferred = bal.get("preferredStock", 0) or 0
        minority = bal.get("minorityInterest", 0) or 0

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
            return None

        # Enterprise Value = Market Cap + Debt + Preferred + Minority Interest - Cash.
        enterprise_value = live_market_cap + total_debt + preferred + minority - cash
        if enterprise_value <= 0:
            return None

        roc = ebit / capital_employed
        earnings_yield = ebit / enterprise_value

        return {
            "Symbol": symbol,
            "CompanyName": inc_latest.get("companyName", symbol),
            "LiveMarketCap": live_market_cap,
            "EBIT": ebit,
            "EBIT_Basis": ebit_basis,
            "CapitalEmployed": capital_employed,
            "EnterpriseValue": enterprise_value,
            "ROC": roc,
            "EarningsYield": earnings_yield
        }

    except FMPError:
        # Persistent fetch failure for this symbol (already retried inside fmp_get).
        # Propagate so the run loop can count and report it rather than hide it.
        raise
    except Exception:
        # Any other unexpected issue with one company's data must not abort the
        # whole run — skip it. (Fetch problems are handled above and surfaced.)
        return None

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
        "ROC_Rank", "EY_Rank"
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