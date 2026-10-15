## What MSP is today (the flow to visualize)

MSP run (every ~2 months)
  1. Screen universe        → Magic Formula / PEG filters
  2. Deep-dive each pass    → BUY_CASE / BULL / BEAR / SALE_CASE agents
  3. Scoring + verdict      → BUY / WATCH / PASS per ticker
  4. Final report           → what we download to knowledge-base
The verdict at step 3 is fundamental-only. It has no idea where price sits relative to anything — no 52-week range, no moving averages, no trend state. All the price/timing gates currently live outside MSP (in your buy-check layer with me). That works, but it means MSP emits "BUY" and we independently decide "not yet" — you carry the override logic manually.

The plan: make momentum a first-class gate inside step 3

## Change 1 — Add a Price/Trend module to the scoring stage.
Before a verdict is finalized, MSP computes for each candidate: price vs 50dma, price vs 200dma, 200dma slope (rising/falling over ~20 sessions), 50dma vs 200dma (golden/death cross), position in 52-week range. All of this is one EOD history call per ticker — no new data vendor needed.

## Change 2 — Verdicts become gated, not just scored.
The fundamental verdict is computed as today. Then the momentum gate re-labels it:

BUY → stays BUY only if: ≤90% of 52wk range AND not parabolic (>20% above 50dma) AND not falling knife (>20% below a falling 200dma).
BUY + parabolic or >90% of range → demote to BUY-ON-PULLBACK (the verdict you wish you'd had for CROX and WEX).
BUY + falling knife → demote to WATCH (cheap but freefalling — exactly the DECK/HRB pattern).
WATCH → stays WATCH; optionally escalate to PASS if also in a falling knife (don't even monitor).
This maps 1:1 to your existing external gates, so behavior is consistent — it just moves the check before the report is written instead of after.

## Change 3 — Keep the gate OUTSIDE the fundamental score.
This is the important design decision. Don't blend momentum into the Magic Formula score itself. Two reasons:

The audit lesson: your value screen deliberately finds stocks below their 200dma (that's why they're cheap). If momentum contaminates the ranking, you won't get value picks at all — you'll just get momentum stocks.
Separation preserves auditability. The report should show "Fundamental verdict: BUY | Momentum gate: FALLING KNIFE → demoted to WATCH" — so a future one-month audit can tell whether you were wrong about the business or wrong about the entry, exactly like the 10-13 audit did.

## Change 4 — Two profiles, mode set at run time.

**value** profile for pipeline runs (the demotion rules above).
A future trend profile mode if you ever run MSP for MANUAL-style growth names (requiring price > rising 50dma + golden cross instead).

## Change 5 — Emit gate data in the report.
Add a small section to the final report: 50dma, 200dma, slope, cross state, % of 52wk range. Then knowledge-base entries and any future audit have the entry regime on record permanently.