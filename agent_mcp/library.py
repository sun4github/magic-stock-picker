"""Read-only access to what the pipeline has already produced.

These are the cheap half of the toolkit: no subprocess, no LLM, no money, and an
answer inside a single tool call. They matter more than they look, because a caller
reaching this server over the network has no filesystem in common with it - without
these it could start a run and never read the report the run produced.

Everything here is read-only by construction. Nothing in this module writes, and the
only caller-supplied values that reach a path are passed through `_safe_ticker`.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
REPORTS_DIR = SRC_DIR / "reports"
CONFIG_PATH = SRC_DIR / "specs" / "config.yaml"
DEFAULT_RANKINGS = SRC_DIR / "magic_formula_rankings_live.csv"

#: Filename suffix -> the kind of document it is. `Final_Report` and `Refined_Report`
#: carry the verdict in the filename, so they are matched by prefix instead.
REPORT_KINDS = {
    "final": "Final_Report",
    "refined": "Refined_Report",
    "sale_advisory": "Sale_Advisory",
    "buy_case": "Buy_Case",
    "sell_check": "Sell_Check",
    "buy_check": "Buy_Check",
    "critic_review": "Critic_Review",
    "skeptical": "Skeptical_Analysis",
}

_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")


def _safe_ticker(ticker: str) -> str:
    """Validate a ticker before it is allowed anywhere near a path.

    A ticker is a short alphanumeric symbol. Anything else - separators, `..`, a
    null byte - is rejected rather than sanitised, because a caller that sent one
    has a bug worth surfacing, and this server may be answering the network.
    """
    ticker = (ticker or "").strip()
    if not _TICKER_RE.match(ticker):
        raise ValueError(
            f"'{ticker}' is not a valid ticker symbol (expected 1-12 letters, digits, "
            f"'.' or '-')."
        )
    return ticker.upper()


def _parse_report_name(name: str) -> Optional[dict]:
    """Split a report filename into ticker, kind and verdict."""
    if not name.endswith(".md"):
        return None
    stem = name[:-3]
    for kind, marker in REPORT_KINDS.items():
        token = f"_{marker}"
        if token not in stem:
            continue
        ticker, _, remainder = stem.partition(token)
        if not ticker:
            continue
        verdict = remainder.lstrip("_") or None
        return {"ticker": ticker, "kind": kind, "verdict": verdict, "filename": name}
    return None


def list_reports(ticker: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> dict:
    """Every stored report, newest first, optionally filtered."""
    if not REPORTS_DIR.exists():
        return {"reports": [], "count": 0, "detail": f"No reports directory at {REPORTS_DIR}."}

    wanted_ticker = _safe_ticker(ticker) if ticker else None
    if kind and kind not in REPORT_KINDS:
        raise ValueError(f"Unknown kind '{kind}'. Choose one of: {', '.join(sorted(REPORT_KINDS))}.")

    found = []
    for path in REPORTS_DIR.glob("*.md"):
        parsed = _parse_report_name(path.name)
        if not parsed:
            continue
        if wanted_ticker and parsed["ticker"].upper() != wanted_ticker:
            continue
        if kind and parsed["kind"] != kind:
            continue
        stat = path.stat()
        parsed["modified"] = __import__("datetime").datetime.fromtimestamp(stat.st_mtime).isoformat(
            timespec="seconds"
        )
        parsed["size_bytes"] = stat.st_size
        found.append(parsed)

    found.sort(key=lambda r: r["modified"], reverse=True)
    return {
        "reports": found[:limit],
        "count": len(found),
        "truncated": len(found) > limit,
        "kinds_available": sorted(REPORT_KINDS),
    }


def read_report(
    ticker: str,
    kind: str = "final",
    max_chars: int = 40_000,
    offset: int = 0,
) -> dict:
    """Return the text of one stored report.

    Reports run to tens of thousands of characters, so the read is windowed. When the
    document is longer than `max_chars` the response says so and gives the offset to
    continue from, rather than silently handing back a truncated document that reads
    as though it simply ended.
    """
    ticker = _safe_ticker(ticker)
    if kind not in REPORT_KINDS:
        raise ValueError(f"Unknown kind '{kind}'. Choose one of: {', '.join(sorted(REPORT_KINDS))}.")

    marker = REPORT_KINDS[kind]
    matches = sorted(
        REPORTS_DIR.glob(f"{ticker}_{marker}*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        available = list_reports(ticker=ticker)["reports"]
        return {
            "error": f"No '{kind}' report stored for {ticker}.",
            "available_for_ticker": [r["kind"] for r in available] or None,
            "hint": (
                f"Run start_ticker_analysis for {ticker} to produce one."
                if not available
                else "One of the kinds above may be what you meant."
            ),
        }

    path = matches[0]
    text = path.read_text(encoding="utf-8", errors="replace")
    window = text[offset : offset + max_chars]
    parsed = _parse_report_name(path.name) or {}
    return {
        "ticker": ticker,
        "kind": kind,
        "verdict": parsed.get("verdict"),
        "filename": path.name,
        "modified": __import__("datetime").datetime.fromtimestamp(path.stat().st_mtime).isoformat(
            timespec="seconds"
        ),
        "total_chars": len(text),
        "returned_chars": len(window),
        "next_offset": offset + len(window) if offset + len(window) < len(text) else None,
        "content": window,
    }


def get_rankings(limit: int = 30, csv_path: Optional[str] = None) -> dict:
    """The current screener output - the ranked candidate list Phase A produced."""
    path = Path(csv_path) if csv_path else DEFAULT_RANKINGS
    if not path.is_absolute():
        path = SRC_DIR / path
    if not path.exists():
        return {
            "error": f"No rankings CSV at {path}.",
            "hint": "Run start_screen to produce one; it is free and takes 20-30 minutes.",
        }

    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    stat = path.stat()
    return {
        "csv_path": str(path),
        "generated": __import__("datetime").datetime.fromtimestamp(stat.st_mtime).isoformat(
            timespec="seconds"
        ),
        "total_candidates": len(rows),
        "returned": min(limit, len(rows)),
        "candidates": rows[:limit],
    }


def get_screen_config() -> dict:
    """The tunable parameters the screen and the budget guards actually run on.

    Returned so a caller can explain *why* a name did or did not make the list, and
    what a run will refuse to spend, without needing the repo on its own disk.
    """
    try:
        import yaml
    except ImportError:
        return {"error": "pyyaml is not installed, so config.yaml cannot be read."}

    if not CONFIG_PATH.exists():
        return {"error": f"No config at {CONFIG_PATH}."}

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    screener = (config.get("agents") or {}).get("screener_agent") or {}
    filters = config.get("screener_filters") or config.get("screener") or {}

    max_peg = _find_key(config, "max_peg")
    growth_cap = _find_key(config, "max_growth_rate_for_peg")
    pe_ceiling = None
    if isinstance(max_peg, (int, float)) and isinstance(growth_cap, (int, float)):
        # The screen's hard P/E ceiling is an emergent property of two settings that
        # look independent. Computing it here means a caller reading the config sees
        # the constraint rather than having to derive it.
        pe_ceiling = round(max_peg * growth_cap * 100, 1)

    return {
        "config_path": str(CONFIG_PATH),
        "model": (config.get("agents") or {}).get("model"),
        "top_n_candidates": screener.get("top_n_candidates"),
        "peg_thresholds": {
            "max_peg": max_peg,
            "max_growth_rate_for_peg": growth_cap,
            "min_base_net_margin": _find_key(config, "min_base_net_margin"),
            "implied_hard_pe_ceiling": pe_ceiling,
            "note": (
                "max_peg and max_growth_rate_for_peg are multiplicatively linked: their "
                "product x 100 is a P/E ceiling no company can enter the screen above, "
                "however fast it grows. Never change one alone."
            ),
        },
        "eligibility_gates": {
            key: _find_key(config, key)
            for key in ("min_roa", "roa_basis", "min_pe", "exclude_recent_earnings_days", "exclude_adr")
        },
        "budget": config.get("budget"),
        "reuse": config.get("reuse"),
        "refinement": {
            key: (config.get("refinement") or {}).get(key)
            for key in ("max_budget_usd", "max_rounds")
        },
        "screener_filters": filters if isinstance(filters, dict) else None,
    }


def _find_key(node, key):
    """Depth-first search for a config key, wherever it sits in the tree.

    The config's nesting has moved between versions; looking the value up by name
    keeps this reader working across those moves rather than returning null the
    first time a block is reorganised.
    """
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None
