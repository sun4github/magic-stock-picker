# Backfill specs

The exact inputs used to enter historical verdicts into the ledger, kept so the
permanent rows they produced can be audited against the judgement that made
them. Both scripts are idempotent-by-refusal — they will not load a verdict or a
factor set twice — so these files are a record, not a thing to re-run casually.

| File | Loaded by | What it says |
| :--- | :--- | :--- |
| `2026-08-14-verdicts.json` | `src/backfill_pipeline_verdicts.py` | Which analysis run stands behind each of the first four real purchases |
| `2026-08-14-basis.json` | `src/load_reconstructed_basis.py` | Factors read back out of those runs' archived bear/bull cases |

## 2026-08-14 — the first four purchases

CROX, VNT, CRMD and WEX were bought before the verdict ledger recorded anything,
so the analyses behind them were absent from it.

**Run selection.** Each lot is linked to the latest run **on or before its
purchase date**, not the latest run overall. A ticker is often re-analysed after
it was bought, and a verdict reached later cannot be the one that caused the
purchase — VNT's most recent run at the time postdated its purchase by 11 days
and said WATCH, which would have recorded a verdict that never influenced
anything. Every run chosen here precedes its purchase and says BUY.

**The factors are reconstructed, not emitted.** No archived report from these
runs contains a `## Verdict Basis` block; the block did not exist yet. The
factors in `2026-08-14-basis.json` were read out of the `BEAR_CASE` and
`BULL_CASE` prose by hand and mapped onto the `verdict_ledger.factor_taxonomy`
vocabulary in `specs/config.yaml`. Two observations had no home in that
vocabulary and went in under the documented `OTHER_` prefix:
`OTHER_single_product_concentration` (CROX) and
`OTHER_regulatory_mandated_demand` (VNT).

That reconstruction carries a layer of interpretation that a natively-logged
basis does not, and it is worth remembering before reading an early factor
scorecard (Q3) as a finding about the prompt. These rows are identifiable:

```sql
SELECT b.* FROM verdict_basis b
  JOIN verdict_log v USING (verdict_id)
 WHERE v.prompt_version IS NULL;
```

`prompt_version` is NULL on every backfilled verdict because the prompt hash as
it stood on the day is not recoverable, and stamping today's would misattribute
these to the current system.
