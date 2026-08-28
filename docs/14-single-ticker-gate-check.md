# Code Walkthrough — `check_ticker_screen_gates`

**Branches:**
- `feature/single-ticker-screener-check` — the core function (agents' toolbelt)
- `feature/operator-gate-check` — exposes it on the operator MCP server

**Files changed:**
- `src/data/screener.py` — new MCP tool `check_ticker_screen_gates` (+ 3 imports)  *(agents' layer)*
- `src/data/__init__.py` — export the new tool  *(agents' layer)*
- `src/mcp_server.py` — re-export the function  *(agents' layer)*
- `src/main.py` — new `--gate-check TICKER` CLI mode  *(bridge)*
- `agent_mcp/server.py` — operator MCP tool that shells out to the CLI  *(operator layer)*

## What this tool does

Checks whether a **single ticker** passes the Magic Formula screen's eligibility
gates, without running the whole-universe screen. Returns a verdict of `PASS` or
`FAIL` with the exact gate that rejected it.

This is the on-demand counterpart to `run_magic_formula_screener` (the full
universe screen). It answers: *"would this name have made the rankings?"* — and
if not, *"which gate stopped it?"* — for ~4 FMP calls instead of a 20–30 min
universe run.

## Why it exists

The full screen (`start_screen` / `run_magic_formula_screener`) only runs over
the whole universe. There was no way to test one candidate against the gates
without either (a) running the full screen, or (b) hand-computing the ratios
outside the screener's own logic — which drifts. This tool reuses the screener's
**own** gate functions, so it cannot disagree with the real screen.

## The gate sequence (same order as the screener)

The tool applies the gates in exactly the order `main()` in
`magic_formula_starter_screener.py` does:

| Step | Gate | Function | Cost |
| :--- | :--- | :--- | :--- |
| 1 | Universe exclusion (sector / industry / ADR / fund-ETF) | `_universe_exclusion_reason()` | 1 profile call |
| 2 | Greenblatt's ROA & P/E cuts | `ratio_gate_reason()` | 2 statement calls (income + balance) |
| 3 | Lynch's PEG / growth cuts | `growth_gate_reason()` | 1 extra call (EPS history) |

### Step 1 — Universe exclusion

Fetches the company profile (`/stable/profile`) and builds a company dict shaped
like a screener-universe row (`symbol`, `companyName`, `sector`, `industry`,
`country`, `isEtf`, `isFund`), then calls `_universe_exclusion_reason()`.

Rejection reasons: `no_symbol`, `excluded_sector`, `fund_or_etf`,
`excluded_industry`, `foreign_adr`. A rejection here costs **no statement calls**
— same as the full screen, which drops these before fetching financials.

### Step 2 — Live market cap

Uses `/stable/market-capitalization` (the Starter-plan-safe endpoint, same as
`compute_ticker_magic_metrics` — `/stable/batch-quote` is restricted on Starter).

### Step 3 — Magic Formula metrics

`compute_company_metrics_detailed(ticker, live_cap, api_key)` computes ROC,
Earnings Yield, ROA (EBIT basis, per `roa_basis` in config), ROA (net income),
and P/E — the same function the full screen uses per company.

If it returns `ok: False`, the tool reports `FAIL` with `Gate:
metrics_unavailable` and the plain-English `reason`/`message` (e.g. negative
EBIT, stale statements).

### Step 4 — Greenblatt's ROA / P/E gates

`ratio_gate_reason(metrics)`:
- **ROA below `min_roa`** (0.20 in config, EBIT basis) → `roa_below_min`
- **P/E below `min_pe`** (5.0) but positive → `pe_below_min`

Missing data is **not** a rejection here (matches the screener: a provider gap
must not silently penalise a filer).

### Step 5 — Lynch's PEG / growth gates

`attach_growth_metrics()` fetches EPS history and computes the 3-year compound
growth + PEG (with the `max_growth_rate_for_peg` cap), then
`growth_gate_reason(metrics)`:
- `growth_unavailable` / `no_eps_growth` (EPS not growing)
- `base_year_breakeven` (base window net margin < `min_base_net_margin`)
- `peg_unavailable` / `peg_above_max` (PEG > 1.5)

Unlike the ratio gates, **missing data IS a rejection** here — deliberate, and
identical to the full screen (a survivor without a PEG cannot be ranked).

## Output shape

**PASS:**
```json
{
  "Symbol": "DY", "CompanyName": "Dycom Industries, Inc.",
  "Verdict": "PASS", "Gate": null, "Reason": null,
  "Message": "Passes all Magic Formula screen gates.",
  "ROA_Pct": 0.1231, "PE_Ratio": 27.44,
  "EPSGrowth": 0.4988, "PEG": 0.55,
  "ROC_Pct": 0.3584, "EarningsYield_Pct": 0.0685
}
```

**FAIL** (each gate returns the same shape with `Verdict: FAIL`, the failing
`Gate` bucket, the stable `Reason` slug, and the metrics relevant to that gate).

## Design decisions

1. **No duplicated gate logic.** The three gate functions are imported from
   `magic_formula_starter_screener` and called as-is. If the config thresholds
   change (`min_roa`, `max_peg`, …), this tool follows automatically.
2. **Same order, same semantics.** The tool mirrors `main()`'s gate order
   (universe → ratio → growth), including the "growth costs an extra request so
   it runs only on ratio survivors" behaviour.
3. **Reported, not enforced, elsewhere.** `compute_ticker_magic_metrics` (the
   pre-existing single-ticker tool) deliberately *reports* the gate figures but
   never enforces them, because an on-demand analysis is about one named company.
   This new tool is the *enforcing* counterpart: it exists precisely to say
   PASS/FAIL against the screen.
4. **Starter-plan-safe endpoints.** Profile, market-cap, income statement,
   balance sheet, EPS history — all available on the FMP Starter plan, with the
   same `fmp_get` retry/timeout wrapper and 0.20 s rate-limit sleeps the other
   tools use.

## How to test

Offline (gate logic only, no network):
```bash
python test_screen_gates.py
```

Live (against the MCP server, after deploy):
```json
{"name": "check_ticker_screen_gates", "arguments": {"ticker": "DY"}}
```
Expected: `FAIL` / `ratio` / `roa_below_min` (DY's EBIT ROA ≈ 12.3% < 20%).

CLI (needs the full interpreter with pandas):
```bash
python src/main.py --gate-check MO     # → PASS with metrics
python src/main.py --gate-check DY     # → FAIL, Gate=ratio, Reason=roa_below_min
```

## Deploying

1. Merge/pull this branch on the Windows host (`E:\repos\magic-stock-picker`).
2. Restart the MCP server (`python tools/mcp_service.py restart`, or the
   Startup-folder launcher) — new `@mcp.tool()` registrations only appear after
   a restart.
3. Verify with `tools/snapshot_tool_surface.py` (the full server's tool count goes 33 → 34).

## Operator-server exposure (branch `feature/operator-gate-check`)

The core `check_ticker_screen_gates` function lives in the **agents' toolbelt**
(`src/data/screener.py`), which the operator server at port 8765 does NOT expose
— it has a separate registry (`agent_mcp/server.py`) and a deliberately thin
environment (no pandas/google-adk). To make the gate-check callable by external
clients (an agent like OpenClaw dialling 8765), this branch adds:

1. **`src/main.py --gate-check TICKER`** — a CLI mode that calls the core function
   and prints the result as JSON. It goes through `main.py` because that is the
   pipeline entry point the operator layer already drives; `main.py` imports the
   full agent stack (it already does, for every other mode).
2. **`agent_mcp/server.py` → `check_ticker_screen_gates(ticker)`** — an operator
   MCP tool that shells out to the CLI synchronously (the check is a few FMP
   calls, seconds, no LLM, no money) and parses the JSON. It uses
   `jobs._interpreter()` / `MSP_PYTHON` so it runs under the heavy interpreter
   that has pandas, not the thin operator env.
3. **`src/mcp_server.py`** — re-exports the function so `main.py` can import it.

**Why subprocess and not a direct import in the operator tool:** the operator
server deliberately avoids importing the pipeline (no pandas in
`agent_mcp/requirements.txt`), and the codebase's stated rule is that the
operator layer reaches the pipeline only via subprocess (see `docs/13-mcp-server.md`).

**Verify the operator tool is registered** (after deploy + restart):
```bash
# tools/list should now show 22 tools including check_ticker_screen_gates
```
