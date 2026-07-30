# Screener Internals Walkthrough (Phase A)

Primary file:
[`src/magic_formula_starter_screener.py`](../src/magic_formula_starter_screener.py)
(879 lines). Runs as a plain script (`main()`) invoked either directly
(`python magic_formula_starter_screener.py`) or wrapped by
`run_magic_formula_screener` in `mcp_server.py:39`. Its output — a ranked CSV
— is the sole hand-off to Phase B; there is no in-memory sharing between this
script and `main.py` beyond that file (and, for a single ticker, the
`compute_company_metrics_detailed` function imported directly).

## Configuration load, `magic_formula_starter_screener.py:16-86`

Reads `screening_parameters:` from `src/specs/config.yaml` into module-level
constants (`MIN_MARKET_CAP`, `UNIVERSE_LIMIT`, `MIN_ROA`, `ROA_BASIS`,
`MIN_PE`, `RECENT_EARNINGS_DAYS`, `EXCLUDED_SECTORS`, `EXCLUDED_INDUSTRIES`,
`EXCLUDE_ADR`, `ALLOWED_COUNTRIES`). Any of the Greenblatt gate thresholds can
be set to `null` in `config.yaml` to disable that gate entirely — see the
checks at each gate site below.

`OUTPUT_FILENAME` (`:33`) and `HISTORY_DIR` (`:36`) are anchored to
`os.path.dirname(__file__)` rather than the current working directory —
important because `mcp_server.py` and `webapp/app.py` both need to find the
same stable file regardless of where the process was launched from.

## `fmp_get`, `magic_formula_starter_screener.py:97-138`

Shared HTTP helper (also used by `compute_company_metrics_detailed`). Retries
transient failures — timeouts, `RETRYABLE_STATUS = {429, 500, 502, 503,
504}`, and FMP's quirk of returning rate-limit/plan errors as an HTTP 200
carrying `{"Error Message": ...}` — with exponential backoff
(`HTTP_BACKOFF_BASE * 2**(attempt-1)`, default base 1.0s, 3 attempts). Raises
`FMPError` (a plain `Exception` subclass, `:89`) with a human-readable
message on exhaustion or a non-retryable 4xx.

## Eligibility gates, applied in this order

```mermaid
flowchart TD
    U["fetch_screener_universe()\n:184"] --> G1{"_universe_exclusion_reason()\n:157"}
    G1 -->|excluded_sector / fund_or_etf /\nexcluded_industry / foreign_adr| Drop1[dropped, 0 API cost]
    G1 -->|survives| Fin["compute_company_metrics_detailed()\n:360 — 1 quarterly + 1 balance-sheet call"]
    Fin --> G2{"ratio_gate_reason()\n:657"}
    G2 -->|roa_below_min| Drop2[dropped]
    G2 -->|pe_below_min| Drop3[dropped]
    G2 -->|passes or missing data| Keep["kept (missing ratio ≠ failure)"]
    Keep --> G3{"fetch_recent_earnings_symbols()\n:218, survivors only"}
    G3 -->|reported in window| Drop4[dropped]
    G3 -->|survives| Rank["ROC_Rank + EY_Rank\n:814-818"]
    Rank --> Out["Top-ranked CSV\nmagic_formula_rankings_live.csv"]
```

1. **Universe exclusions** — `_universe_exclusion_reason` (`:157-181`),
   applied inside `fetch_screener_universe` (`:184-216`) to the raw FMP
   screener response, **before any per-company financial statement is
   fetched**. Order of checks: missing symbol → `sector` in
   `EXCLUDED_SECTORS` → `isEtf`/`isFund` flags → `industry` substring match
   against `EXCLUDED_INDUSTRIES` (catches banks/insurers/REITs/SPACs filed
   under sectors not in the sector list) → `is_adr()` (`:141-154`, name
   markers like "ADR"/"ADS" or a non-US `country`; a **blank** country is
   treated as unknown, not foreign).
2. **ROA ≥ `min_roa`** and **P/E ≥ `min_pe`** — `ratio_gate_reason`
   (`:657-682`), called from `main()`'s per-symbol loop (`:734-742`) after
   `compute_company_metrics_detailed` runs, but **before** ranking. `roa_basis`
   (`config.yaml`) picks `ROA` (EBIT basis) or `ROA_NetIncome` via
   `selected_roa` (`:650-654`). A P/E gate only fires on a **positive** P/E
   below the floor — loss-makers have no P/E and are already excluded by the
   negative-EBIT check inside `compute_company_metrics_detailed`. Missing
   ratios never cause a drop (a provider gap ≠ a business failing the test).
3. **No earnings in the last N days** — `fetch_recent_earnings_symbols`
   (`:218-314`), run once over all survivors (`main()` `:761-778`). Prefers
   one bulk `/stable/earnings-calendar` call; falls back to per-symbol
   `/stable/earnings` only if that endpoint is unavailable, and only for the
   already-narrowed survivor list. If **neither path works**, the filter is
   reported as **not applied** (`ok=False`) rather than silently passing
   everyone through.

## Metrics computation, `compute_company_metrics_detailed`, `:360-648`

The core per-company math (called once per survivor of gate 1, and directly
by `compute_ticker_magic_metrics` in `mcp_server.py:60` for on-demand
tickers). Returns `{"ok": False, "reason": ..., "message": ...}` on any
disqualifying condition instead of raising, via the `_skip()` helper
(`:353-359`), so callers can render *why* a company or ticker has no ratios.

Key formulas:

- **EBIT** — TTM (sum of last 4 quarters' `operatingIncome`) preferred;
  falls back to the latest annual filing if quarterly data is short
  (`EBIT_Basis` field records which). Rejects `ebit <= 0` outright
  (`reason="negative_ebit"`) since both ratios divide by it.
- **Capital Employed** = net working capital (with an *excess-cash*
  carve-out, floored at 0 — see the long comment at `:494-510` for why this
  keeps asset-light net-cash companies like ADBE from being wrongly dropped
  or inflated) **+ net fixed assets** (`propertyPlantEquipmentNet`).
- **Enterprise Value** = market cap + total debt + preferred + minority
  interest − cash. Rejected if ≤ 0 (`reason="negative_enterprise_value"`).
- **ROC** = EBIT / Capital Employed. **Earnings Yield** = EBIT / Enterprise Value.
  These two, ranked, are the actual Magic Formula.
- **ROIC_InclGoodwill** = EBIT / (equity + debt + minority − cash)
  ("invested capital", includes goodwill/intangibles unlike ROC). `None`
  when invested capital is non-positive (heavy-buyback companies like BKNG,
  WINA) — the reason (`negative_invested_capital` / `not_computable`) is
  carried in `ROIC_Unavailable_Reason` rather than left unexplained.
- **ROA** (two bases) = EBIT / TotalAssets, or NetIncome / TotalAssets. This
  is the gate figure (step 1 above), never the ranking figure.
- **P/E** = market cap / TTM net income (avoids a per-share/share-count
  dependency); `None` for non-positive net income.

## Ranking & output, `main()`, `:697-878`

1. Fetch universe → per-company metrics → gates 1–3 (above), printing gate
   drop counts as it goes.
2. **Data-basis integrity check** (`:798-810`): warns if ≥5% of the surviving
   universe fell back to `Annual` EBIT instead of `TTM` — a signal that FMP
   may have restricted quarterly access, which would otherwise silently mix
   TTM and annual bases in one ranking.
3. **Ranking** (`:814-818`): `ROC_Rank` and `EY_Rank` are independent
   percentile ranks (`ascending=False`, ties get `method="min"`);
   `MagicFormula_Score = ROC_Rank + EY_Rank`; `Final_Rank` is the rank of
   that score (lower score = better rank = higher on the list).
4. **Output** (`:837-874`): writes a timestamped archive to
   `rankings_history/` plus overwrites the stable `magic_formula_rankings_live.csv`
   (`OUTPUT_FILENAME`). Warns if fewer than 30 companies survive — Phase B
   expects a top 30, so a short list is a signal the gates are mistuned, not
   a stricter screen working as intended.

## Where to look next

- How `main.py` consumes this CSV (`--from-csv`) or calls the screener
  directly (`run_orchestrator`): [01-orchestration-and-cli.md](01-orchestration-and-cli.md).
- The two different shapes a "candidate" dict takes depending on whether it
  came from this CSV or from `compute_ticker_magic_metrics`, and how
  `_normalize_candidate` reconciles them:
  [05-guardrails-cost-and-reuse.md](05-guardrails-cost-and-reuse.md).
