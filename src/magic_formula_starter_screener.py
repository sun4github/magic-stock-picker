import os
import time
import requests
import pandas as pd
import yaml
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
OUTPUT_FILENAME = "magic_formula_rankings_live.csv"

# Excluded Sectors per Joel Greenblatt
EXCLUDED_SECTORS = screening_params.get("excluded_sectors", [
    "Financial Services",
    "Financial",
    "Utilities",
    "Banking"
])

headers = {"User-Agent": "Mozilla/5.0"}

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
    res = requests.get(url, params=params, headers=headers)
    if res.status_code != 200:
        raise Exception(f"Screener request failed: {res.status_code}")
    
    data = res.json()
    valid_stocks = [
        item for item in data 
        if item.get("sector") not in EXCLUDED_SECTORS and item.get("symbol")
    ]
    print(f"Found {len(valid_stocks)} eligible companies.")
    return valid_stocks

def fetch_live_batch_quotes(symbols, api_key):
    """Fetches real-time prices & market caps in batches of 50 to save API calls."""
    quote_map = {}
    chunk_size = 50
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        symbols_str = ",".join(chunk)
        url = f"https://financialmodelingprep.com/stable/batch-quote?symbols={symbols_str}&apikey={api_key}"
        try:
            res = requests.get(url, headers=headers).json()
            if isinstance(res, list):
                for item in res:
                    quote_map[item["symbol"]] = {
                        "price": item.get("price", 0),
                        "marketCap": item.get("marketCap", 0)
                    }
        except Exception:
            pass
        time.sleep(0.1) # Soft pause
    return quote_map

def calculate_company_metrics(symbol, live_market_cap, api_key):
    """Fetches latest statements and calculates ROC and EY using Live Market Cap."""
    try:
        # Income Statement
        inc_url = "https://financialmodelingprep.com/stable/income-statement"
        inc_res = requests.get(inc_url, params={"symbol": symbol, "limit": 1, "apikey": api_key}, headers=headers).json()
        
        # Balance Sheet
        bal_url = "https://financialmodelingprep.com/stable/balance-sheet-statement"
        bal_res = requests.get(bal_url, params={"symbol": symbol, "limit": 1, "apikey": api_key}, headers=headers).json()
        
        if not isinstance(inc_res, list) or not isinstance(bal_res, list) or not inc_res or not bal_res:
            return None

        inc = inc_res[0]
        bal = bal_res[0]

        # 1. Operating Income (EBIT)
        ebit = inc.get("operatingIncome")
        if ebit is None or ebit <= 0:
            return None

        # 2. Balance Sheet Items
        current_assets = bal.get("totalCurrentAssets", 0) or 0
        current_liabilities = bal.get("totalCurrentLiabilities", 0) or 0
        nfa = bal.get("propertyPlantEquipmentNet", 0) or 0
        cash = bal.get("cashAndShortTermInvestments", 0) or 0
        total_debt = bal.get("totalDebt", 0) or 0

        # 3. Capital Employed = Net Working Capital + Net Fixed Assets
        nwc = current_assets - current_liabilities
        capital_employed = nwc + nfa
        if capital_employed <= 0:
            return None

        # 4. Live Enterprise Value = Live Market Cap + Debt - Cash
        enterprise_value = live_market_cap + total_debt - cash
        if enterprise_value <= 0:
            return None

        # 5. Ratios
        roc = ebit / capital_employed
        earnings_yield = ebit / enterprise_value

        return {
            "Symbol": symbol,
            "CompanyName": inc.get("companyName", symbol),
            "LiveMarketCap": live_market_cap,
            "EBIT": ebit,
            "CapitalEmployed": capital_employed,
            "EnterpriseValue": enterprise_value,
            "ROC": roc,
            "EarningsYield": earnings_yield
        }

    except Exception:
        return None

def main():
    if API_KEY == "YOUR_FMP_API_KEY_HERE":
        print("Error: Please insert your actual FMP API Key into the script.")
        return

    # Step 1: Universe
    universe = fetch_screener_universe(API_KEY, MIN_MARKET_CAP, UNIVERSE_LIMIT)
    symbols = [item["symbol"] for item in universe]

    # Step 2: Live Real-Time Quotes
    print("\nFetching Real-Time Prices and Market Caps in batches...")
    live_quotes = fetch_live_batch_quotes(symbols, API_KEY)

    # Step 3: Process Financial Statements
    results = []
    print("\nCalculating Magic Formula metrics across universe...")
    for idx, company in enumerate(universe):
        symbol = company["symbol"]
        
        # Use live market cap from batch quotes if available, else fallback to screener
        live_cap = live_quotes.get(symbol, {}).get("marketCap", company.get("marketCap", 0))
        
        metrics = calculate_company_metrics(symbol, live_cap, API_KEY)
        if metrics:
            metrics["Price"] = live_quotes.get(symbol, {}).get("price", company.get("price", 0))
            results.append(metrics)

        # Pause to easily stay within Starter Limit (300 calls/min = ~5 calls/sec)
        time.sleep(0.15)

        if (idx + 1) % 50 == 0:
            print(f"Processed {idx + 1}/{len(universe)} stocks... ({len(results)} valid so far)")

    df = pd.DataFrame(results)
    if df.empty:
        print("No valid companies calculated.")
        return

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
        "LiveMarketCap", "ROC_Pct", "EY_Pct", "MagicFormula_Score", 
        "ROC_Rank", "EY_Rank"
    ]]

    output_df.to_csv(OUTPUT_FILENAME, index=False)
    print(f"\nSuccess! Processed {len(output_df)} valid candidates.")
    print(f"Results exported to '{OUTPUT_FILENAME}'.")

    print("\n--- TOP 10 LIVE MAGIC FORMULA CANDIDATES ---")
    print(output_df.head(10).to_string(index=False))

if __name__ == "__main__":
    main()