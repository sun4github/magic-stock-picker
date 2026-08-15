"""The reuse fingerprint: what makes two runs the same run.

A 30-ticker screen costs about $11, and several runs a day against the same filings
were paying that again for near-identical reports. `_analysis_key` is what stops
that: a hash of the inputs a report is produced from, so a second run against the
same filing and the same prompts reuses the first instead of re-buying it.

**`_prompt_version` is the delicate half.** It hashes every agent instruction, so any
prompt edit invalidates reuse — which is correct, since a tuned prompt must not keep
serving reports written under the old one, but it also means an accidental change
here re-analyzes 30 tickers. It reads `68b74f9c3e9a` and is expected to keep reading
it across any refactor that does not touch a prompt.
"""
import hashlib
import logging
import os

from agents import (
    PIPELINE_AGENTS,
    RECENCY_MANDATE,
    REFERENCE_DATA_BLOCKS,
    VERIFIED_FIGURES_MANDATE,
)

logger = logging.getLogger("InvestmentAgentPipeline")

# Paths resolved from this file used to resolve from src/, because this code lived
# in src/main.py. It now lives one directory down, so src/ is named explicitly.
#
# This is not cosmetic. `_read_prompt_files` below reads prompts/buy-case.md into the
# reuse fingerprint; a path that quietly misses would drop it from the hash, and the
# next --from-csv run would re-analyze all 30 tickers instead of reusing them.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# The buy-case advisor's prompt is not reachable from PIPELINE_AGENTS: its agent
# lives in `buy_case_agent`, which imports this module, so this module cannot import
# it back at load time. Its instruction FILE is right here on disk, though, and that
# is what actually changes when the prompt is tuned — so the fingerprint reads the
# file rather than the agent. Read once at import: it is per-ticker input to
# `_analysis_key` and re-reading it 30 times a run buys nothing.
def _read_prompt_files(*names) -> str:
    out = []
    for name in names:
        try:
            with open(os.path.join(_SRC_DIR, name), "r", encoding="utf-8") as fh:
                out.append(fh.read())
        except OSError as e:
            # Not fatal here — the module that owns these files fails loudly on its
            # own import if one is missing. All this costs is a fingerprint that no
            # longer moves when that prompt is edited, so say it once.
            logger.warning(f"Could not read {name} for the reuse fingerprint ({e}); "
                           f"edits to it will not invalidate reused reports.")
    return "".join(out)


_DOWNSTREAM_PROMPTS = _read_prompt_files(os.path.join("prompts", "buy-case.md"))


def _prompt_version() -> str:
    """Short hash of every agent instruction plus the shared prompt fragments.

    Any edit to a prompt changes this, which invalidates reuse — otherwise tuning
    the analyst's verdict stance would silently keep serving reports written under
    the old rules. The buy-case instructions are included via `_DOWNSTREAM_PROMPTS`
    for the same reason, since a reused report also reuses the buy case copied
    alongside it.

    `prompts/buy-check.md` is deliberately NOT included: nothing that command
    produces is stored under a reuse key, so editing it invalidates nothing."""
    blob = "".join(a.instruction for a in PIPELINE_AGENTS)
    blob += RECENCY_MANDATE + VERIFIED_FIGURES_MANDATE + REFERENCE_DATA_BLOCKS
    blob += _DOWNSTREAM_PROMPTS
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _analysis_key(ticker: str, candidate: dict, skip_sale_advisor: bool = False,
                  skip_buy_case: bool = False) -> str:
    """Fingerprint of the inputs a report is produced from.

    Deliberately built from the BALANCE SHEET DATE rather than today's date: two
    runs a day apart against the same filing are the same analysis. A new filing
    moves the date and forces a fresh run, as does any prompt edit.

    `skip_sale_advisor` is part of the fingerprint because a run without Phase C
    produces a STRICTLY SMALLER artifact — no SALE_CASE. Without this, a cheap
    screening run would be reused to satisfy a later full run, and
    `db_copy_ticker_outputs` would copy a set of outputs missing the sale advisory;
    the ticker would then appear complete in the web UI with an empty Sale tab, and
    `--sell-check` would find no conditions to test. Keeping the two variants under
    different keys means neither can be served in place of the other.

    `skip_buy_case` is in the fingerprint for exactly the same reason, one artifact
    along: a run made with it produces no BUY_CASE for a Watch, so reusing it to
    satisfy a full run would leave `--buy-check` with nothing to test. Note the
    asymmetry with Phase C — a run WITHOUT this flag still writes no buy case unless
    the verdict lands on Watch, which is a property of the analysis rather than of
    the flag, so the two variants genuinely differ only in what was ASKED for.

    Returns "" when the balance sheet date is unknown, which disables reuse for
    that ticker — better to pay again than to serve a report whose provenance we
    cannot establish."""
    bs_date = (candidate or {}).get("BalanceSheetDate")
    if not bs_date:
        return ""
    variant = "|no-phase-c" if skip_sale_advisor else ""
    variant += "|no-buy-case" if skip_buy_case else ""
    raw = f"{ticker}|{bs_date}|{_prompt_version()}{variant}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
