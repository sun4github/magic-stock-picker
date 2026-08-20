"""Backfill: re-embed every stored row after the Ollama experiment reverted.

Embeddings briefly ran through a local Ollama model (nomic-embed-text, then
snowflake-arctic-embed2) before being reverted back to Gemini's
gemini-embedding-001 -- see the "Revert embeddings from Ollama back to Gemini"
commit. Any row embedded during that window holds a vector from a different
model, in a different vector space, than everything embedded before or after
it. Cosine similarity between those and Gemini-embedded rows is meaningless,
so `db_search_historical_reports` would silently return noise for whatever
mix of rows it touches.

Rather than track which rows fall in that narrow window, this re-embeds
EVERYTHING with embed=True -- every `agent_outputs` row that already carries
an embedding (BEAR_CASE/BULL_CASE/SALE_CASE/BUY_CASE; SEC_DATA/QUANT_METRICS
are embed=False and stay untouched) and every `final_reports` row. Overwriting
an already-correct Gemini vector with a fresh Gemini vector is a no-op in
substance, so there is no need to distinguish "was this one actually stale."

    python backfills/backfill_reembed_gemini.py --dry-run
    python backfills/backfill_reembed_gemini.py --apply
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from persistence import get_db_connection  # noqa: E402
from persistence.embeddings import get_embedding  # noqa: E402

# Gentle pacing between embedding calls. Not chasing a documented RPM limit --
# just enough to avoid bursting hundreds of requests at once.
DELAY_SECONDS = 0.3


def _fetch_targets():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT output_id, raw_content FROM agent_outputs WHERE embedding IS NOT NULL ORDER BY created_at")
    outputs = cur.fetchall()
    cur.execute("SELECT report_id, markdown_report FROM final_reports ORDER BY created_at")
    reports = cur.fetchall()
    cur.close()
    conn.close()
    return outputs, reports


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="show what would be re-embedded")
    g.add_argument("--apply", action="store_true", help="re-embed and write every row")
    args = ap.parse_args()

    outputs, reports = _fetch_targets()
    total = len(outputs) + len(reports)
    print(f"{len(outputs)} agent_outputs row(s) + {len(reports)} final_reports row(s) "
          f"= {total} row(s) to re-embed.")

    if args.dry_run:
        print("\nDry run: nothing written. Re-run with --apply to re-embed.")
        return 0

    conn = get_db_connection()
    cur = conn.cursor()
    zero_vectors = 0
    done = 0

    for output_id, raw_content in outputs:
        embedding = get_embedding(raw_content)
        if all(v == 0.0 for v in embedding):
            zero_vectors += 1
        cur.execute("UPDATE agent_outputs SET embedding = %s WHERE output_id = %s",
                    (embedding, output_id))
        conn.commit()
        done += 1
        print(f"  [{done}/{total}] agent_outputs {output_id}")
        time.sleep(DELAY_SECONDS)

    for report_id, markdown_report in reports:
        embedding = get_embedding(markdown_report)
        if all(v == 0.0 for v in embedding):
            zero_vectors += 1
        cur.execute("UPDATE final_reports SET embedding = %s WHERE report_id = %s",
                    (embedding, report_id))
        conn.commit()
        done += 1
        print(f"  [{done}/{total}] final_reports {report_id}")
        time.sleep(DELAY_SECONDS)

    cur.close()
    conn.close()

    print(f"\nDone. {done} row(s) re-embedded.")
    if zero_vectors:
        print(f"WARNING: {zero_vectors} row(s) got a ZERO vector (embedding call failed "
              f"-- check the ERROR lines above). Those rows are stored but unsearchable.")
    return 0 if not zero_vectors else 1


if __name__ == "__main__":
    sys.exit(main())
