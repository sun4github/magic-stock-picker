"""Backfill: give existing refinement runs the bear/bull rows they inherited.

A refinement reviews a finished report and never re-runs the bear and bull agents,
so those rows only ever existed under the run being reviewed. Every refinement run
therefore lists in the web UI with Bear Case and Bull Case tabs that load nothing.
`refine.py` now carries them forward at run time (`db_copy_inherited_outputs`);
this does the same for the runs that predate that change.

Idempotent: the copy skips any (run, ticker, agent_type) that already exists, so
running it twice adds nothing the second time. Embeddings are deliberately not
copied — the text is identical to the source run's and already indexed under it.

    python backfills/backfill_refinement_inherited_outputs.py --dry-run
    python backfills/backfill_refinement_inherited_outputs.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from persistence import db_copy_inherited_outputs, get_db_connection  # noqa: E402

INHERITED = ("BEAR_CASE", "BULL_CASE", "SEC_DATA", "QUANT_METRICS")


def refinements_missing_inherited():
    """[(refine_run_id, source_run_id, ticker, [missing types])] oldest first."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.run_id, r.refines_run_id, t.ticker
          FROM pipeline_runs r
          JOIN ticker_runs t ON t.run_id = r.run_id
         WHERE r.refines_run_id IS NOT NULL
         ORDER BY r.started_at
    """)
    rows = cur.fetchall()
    out = []
    for refine_id, src_id, ticker in rows:
        cur.execute("""SELECT agent_type FROM agent_outputs
                        WHERE run_id = %s AND ticker = %s AND agent_type = ANY(%s)""",
                    (refine_id, ticker, list(INHERITED)))
        have = {r[0] for r in cur.fetchall()}
        # Walk back to the run that actually holds these rows, exactly as
        # `db_copy_inherited_outputs` does. A refined report can itself be refined,
        # and the second refinement's immediate source is the first refinement —
        # which never had a bear case either. Looking only one hop back reported
        # such a run as "nothing to copy" when its grandparent had everything.
        origin = src_id
        for _ in range(10):
            cur.execute("""SELECT 1 FROM agent_outputs
                            WHERE run_id = %s AND ticker = %s AND agent_type = ANY(%s)
                            LIMIT 1""", (origin, ticker, list(INHERITED)))
            if cur.fetchone():
                break
            cur.execute("SELECT refines_run_id FROM pipeline_runs WHERE run_id = %s",
                        (origin,))
            nxt = cur.fetchone()
            if not nxt or not nxt[0]:
                break
            origin = str(nxt[0])
        src_id = origin

        # Only what the ORIGIN actually has is copyable; an old run may not carry
        # all four either, and reporting those as "missing" would be noise.
        cur.execute("""SELECT agent_type FROM agent_outputs
                        WHERE run_id = %s AND ticker = %s AND agent_type = ANY(%s)""",
                    (src_id, ticker, list(INHERITED)))
        available = {r[0] for r in cur.fetchall()}
        missing = sorted(available - have)
        if missing:
            out.append((str(refine_id), str(src_id), ticker, missing))
    cur.close()
    conn.close()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="list what would be copied")
    g.add_argument("--apply", action="store_true", help="perform the copy")
    args = ap.parse_args()

    work = refinements_missing_inherited()
    if not work:
        print("Nothing to do: every refinement run already has its inherited outputs.")
        return 0

    print(f"{len(work)} refinement run(s) missing inherited outputs:\n")
    for refine_id, src_id, ticker, missing in work:
        print(f"  {refine_id[:8]}  {ticker:<6} <- {src_id[:8]}  {', '.join(missing)}")

    if args.dry_run:
        print("\nDry run: nothing written. Re-run with --apply to copy.")
        return 0

    print()
    for refine_id, src_id, ticker, _ in work:
        print(f"  {refine_id[:8]} {ticker:<6} {db_copy_inherited_outputs(src_id, refine_id, ticker)}")

    left = refinements_missing_inherited()
    print(f"\nDone. {len(left)} run(s) still missing inherited outputs "
          f"({'expected 0' if not left else 'check the errors above'}).")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
