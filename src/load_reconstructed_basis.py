"""Attach RECONSTRUCTED factors to a backfilled verdict.

Read this first: these rows are not what the analyst emitted.

The verdict ledger normally gets its factors from a `## Verdict Basis` block the
analyst writes into its own report. Runs from before that block existed have no
such section — the reasoning survives only as prose in the archived BEAR_CASE and
BULL_CASE outputs. This loads factors that were read back out of that prose and
mapped onto the controlled vocabulary by hand.

    python src/load_reconstructed_basis.py --spec basis.json
    python src/load_reconstructed_basis.py --spec basis.json --commit

WHY THE EXTRACTION IS NOT DONE BY THIS SCRIPT
---------------------------------------------
Deliberately split in two. The judgement — which factor a paragraph is really
making, and how material it is — is written into a JSON file that a person can
read, diff and correct. This script only validates and loads it. A script that
called a model and wrote straight to the database would put an unreviewed
interpretation into the permanent record with nothing to inspect afterwards.

HOW TO TELL THESE ROWS APART LATER
----------------------------------
`verdict_log.prompt_version IS NULL` marks every backfilled verdict, because the
prompt hash from the day is not recoverable. Any `verdict_basis` row hanging off
such a verdict is therefore reconstructed rather than emitted:

    SELECT b.* FROM verdict_basis b
      JOIN verdict_log v USING (verdict_id)
     WHERE v.prompt_version IS NULL;

That matters for Q3, the factor scorecard. These factors were selected by a
different reader than the one that wrote the analysis, so they carry an extra
layer of interpretation that natively-logged factors do not. Worth remembering
before treating an early Q3 result as a finding about the prompt.

WHAT IS GUARDED AGAINST
-----------------------
The one failure that would quietly ruin the scorecard is a factor_key that is
almost but not quite in the vocabulary — "margin_compressed" for
"margin_compression" — because it never aggregates with anything and simply
vanishes from every base rate. Keys are therefore checked against
specs/config.yaml, and anything unrecognised must already carry the OTHER_
prefix or the load is refused.
"""
import argparse
import json
import logging
import os
import sys

import psycopg2
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("basis_backfill")

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, "webapp", ".env"))

STANCES = ("BULL", "BEAR")
MATERIALITIES = ("HIGH", "MEDIUM", "LOW")


def taxonomy() -> set:
    path = os.path.join(SRC_ROOT, "specs", "config.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    keys = set((cfg.get("verdict_ledger") or {}).get("factor_taxonomy") or [])
    if not keys:
        raise SystemExit(f"No verdict_ledger.factor_taxonomy found in {path}; "
                         "refusing to load factors with nothing to validate against.")
    return keys


def validate(spec: list, vocab: set) -> list:
    """Reject the whole file on any bad row rather than loading it partially."""
    problems, total = [], 0
    for entry in spec:
        where = f"{entry.get('ticker')} / {str(entry.get('run_id'))[:8]}"
        if not entry.get("ticker") or not entry.get("run_id"):
            problems.append(f"{where}: needs both ticker and run_id")
            continue
        for f in entry.get("factors") or []:
            total += 1
            key = (f.get("factor_key") or "").strip()
            label = f"{where} [{key or '<blank>'}]"
            if not key:
                problems.append(f"{label}: factor_key is empty")
            elif key not in vocab and not key.startswith("OTHER_"):
                problems.append(
                    f"{label}: not in the taxonomy and not prefixed OTHER_. "
                    "A near-miss key aggregates with nothing and disappears from "
                    "every base rate, so it is refused rather than stored.")
            if (f.get("stance") or "").strip().upper() not in STANCES:
                problems.append(f"{label}: stance must be BULL or BEAR")
            if (f.get("materiality") or "").strip().upper() not in MATERIALITIES:
                problems.append(f"{label}: materiality must be HIGH, MEDIUM or LOW")
            if len((f.get("observation") or "").strip()) < 20:
                problems.append(f"{label}: observation is missing or too short to be useful")
    if problems:
        raise SystemExit("Refusing to load. Problems found:\n"
                         + "".join(f"    {p}\n" for p in problems))
    return total


def run(spec_path: str, commit: bool) -> dict:
    if not os.getenv("DATABASE_URL"):
        raise SystemExit("DATABASE_URL not set.")
    with open(spec_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    vocab = taxonomy()
    total = validate(spec, vocab)

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    plan = []
    for entry in spec:
        ticker, run_id = entry["ticker"].strip().upper(), entry["run_id"].strip()
        cur.execute(
            """SELECT verdict_id, verdict, verdict_date, prompt_version
                 FROM verdict_log
                WHERE ticker = %s AND run_id::text = %s AND source = 'PIPELINE'""",
            (ticker, run_id))
        rows = cur.fetchall()
        if len(rows) != 1:
            raise SystemExit(
                f"Expected exactly one PIPELINE verdict for {ticker} / {run_id[:8]}, "
                f"found {len(rows)}. Run backfill_pipeline_verdicts.py first.")
        verdict_id, verdict, verdict_date, prompt_version = rows[0]
        if prompt_version is not None:
            logger.warning(
                "%s / %s has prompt_version %s, so it was logged natively rather "
                "than backfilled. Attaching reconstructed factors to it would mix "
                "the analyst's own basis with a later reading of it.",
                ticker, run_id[:8], prompt_version)

        cur.execute("SELECT COUNT(*) FROM verdict_basis WHERE verdict_id = %s", (verdict_id,))
        existing = cur.fetchone()[0]
        if existing:
            raise SystemExit(
                f"{ticker} / {run_id[:8]} already has {existing} basis row(s). "
                "Delete them first if you mean to replace them; this script will "
                "not silently double them.")

        plan.append({"ticker": ticker, "verdict_id": verdict_id, "verdict": verdict,
                     "verdict_date": verdict_date, "factors": entry["factors"]})

    print(f"\n{total} reconstructed factor(s) across {len(plan)} verdict(s):\n")
    for p in plan:
        bull = sum(1 for f in p["factors"] if f["stance"].upper() == "BULL")
        bear = len(p["factors"]) - bull
        novel = [f["factor_key"] for f in p["factors"] if f["factor_key"] not in vocab]
        print(f"    {p['ticker']:<6} {p['verdict']:<6} {p['verdict_date']}  "
              f"{len(p['factors'])} factors ({bull} bull / {bear} bear)"
              + (f"   new: {', '.join(novel)}" if novel else ""))

    if not commit:
        print("\nDRY RUN - nothing written. Re-run with --commit.")
        cur.close(); conn.close()
        return {"factors": total, "written": 0}

    written = 0
    for p in plan:
        for f in p["factors"]:
            cur.execute(
                """INSERT INTO verdict_basis (verdict_id, factor_key, stance,
                                              materiality, observation)
                   VALUES (%s, %s, %s, %s, %s)""",
                (p["verdict_id"], f["factor_key"].strip(), f["stance"].strip().upper(),
                 f["materiality"].strip().upper(), f["observation"].strip()))
            written += 1
    conn.commit()
    cur.close()
    conn.close()
    return {"factors": total, "written": written}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load hand-reconstructed verdict_basis factors for backfilled verdicts.")
    parser.add_argument("--spec", required=True, help="JSON file of factors per ticker/run.")
    parser.add_argument("--commit", action="store_true",
                        help="Actually write. Without it this is a dry run.")
    args = parser.parse_args()
    logger.info("Reconstructed basis load: %s", run(args.spec, args.commit))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("Basis load failed with an unhandled exception.")
        sys.exit(1)
