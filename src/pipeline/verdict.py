"""Reading a finished report: its verdict, its machine-readable tail, its stamps.

What the analyst writes is prose with a verdict buried in it and a JSON block on the
end. This module turns that back into data — the verdict word, the factor list for
the ledger — and adds the provenance a stored report needs: which run wrote it, and
against which commit and benchmark level.

`_split_verdict_basis` STRIPS the JSON tail before the report is stored. No reader
ever sees it: the report is written for someone with no finance background, and a
JSON dump at the end would be noise to every human who opens it.
"""
import json
import logging
import os
import re
import subprocess
from datetime import datetime

from mcp_server import fmp_price_snapshot
from settings import LEDGER_BENCHMARK, LEDGER_FACTOR_TAXONOMY

logger = logging.getLogger("InvestmentAgentPipeline")

# Paths resolved from this file used to resolve from src/, because this code lived
# in src/main.py. It now lives one directory down, so src/ is named explicitly.
#
# This is not cosmetic. `_read_prompt_files` below reads prompts/buy-case.md into the
# reuse fingerprint; a path that quietly misses would drop it from the hash, and the
# next --from-csv run would re-analyze all 30 tickers instead of reusing them.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- 3. PHASE B: PER-TICKER ANALYSIS (shared by full + on-demand runs) ---
def _with_run_header(md: str, run_id: str, ticker: str) -> str:
    """Stamp the run_id onto a stored report so it is visible (and copyable) at the
    top of the web viewer. When recording a purchase, this run_id is what you save
    against the lot so the sell-condition check can pin the exact thesis you bought
    under (`--run RUN_ID`; see specs/agent_architecture.md §8.D). Rendered as a
    blockquote at the very top; it does not disturb '## ...' section parsing."""
    if not md:
        return md
    banner = f"> **Run ID:** `{run_id}`  \n> **Ticker:** {ticker}\n\n"
    return banner + md


def _extract_verdict(report_text: str) -> str:
    """Extract BUY/WATCH/AVOID from the report's '## Final Verdict' section.

    Anchors on the explicit 'Verdict: X' declaration rather than a naive
    substring search — the justification prose often mentions the word 'buy'
    when weighing the bull case, which would misclassify the verdict.
    """
    section = report_text.split("## Final Verdict")[-1]
    m = re.search(r"verdict[^A-Za-z]{0,12}(buy|watch|avoid)", section, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    lowered = section.lower()
    positions = {w: lowered.find(w) for w in ("buy", "watch", "avoid") if lowered.find(w) != -1}
    return min(positions, key=positions.get).upper() if positions else "WATCH"


# Matches the '## Verdict Basis' section and everything after it. Anchored to the
# heading rather than to the fence so a model that emits stray prose around the
# block still gets the whole tail removed from the reader-facing report.
_VERDICT_BASIS_RE = re.compile(r"\n#{1,6}\s*Verdict Basis\b.*", re.IGNORECASE | re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


def _split_verdict_basis(report_text: str, ticker: str = "") -> tuple:
    """Split the analyst's report into (reader-facing report, basis factor list).

    The basis block is machine-only, so it is removed from the text that gets
    stored, rendered and shown: the report is written for a reader with no
    finance background, and a JSON array at the foot of it is noise to every
    human who opens it. Stripping happens here, before the reconciliation gate
    and the critic see the text, so no downstream consumer has to know the
    section ever existed.

    A missing or malformed block costs the ledger one row and nothing else — it
    never fails the analysis. The report is the product; the bookkeeping is not.
    """
    if not report_text:
        return report_text, []
    m = _VERDICT_BASIS_RE.search(report_text)
    if not m:
        logger.warning(f"[{ticker}] No '## Verdict Basis' section in the analyst's "
                       f"report; this verdict will be logged with no factors.")
        return report_text, []

    tail, cleaned = m.group(0), report_text[:m.start()].rstrip() + "\n"
    fence = _JSON_FENCE_RE.search(tail)
    raw = fence.group(1) if fence else None
    if raw is None:
        # Fall back to the outermost bracket pair: models occasionally emit the
        # array without fencing it, and the data is perfectly good when they do.
        start, end = tail.find("["), tail.rfind("]")
        raw = tail[start:end + 1] if 0 <= start < end else None
    if raw is None:
        logger.warning(f"[{ticker}] Verdict Basis section contained no JSON array.")
        return cleaned, []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[{ticker}] Could not parse the Verdict Basis JSON ({e}); "
                       f"this verdict will be logged with no factors.")
        return cleaned, []
    if not isinstance(parsed, list):
        logger.warning(f"[{ticker}] Verdict Basis JSON was not an array.")
        return cleaned, []

    known = set(LEDGER_FACTOR_TAXONOMY)
    factors, off_vocabulary = [], []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        key = str(item.get("factor_key") or "").strip()
        if not key:
            continue
        # An unrecognised key is KEPT, not dropped. It is recorded as given so
        # the monthly taxonomy review can see what the analyst reached for; a
        # dropped observation is unrecoverable, an untidy key is a rename.
        if key not in known and not key.upper().startswith("OTHER_"):
            off_vocabulary.append(key)
            key = f"OTHER_{key}"
        factors.append({
            "factor_key": key,
            "stance": str(item.get("stance") or "").strip().upper(),
            "materiality": str(item.get("materiality") or "").strip().upper(),
            "observation": str(item.get("observation") or "").strip(),
        })
    if off_vocabulary:
        logger.info(f"[{ticker}] Verdict basis used {len(off_vocabulary)} key(s) outside "
                    f"the taxonomy, stored under OTHER_: {', '.join(off_vocabulary)}")
    return cleaned, factors


_BENCHMARK_CACHE = {}


def _benchmark_price() -> float:
    """Today's benchmark level, fetched once per process and reused.

    Cached by date rather than fetched per ticker: a 30-ticker run would
    otherwise make 30 identical quote calls, and every verdict in that run would
    be baselined against a slightly different intraday level for no reason. The
    benchmark is what a verdict's excess return is measured against years later,
    so it is captured now — recovering a historical index level for an arbitrary
    past date later means trusting a provider to still serve it.

    Returns 0.0 on failure, which db_log_verdict stores as NULL. A verdict with
    no baseline is still gradeable on absolute return; refusing to log it would
    lose the observation entirely."""
    today = datetime.now().date().isoformat()
    if _BENCHMARK_CACHE.get("date") == today:
        return _BENCHMARK_CACHE.get("price", 0.0)
    price = 0.0
    try:
        snap = json.loads(fmp_price_snapshot(LEDGER_BENCHMARK))
        price = float(snap.get("price") or 0.0)
    except Exception as e:
        logger.warning(f"Could not fetch the {LEDGER_BENCHMARK} benchmark price ({e}); "
                       f"verdicts this run will be logged without a baseline.")
    _BENCHMARK_CACHE.update({"date": today, "price": price})
    return price


def _git_sha() -> str:
    """Short commit SHA of the working tree, or "" outside a checkout.

    Stored beside `_prompt_version()` on every logged verdict. The hash says two
    prompt versions differ; the commit says HOW they differ, which is the part a
    human needs when the track record splits either side of an edit."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_SRC_DIR,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()[:40] if out.returncode == 0 else ""
    except Exception:
        return ""
