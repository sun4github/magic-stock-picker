"""Reclassify one-off analyses as ONDEMAND, and drop mirrors that double-count them.

Run once, after the commit that introduced `verdict_log.source = 'ONDEMAND'`.

    python src/backfills/backfill_ondemand_source.py            # dry run
    python src/backfills/backfill_ondemand_source.py --commit

WHY THIS EXISTS
---------------
`source` used to hold two values: PIPELINE for anything this system analysed,
and MANUAL for a purchase mirrored in from the position tracker. That put a
one-off `main.py TICKER` in the same bucket as a name the SCREEN chose, and the
two are not the same claim.

Only a screening run writes `run_universe` rows, and Q1 — "did filtering the
screen beat not filtering it" — measures the verdicts against exactly those
rows. So every one-off verdict inflated the approved side of Q1 out of a
denominator it was never in. On the history this was written against that was
not a rounding error: 24 of 38 PIPELINE verdicts came from one-off runs, and
only ONE run had ever screened, so Q1 was comparing 38 approvals against a
14-name universe.

TWO FIXES, AND WHY THE SECOND IS NARROWER THAN IT LOOKS
-------------------------------------------------------
1. RECLASSIFY. Every verdict whose run wrote no `run_universe` rows becomes
   ONDEMAND. That is the definition of "the screen never chose it", read off the
   data rather than guessed: a screening run always writes the universe before
   any agent runs (main.py), so its absence is proof, not inference.

   POST_CRITIC rows move with their INDEPENDENT row, by run, so a refined ticker
   never ends up with its two stages in different populations — Q4 pairs them.

2. UNMIRROR THE DOUBLE-COUNTED PURCHASE. A lot carrying a `run_id` was bought
   off an analysis that already wrote its own verdict. Mirroring it as well
   records ONE decision twice — once as the opinion, once as the purchase — and
   the dollar queries then count the cost basis twice.

   This only ever bit lots whose tracker source is not PIPELINE, because the
   mirror skips PIPELINE lots. In practice that is a very small set: a lot gets
   a run_id only when the operator types one in.

WHAT IT WILL NOT DO
-------------------
It never deletes a verdict that carries factors, a prompt_version or a git_sha —
those mark a real analysis, and no mirror has them. It only ever removes a
mirrored row that has been superseded by the analysis it duplicates. Anything
that fails that test is reported and left alone: a ledger is years of
accumulated marks that no re-run can rebuild.
"""
import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, "webapp", ".env"))

ANALYSIS_SOURCES = ("PIPELINE", "ONDEMAND")


def one_off_verdicts(cur) -> list:
    """PIPELINE verdicts whose run never wrote a run_universe row."""
    cur.execute("""
        SELECT v.verdict_id, v.ticker, v.verdict_date, v.verdict, v.verdict_stage, v.run_id
          FROM verdict_log v
         WHERE v.source = 'PIPELINE'
           AND v.run_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM run_universe u WHERE u.run_id = v.run_id)
         ORDER BY v.verdict_date, v.ticker
    """)
    return cur.fetchall()


def double_counted_mirrors(cur) -> list:
    """Mirrored purchases whose lot is already represented by an analysis verdict.

    Matched exactly the way sync_external_picks.py now matches: by the lot's
    run_id, which takes priority over the (ticker, purchase_date) natural key.

    The mirror is identified positively — no prompt_version, no git_sha, no
    factors — rather than by elimination, so a real analysis can never be caught
    by this even if it somehow shared a date with a purchase.
    """
    cur.execute("""
        SELECT m.verdict_id, m.ticker, m.verdict_date, m.price, m.source,
               a.verdict_id AS analysis_verdict_id, a.source AS analysis_source,
               a.price AS analysis_price,
               (SELECT COUNT(*) FROM verdict_outcome o WHERE o.verdict_id = m.verdict_id) AS marks
          FROM verdict_log m
          JOIN stock_tracker.lots l
            ON l.ticker = m.ticker
           AND l.purchase_date = m.verdict_date
           AND l.run_id IS NOT NULL
          JOIN verdict_log a
            ON a.run_id::text = l.run_id
           AND a.ticker = l.ticker
           AND a.source = ANY(%s)
           AND a.verdict_stage = 'INDEPENDENT'
         WHERE m.source <> ALL(%s)
           AND m.prompt_version IS NULL
           AND m.git_sha IS NULL
           AND NOT EXISTS (SELECT 1 FROM verdict_basis b WHERE b.verdict_id = m.verdict_id)
         ORDER BY m.verdict_date, m.ticker
    """, (list(ANALYSIS_SOURCES), list(ANALYSIS_SOURCES)))
    return cur.fetchall()


def has_position_tracker(cur) -> bool:
    cur.execute("SELECT to_regclass('stock_tracker.lots') IS NOT NULL")
    return bool(cur.fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reclassify one-off analyses as ONDEMAND and remove mirrors "
                    "that double-count them.")
    parser.add_argument("--commit", action="store_true",
                        help="Actually write. Without it this is a dry run.")
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set. Nothing done.")
        return 2

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    one_offs = one_off_verdicts(cur)
    print(f"\n1. RECLASSIFY -> ONDEMAND: {len(one_offs)} verdict(s) from runs that "
          f"never screened.\n")
    for _vid, ticker, vdate, verdict, stage, _run in one_offs:
        print(f"    {vdate}  {ticker:<6} {verdict:<5} {stage}")

    mirrors = []
    if has_position_tracker(cur):
        mirrors = double_counted_mirrors(cur)
        print(f"\n2. REMOVE DOUBLE-COUNTED MIRROR(S): {len(mirrors)}\n")
        for (_mid, ticker, vdate, price, src, _aid, asrc, aprice, marks) in mirrors:
            print(f"    {vdate}  {ticker:<6} mirror {src} @ {float(price):.2f} "
                  f"({marks} mark(s)) duplicates {asrc} @ {float(aprice):.2f}")
        if mirrors:
            print("\n    The lot carries the analysis run_id, so the purchase is already "
                  "\n    represented by the analysis verdict. Keeping both would count one "
                  "\n    decision twice, and its cost basis twice in Q10.")
    else:
        print("\n2. No stock_tracker.lots table here, so no mirrors to check.")

    if not one_offs and not mirrors:
        print("\nNothing to do.")
        cur.close(); conn.close()
        return 0

    if not args.commit:
        print("\nDRY RUN - nothing written. Re-run with --commit.")
        cur.close(); conn.close()
        return 0

    # Reclassify by RUN, so a run's INDEPENDENT and POST_CRITIC rows move
    # together and Q4 never pairs rows from two different populations.
    cur.execute("""
        UPDATE verdict_log v
           SET source = 'ONDEMAND'
         WHERE v.source = 'PIPELINE'
           AND v.run_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM run_universe u WHERE u.run_id = v.run_id)
    """)
    reclassified = cur.rowcount

    removed = 0
    for row in mirrors:
        # verdict_outcome and verdict_basis cascade off verdict_log.
        cur.execute("DELETE FROM verdict_log WHERE verdict_id = %s", (row[0],))
        removed += cur.rowcount

    conn.commit()
    cur.close()
    conn.close()
    print(f"\nDone. Reclassified {reclassified} verdict(s) as ONDEMAND; "
          f"removed {removed} double-counted mirror(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
