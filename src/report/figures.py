"""Candidate-dict normalisation and the small figure helpers the report is built from.

The `_Pct` display fields, the growth-window prose, and the plain-English reason a
PEG could not be computed — everything that turns a raw screener row into figures
fit to render, without rendering any of it.

Extracted verbatim from main.py, where this lived among the pipeline orchestration
it has nothing to do with. The bodies are unchanged — every one of these functions
is judged by the exact bytes it emits (stored in the database, injected into agent
prompts, hashed into the reuse fingerprint), so the move was verified by diffing
`tools/snapshot_report_surface.py` output before and after, not by inspection.

Names keep their leading underscore. They are private to the report layer by
intent, but `refine`, `buy_case`, `buy_case_agent`, `sale_advisory` and four test
modules call them as `main._name`, and renaming them here would have meant editing
~40 call sites in the same change that moved the code — losing the byte-identical
guarantee that makes the move checkable. `main` re-exports them under their old
names; the renaming, if it happens, is its own reviewable step.
"""
import pandas as pd


def _present(value):
    """Return `value` if it is a real, displayable figure, else None.

    Candidate dicts reach this module two ways: straight from the screener (missing
    fields are None) and via pandas in --from-csv mode (missing fields are NaN, which
    is TRUTHY and formats as the string 'nan'). Everything rendered into a report has
    to survive both paths, so normalise here rather than at each call site."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "none"):
        return None
    return value


# Ratio fields the CSV stores raw, mapped to the percent-string key the report and
# VERIFIED_FIGURES blocks actually read, with the decimal places each is shown at.
_RATIO_TO_PCT_FIELD = {
    "ROIC_InclGoodwill": ("ROIC_InclGoodwill_Pct", 2),
    "IntangiblesShareOfAssets": ("IntangiblesShareOfAssets_Pct", 1),
    "ROA": ("ROA_Pct", 2),
    "ROA_NetIncome": ("ROA_NetIncome_Pct", 2),
    # Lynch's EPS growth rate. PEG itself is NOT here: it is a plain multiple, not a
    # percentage, and both paths already emit it as `PEG_Ratio`.
    "EPSGrowth": ("EPSGrowth_Pct", 2),
    "BaseNetMargin": ("BaseNetMargin_Pct", 2),
}


def _normalize_candidate(candidate: dict) -> dict:
    """Fill in the `*_Pct` display fields from their raw ratio columns.

    Candidates reach the report two ways and they do NOT agree on key names. The
    single-ticker path (`compute_ticker_magic_metrics`) emits pre-formatted percent
    strings like `ROIC_InclGoodwill_Pct`; the screener CSV stores the same figures as
    raw ratios under `ROIC_InclGoodwill`. Both report formatters only ever read the
    `_Pct` form.

    The consequence was silent and total: in the full-pipeline and `--from-csv` modes
    — every batch run — `ROIC_InclGoodwill_Pct` was simply absent, so the
    goodwill-inclusive companion was skipped in both the reader-facing '## Magic
    Formula Metrics' section AND the agent-facing VERIFIED_FIGURES block. The §2.F
    guard that exists specifically to stop a four-digit ROC being read as operating
    efficiency was therefore doing nothing on batch runs, which is where it matters.

    Normalising here rather than renaming the CSV columns keeps old archived CSVs
    readable — a `--from-csv` run against a previously archived file still works.
    """
    if not candidate:
        return candidate or {}
    out = dict(candidate)
    for raw_key, (pct_key, places) in _RATIO_TO_PCT_FIELD.items():
        if _present(out.get(pct_key)) is not None:
            continue  # already supplied in display form
        raw = _present(out.get(raw_key))
        if isinstance(raw, (int, float)):
            out[pct_key] = f"{round(float(raw) * 100, places)}%"
    return out


def _eps_window_phrase(candidate: dict) -> str:
    """Plain-English description of the two figures the growth rate came from.

    The wording has to follow the measurement. Under the "sums" method the figures
    are MULTI-YEAR TOTALS, and describing them as "earnings per share in the year
    ended X" would be a plain misstatement of what was divided — the exact class of
    small false claim the verified-figures block exists to prevent."""
    candidate = candidate or {}
    base = _present(candidate.get("EPS_Base"))
    cur = _present(candidate.get("EPS_Current"))
    base_date = _present(candidate.get("EPS_Base_Date")) or "not available"
    cur_date = _present(candidate.get("EPS_Current_Date")) or "not available"
    base_s = base if base is not None else "not available"
    cur_s = cur if cur is not None else "not available"
    if _present(candidate.get("EPS_Window")) == "sums":
        span = _present(candidate.get("EPS_Window_Years")) or 3
        return (f"total earnings per share of {base_s} across the {span} years up to "
                f"{base_date}, compared with {cur_s} across the {span} years up to "
                f"{cur_date}. Adding up whole periods rather than picking two single "
                f"years means a bad year in the middle is counted, not skipped over")
    return (f"earnings per share of {base_s} in the year ended {base_date}, compared "
            f"with {cur_s} in the year ended {cur_date}")


def _growth_window(candidate: dict):
    """The growth window's length, formatted for prose ('3', not '3.0').

    A whole number of fiscal years is the normal case and must read as one; a company
    that changed its fiscal year end genuinely has a fractional window and keeps it."""
    years = _present((candidate or {}).get("EPSGrowth_Years"))
    if not isinstance(years, (int, float)):
        return "3"
    return str(int(years)) if float(years).is_integer() else f"{float(years):.2f}"


def _no_peg_reason(candidate: dict) -> str:
    """Plain-English cause when no PEG could be computed, or "" if one was.

    Three distinct causes, and conflating them misinforms in opposite directions. The
    growth rate may be MISSING (its own set of reasons, carried on the candidate), or
    it may be perfectly well measured and NEGATIVE — a fact about the business, not a
    gap — or the company may have no P/E because it is loss-making on a trailing
    twelve-month basis even though its annual EPS grew."""
    candidate = candidate or {}
    if _present(candidate.get("PEG_Ratio")) is not None:
        return ""
    growth = _present(candidate.get("EPSGrowth"))
    if isinstance(growth, (int, float)) and growth <= 0:
        return ("its profit per share has been shrinking rather than growing, and this "
                "ratio only means anything for a company whose profits are rising — "
                "dividing by a negative growth rate would produce a negative figure "
                "that sorts as the cheapest thing on the list, which is the exact "
                "trap the ratio exists to expose")
    if isinstance(growth, (int, float)) and _present(candidate.get("PE_Ratio")) is None:
        return ("the company has no price-to-earnings ratio to divide: it is "
                "loss-making over the last twelve months, even though its annual "
                "earnings per share grew over the longer window")
    return {
        "non_positive_base_eps":
            "the company was losing money in the earlier year, so there is no positive "
            "starting profit to measure growth from. That is a fact about its history, "
            "not a gap in the data",
        "non_positive_current_eps":
            "the company lost money in its most recent full year, so there is no growth "
            "rate to work out",
        "insufficient_history":
            "the company has not published enough years of annual results to cover the "
            "whole growth window — usually a recent listing",
        "stale_eps_history":
            "its most recent annual report is too old for a growth rate drawn from it to "
            "describe the company as it is today",
        "no_eps_reported":
            "the data provider did not report an earnings-per-share figure for this company",
        "fetch_failed":
            "the annual accounts could not be retrieved from the data provider",
    }.get(
        _present(candidate.get("EPSGrowth_Unavailable_Reason")),
        "a growth rate could not be worked out from the available annual accounts",
    )


def _verified_figures(candidate: dict) -> dict:
    """The subset of screener output that is deterministic ground truth: figures
    read straight off the filings rather than written by a model. Used twice --
    injected into every agent prompt as VERIFIED_FIGURES, and used as the baseline
    the reconciliation gate checks agent prose against."""
    candidate = candidate or {}
    out = {}
    for key in (
        "TotalDebt", "Cash", "TotalEquity", "TotalAssets", "GoodwillAndIntangibles",
        "InvestedCapital", "EnterpriseValue", "LiveMarketCap", "EBIT", "CapitalEmployed",
    ):
        val = candidate.get(key)
        if isinstance(val, (int, float)) and not pd.isna(val):
            out[key] = float(val)
    return out
