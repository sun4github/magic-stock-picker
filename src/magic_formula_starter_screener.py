import os
import time
import requests
import pandas as pd
import yaml
from datetime import datetime, timedelta, timezone
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

# Greenblatt's step-by-step screen ("The Little Book That Still Beats the Market",
# step-by-step appendix). These are ELIGIBILITY GATES applied on top of the universe;
# the RANKING remains the real Magic Formula (Return on Capital + Earnings Yield).
# See the long comment block under `screening_parameters:` in specs/config.yaml.
MIN_ROA = screening_params.get("min_roa", 0.25)
ROA_BASIS = (screening_params.get("roa_basis") or "ebit").lower()
MIN_PE = screening_params.get("min_pe", 5.0)
RECENT_EARNINGS_DAYS = screening_params.get("exclude_recent_earnings_days", 7)

# Financial/fund industries that FMP files OUTSIDE the excluded sectors above.
# Matched case-insensitively as substrings of the `industry` field.
EXCLUDED_INDUSTRIES = [
    s.lower() for s in screening_params.get("excluded_industries", [
        "bank", "insurance", "asset management", "capital markets",
        "financial conglomerate", "financial data", "shell companies",
        "mortgage", "closed-end fund", "exchange traded fund", "reit",
    ])
]

EXCLUDE_ADR = screening_params.get("exclude_adr", True)
ALLOWED_COUNTRIES = {
    (c or "").upper() for c in screening_params.get("allowed_countries", ["US"])
}

# Name markers for a depositary receipt. FMP writes these into `companyName` for
# foreign issuers trading on US exchanges; the ticker itself carries no reliable
# signal (the 5-letter '...Y' convention only covers OTC receipts).
ADR_NAME_MARKERS = (
    "adr", "ads", "american depositary", "american depository",
    "depositary receipt", "depositary share",
)

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


def is_adr(company):
    """True if a screener row looks like a foreign issuer's depositary receipt.

    Greenblatt's step-by-step screen says to "eliminate all foreign companies",
    which on a US screener means the ADRs. There is no `isAdr` flag on the screener
    payload, so this leans on the two signals that ARE present: the depositary-receipt
    wording FMP writes into the company name, and a non-US country of domicile.
    """
    name = (company.get("companyName") or "").lower()
    if any(marker in name for marker in ADR_NAME_MARKERS):
        return True
    country = (company.get("country") or "").upper()
    # An empty country is unknown, not foreign — don't drop a company on missing data.
    return bool(country) and country not in ALLOWED_COUNTRIES


def _universe_exclusion_reason(company):
    """Why this company is ineligible before any financials are fetched, or None.

    Covers steps 3 and 4 of Greenblatt's DIY screen (drop utilities, financials and
    foreign companies). Kept separate from the ratio gates because these need no API
    calls — a company rejected here never costs us two statement requests.
    """
    if not company.get("symbol"):
        return "no_symbol"
    if company.get("sector") in EXCLUDED_SECTORS:
        return "excluded_sector"
    if company.get("isEtf") or company.get("isFund"):
        return "fund_or_etf"
    industry = (company.get("industry") or "").lower()
    # REITs are financial in nature — a REIT has no meaningful "capital employed" in
    # Greenblatt's sense, so its ROC is distorted (e.g. mortgage-REIT LADR ranking
    # top-5). Filtering by INDUSTRY rather than the whole "Real Estate" sector keeps
    # legitimate operating developers and homebuilders in the universe. The same
    # applies to the banks/insurers/asset managers FMP files outside the excluded
    # sectors: catching them by industry avoids nuking an entire sector to reach them.
    if any(kw in industry for kw in EXCLUDED_INDUSTRIES):
        return "excluded_industry"
    if EXCLUDE_ADR and is_adr(company):
        return "foreign_adr"
    return None


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

    valid_stocks = []
    dropped = {}
    for item in data:
        reason = _universe_exclusion_reason(item)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            valid_stocks.append(item)

    print(f"Found {len(valid_stocks)} eligible companies (from {len(data)} returned).")
    if dropped:
        # Print the breakdown so a mis-tuned exclusion list is visible as a number
        # rather than as a company quietly missing from the rankings.
        detail = ", ".join(f"{n} {reason}" for reason, n in sorted(dropped.items()))
        print(f"  Excluded before fetching financials: {detail}.")
    return valid_stocks

def fetch_recent_earnings_symbols(api_key, days=RECENT_EARNINGS_DAYS, symbols=None):
    """Symbols that ANNOUNCED earnings within the last `days` days.

    Greenblatt's step-by-step screen drops these: a report published days ago has not
    been absorbed into the price yet, so the screen would be ranking on figures the
    market is still digesting.

    Two paths, cheapest first:
      1. `/stable/earnings-calendar` over the date window — ONE call for the whole
         market (~1,500 rows for a week). Preferred; the screen only needs "who
         reported", not the numbers.
      2. Per-symbol `/stable/earnings` — used only if the calendar endpoint is not
         available on this plan, and only for the handful of symbols that survived
         the ratio gates, so the cost is bounded.

    A calendar DATE inside the window is enough to exclude a company; we do not also
    require `epsActual` to be filled in. Both endpoints carry scheduled dates whose
    result fields are still null, and FMP backfills them with a lag — so insisting on
    a posted EPS would keep exactly the company that reported two days ago and hasn't
    been transcribed yet, which is the case this filter exists to catch. Erring the
    other way costs us a handful of names out of a ~2,500-company universe.

    Returns (symbols_set, ok). `ok` is False when neither path worked — the caller
    must then say so out loud rather than let the run pretend the filter was applied.
    """
    if not days or days <= 0:
        return set(), True

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    try:
        data = fmp_get(
            "https://financialmodelingprep.com/stable/earnings-calendar",
            params={"from": start.isoformat(), "to": today.isoformat(), "apikey": api_key},
            context="earnings calendar",
        )
        if isinstance(data, list):
            # The endpoint is already bounded by from/to, but re-check each row's date
            # rather than trusting the query: a stray out-of-window row would otherwise
            # eliminate a company for no reason.
            recent = set()
            for row in data:
                if not isinstance(row, dict) or not row.get("symbol"):
                    continue
                try:
                    d = datetime.fromisoformat(str(row.get("date") or "")[:10]).date()
                except ValueError:
                    continue
                if start <= d <= today:
                    recent.add(row["symbol"])
            return recent, True
        print(f"  Earnings calendar returned {type(data).__name__}, not a list — "
              f"falling back to per-symbol lookups.")
    except FMPError as e:
        print(f"  Earnings calendar unavailable ({e}) — falling back to per-symbol lookups.")

    # --- Fallback: ask each surviving symbol directly ---
    if not symbols:
        return set(), False

    recent = set()
    failures = 0
    for symbol in symbols:
        try:
            rows = fmp_get(
                "https://financialmodelingprep.com/stable/earnings",
                params={"symbol": symbol, "limit": 4, "apikey": api_key},
                context=f"{symbol} earnings dates",
            )
        except FMPError:
            failures += 1
            continue
        if not isinstance(rows, list):
            failures += 1
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Same rule as the bulk path above: the date alone decides. This endpoint
            # also lists UPCOMING dates (with null results), but those fall outside
            # the trailing window and are filtered by the range check below.
            date_str = str(row.get("date") or "")[:10]
            try:
                d = datetime.fromisoformat(date_str).date()
            except ValueError:
                continue
            if start <= d <= today:
                recent.add(symbol)
                break
        time.sleep(0.15)

    if failures:
        print(f"  Note: earnings dates could not be read for {failures}/{len(symbols)} "
              f"symbols; those were kept rather than dropped on missing data.")
    # The fallback succeeded as long as it wasn't a total washout.
    return recent, failures < len(symbols)


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
            # Bottom-line profit over the SAME four quarters, from the SAME payload —
            # no extra request. Feeds the P/E gate and the net-income view of ROA.
            # Left as None on a partial set rather than summing three quarters and
            # calling it a year, which would understate earnings and inflate P/E.
            ni_parts = [q.get("netIncome") for q in inc_q[:4]]
            net_income = None if any(v is None for v in ni_parts) else sum(ni_parts)
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
            net_income = inc_latest.get("netIncome")

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

        # Why the companion figure is missing, when it is. Invested capital goes
        # non-positive for companies that have bought back more stock than their
        # retained earnings cover (BKNG, WINA) — shareholders' equity turns negative
        # and "return on capital invested" has no denominator left to divide by.
        #
        # This matters because the companion is not decoration: it is the ONLY guard
        # against reading a four-digit Magic Formula ROC as operating efficiency
        # (spec §2.F). Silently omitting it leaves the headline standing alone on
        # exactly the companies whose headline is least trustworthy. So the reason is
        # carried through to the report, and ROA — which divides by every asset
        # including goodwill, and is always computable — is named as the fallback
        # counterweight. ROA is NOT ROIC and is never relabelled as such; it is a
        # more conservative denominator serving the same sanity-check purpose.
        roic_unavailable_reason = None
        if roic_incl_goodwill is None:
            roic_unavailable_reason = (
                "negative_invested_capital" if invested_capital <= 0 else "not_computable"
            )

        # --- Return on ASSETS: the proxy Greenblatt's DIY screen filters on ---
        # ROA is NOT Return on Capital. Same numerator, different denominator: ROC
        # divides by capital employed (working capital + net fixed assets only), ROA
        # divides by EVERY asset on the books — including cash, goodwill and acquired
        # intangibles. So ROA is always the lower, blunter figure, and a company can
        # look mediocre on ROA while ranking as elite on ROC. Greenblatt uses it in the
        # step-by-step appendix only because free screeners expose ROA and not ROC,
        # which is why the hurdle he sets (25%) is high. Ranking still uses ROC.
        #
        # Both bases are computed because they answer different questions and neither
        # costs a request: the EBIT basis keeps one numerator across ROC/EY/ROA, while
        # the net-income basis is what a retail screener would have shown his readers.
        roa_ebit = ebit / total_assets if total_assets > 0 else None
        roa_net_income = (
            net_income / total_assets
            if net_income is not None and total_assets > 0
            else None
        )

        # P/E on the same TTM window as EBIT. Derived as market cap / net income rather
        # than price / EPS: identical arithmetic, but it avoids depending on a share
        # count and a per-share figure that FMP reports inconsistently across filers.
        # Undefined (None) for loss-makers — a negative P/E is not a low P/E, and the
        # "P/E below 5" gate must not silently swallow them.
        pe_ratio = (
            live_market_cap / net_income
            if net_income and net_income > 0 and live_market_cap > 0
            else None
        )

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
            # Greenblatt's step-by-step proxies. Computed for EVERY company, gated
            # nowhere in here: main() applies the 25%-ROA / P/E-5 cuts to the screening
            # universe, while an on-demand single-ticker run still wants to SEE these
            # figures for a company the screen would have rejected.
            "NetIncome": net_income,
            "ROA": roa_ebit,
            "ROA_NetIncome": roa_net_income,
            "PE": pe_ratio,
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
            "ROIC_Unavailable_Reason": roic_unavailable_reason,
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


def selected_roa(metrics):
    """The ROA the 25% hurdle is measured against, per `roa_basis` in config.yaml."""
    if ROA_BASIS == "net_income":
        return metrics.get("ROA_NetIncome")
    return metrics.get("ROA")


def ratio_gate_reason(metrics):
    """Why a company fails Greenblatt's ROA / P/E gates, or None if it passes.

    Steps 1 and 2 of the step-by-step screen. Deliberately applied AFTER the metrics
    are computed (they need the financials) but BEFORE ranking, so a rejected company
    never influences the percentile ranks of the ones that survive.

    Missing data is not a failure: a company whose ROA or P/E could not be computed is
    kept, because dropping it would silently penalise a filer for a provider gap rather
    than for anything about the business.
    """
    if MIN_ROA is not None:
        roa = selected_roa(metrics)
        if roa is not None and roa < MIN_ROA:
            return "roa_below_min"

    if MIN_PE is not None:
        pe = metrics.get("PE")
        # Only a POSITIVE P/E below the floor is a rejection. Greenblatt's reason for
        # this cut is that a P/E that low usually means one-off earnings (an asset sale,
        # a settlement) rather than a genuine bargain — a loss-maker has no P/E at all
        # and is already excluded upstream by the negative-EBIT check.
        if pe is not None and 0 < pe < MIN_PE:
            return "pe_below_min"

    return None


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
    gate_counts = {}         # companies dropped by the ROA / P/E gates, by reason
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
            # Steps 1 & 2 of Greenblatt's screen. Dropped BEFORE ranking so a rejected
            # company cannot shift the percentile ranks of the ones that survive.
            gate = ratio_gate_reason(metrics)
            if gate:
                gate_counts[gate] = gate_counts.get(gate, 0) + 1
            else:
                metrics["Price"] = company.get("price", 0) or 0
                results.append(metrics)

        # Pause to easily stay within Starter Limit (300 calls/min = ~5 calls/sec)
        time.sleep(0.15)

        if (idx + 1) % 50 == 0:
            print(f"Processed {idx + 1}/{len(universe)} stocks... ({len(results)} valid so far)")

    if gate_counts:
        roa_label = "net income" if ROA_BASIS == "net_income" else "EBIT"
        print(
            f"\nGreenblatt gates: {gate_counts.get('roa_below_min', 0)} dropped for ROA "
            f"below {MIN_ROA:.0%} ({roa_label} / total assets), "
            f"{gate_counts.get('pe_below_min', 0)} dropped for a P/E below {MIN_PE:g}."
        )

    # Step 3: drop anything that reported earnings in the last week. Run over the
    # SURVIVORS only — the bulk calendar is one call, and the per-symbol fallback then
    # costs a few dozen requests instead of a few thousand.
    if RECENT_EARNINGS_DAYS and results:
        survivors = [m["Symbol"] for m in results]
        print(f"\nChecking which of the {len(survivors)} survivors reported earnings in "
              f"the last {RECENT_EARNINGS_DAYS} days...")
        recent_symbols, ok = fetch_recent_earnings_symbols(
            API_KEY, RECENT_EARNINGS_DAYS, survivors
        )
        if not ok:
            # Never let a failed lookup masquerade as "nobody reported recently".
            print("  WARNING: earnings dates could not be retrieved, so this filter was "
                  "NOT applied. The rankings may include companies that reported in the "
                  "last week.")
        else:
            before = len(results)
            results = [m for m in results if m["Symbol"] not in recent_symbols]
            dropped_recent = before - len(results)
            print(f"  Dropped {dropped_recent} company(ies) that reported within the "
                  f"window; {len(results)} remain.")

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
    # ROA and P/E are gate figures, not ranking figures — carried through so a reader
    # (and the final report) can see WHICH side of Greenblatt's 25% / P/E-5 cuts a
    # surviving company sits on. Missing values stay blank rather than becoming "nan%".
    df["ROA_Pct"] = (df["ROA"] * 100).round(2).map(
        lambda v: f"{v}%" if pd.notna(v) else ""
    )
    df["ROA_NetIncome_Pct"] = (df["ROA_NetIncome"] * 100).round(2).map(
        lambda v: f"{v}%" if pd.notna(v) else ""
    )
    df["PE_Ratio"] = df["PE"].round(2)

    output_df = df[[
        "Final_Rank", "Symbol", "CompanyName", "Live_Price",
        "LiveMarketCap", "ROC_Pct", "EY_Pct", "EBIT_Basis", "MagicFormula_Score",
        "ROC_Rank", "EY_Rank",
        # Greenblatt's step-by-step gate figures (see ratio_gate_reason).
        "ROA_Pct", "ROA_NetIncome_Pct", "PE_Ratio", "NetIncome",
        # Raw component dollar figures, kept alongside the Pct columns so the
        # final report can show the actual EY/ROC formula, not just the result.
        "EBIT", "CapitalEmployed", "EnterpriseValue",
        # Balance-sheet provenance + the goodwill-inclusive ROIC companion. Carried
        # through to the CSV so a --from-csv run gets the same authoritative figures
        # (and the same reconciliation gate) as an on-demand single-ticker run.
        "TotalDebt", "Cash", "TotalEquity", "TotalAssets", "GoodwillAndIntangibles",
        "InvestedCapital", "ROIC_InclGoodwill", "ROIC_Unavailable_Reason",
        "IntangiblesShareOfAssets",
        "BalanceSheetDate",
    ]]

    # Write a timestamped archive of this run, plus overwrite the stable "latest"
    # file that the MCP server / webapp always read.
    os.makedirs(HISTORY_DIR, exist_ok=True)
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_path = os.path.join(HISTORY_DIR, f"magic_formula_rankings_{run_stamp}.csv")
    output_df.to_csv(timestamped_path, index=False)
    output_df.to_csv(OUTPUT_FILENAME, index=False)
    # Greenblatt's 25% ROA hurdle is severe by design — on a sampled run it clears
    # roughly 95% of the universe on its own. That is the intent, but Phase B then
    # analyzes the top 30, so a run that leaves fewer than that is not a stricter
    # screen, it is a broken one. Say so rather than letting the pipeline quietly
    # analyze whatever handful survived.
    if len(output_df) < 30:
        print(f"\nWARNING: only {len(output_df)} companies cleared the screen, which is "
              f"fewer than the 30 the analysis phase expects. Consider lowering "
              f"`min_roa` or raising `universe_limit` in specs/config.yaml.")

    print(f"\nSuccess! Processed {len(output_df)} valid candidates.")
    print(f"Archived run : '{timestamped_path}'")
    print(f"Latest (stable): '{OUTPUT_FILENAME}'")

    print("\n--- TOP 10 LIVE MAGIC FORMULA CANDIDATES ---")
    print(output_df.head(10).to_string(index=False))

if __name__ == "__main__":
    main()