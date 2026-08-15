"""The reconciliation gate: agent prose checked against the pipeline's own figures.

A tripwire for one specific failure — a fabricated headline number propagating
through the report chain — not a general fact checker. A clean result means 'no
contradiction of our own basis', never 'everything is true'.

The commentary below records which association rules were tried and what defeated
them, on real reports. It is the most expensive thing in this file to rediscover,
so it travels with the regexes it explains.

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
import re

from magic_formula_starter_screener import format_money

from .figures import _verified_figures


# Heading of the reader-facing warning table. Defined once: the gate strips
# everything from this point on before scanning, so it never re-reads its own
# output when an already-stored report is checked again.
_RECONCILIATION_HEADING = "## Data Reconciliation Warnings"

# Figures an agent must not contradict, mapped to the phrases that introduce them
# in prose. Used by the reconciliation gate below.
_RECONCILED_FIELDS = {
    "TotalDebt": (r"total debt|debt (?:load|stack|burden|pile)|gross debt", "total debt"),
    "LiveMarketCap": (r"market cap(?:italisation|italization)?", "market capitalisation"),
    "EnterpriseValue": (r"enterprise value", "enterprise value"),
}

# Written amounts: $36.9 billion / $36.9B / $36,908 million / $36908M.
_MONEY_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|bn|b|million|mm|m|trillion|t)\b",
    re.IGNORECASE,
)
_MULTIPLIER = {
    "t": 1e12, "trillion": 1e12,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "m": 1e6, "mm": 1e6, "million": 1e6,
}

# How far an agent-written figure may drift from the verified one before it is
# flagged. Loose enough to tolerate rounding and a slightly different period end,
# tight enough to catch a genuinely different number.
#
# Per-field, because these are not the same kind of number. Total debt is an
# accounting figure read off a filing and should barely move between the sources
# an agent might cite. Market capitalisation is a LIVE price that legitimately
# drifts between when a cited article was written and when the pipeline ran, and
# enterprise value inherits that drift. Holding a market price to a 10% accounting
# tolerance just manufactures warnings about ordinary price movement.
RECONCILE_TOLERANCE = 0.10
_FIELD_TOLERANCE = {
    "TotalDebt": 0.10,
    "EnterpriseValue": 0.20,
    "LiveMarketCap": 0.25,
}

# The gate adjudicates claims about the CURRENT level only. Two kinds of figure
# are legitimately different from today's balance sheet and must not be flagged,
# or the warnings that matter get lost in noise:
#
#   1. A change rather than a level -- "debt rose BY $11 billion". Detected by
#      delta language in the ~30 characters immediately before the number.
#   2. A figure pinned to another period -- "debt was $24.4B in 2023", "reaches
#      $32B by FY2027". Detected by a year or quarter marker close to the number.
#   3. A THRESHOLD rather than a level -- "Re-leveraging above $6.00B Total Debt".
#      The sale advisory is written almost entirely in this form: every sell
#      trigger names a level the company has NOT reached, sitting deliberately
#      clear of the current figure. Flagging those reports a correct advisory as
#      contradicting the filings, which is how this class was found.
#
# Known limitation: a figure written only inside a markdown table whose unit lives
# in the column header ("| Total Debt ($M) | 36,908 |") carries no unit next to the
# number and is not checked. Matching bare table numbers would flag every legitimate
# multi-year history table, so prose assertions are the deliberate scope here.
_HEDGE = r"(?:over|under|about|nearly|roughly|approximately|more\s+than|less\s+than)"
# Change language: the amount is a movement, not a level.
_DELTA_PREFIX_RE = re.compile(
    rf"\b(?:by|added|increase[ds]?|rose|grew|surged?|jumped|climbed|reduced?|repaid|"
    rf"paid\s+down|cut|declined?|fell|dropped)\s*{_HEDGE}?\s*$",
    re.IGNORECASE,
)
# Threshold language: the amount is a level the company has NOT reached. Scanned
# only in the text PRECEDING the number -- "above $6.00B", "rises above $9.50B".
# Scanning forward as well would let a trigger in the NEXT sentence suppress a
# wrong baseline in this one: "Current total debt of $8.92B. Sell if debt rises
# above $9.50B." must still flag the $8.92B.
_THRESHOLD_RE = re.compile(
    r"\b(?:above|below|under|over|beyond|exceed(?:s|ing|ed)?|surpass(?:es|ing|ed)?|"
    r"breach(?:es|ing|ed)?|rises?\s+(?:above|to)|climbs?\s+(?:above|to)|"
    r"falls?\s+(?:below|under)|drops?\s+below|at\s+least|greater\s+than|"
    r"more\s+than|less\s+than|no\s+more\s+than|re-?lever\w*|threshold)\b",
    re.IGNORECASE,
)
_THRESHOLD_PROXIMITY = 45  # chars either side scanned for threshold language
_OTHER_PERIOD_RE = re.compile(r"\b(?:FY\s?)?20\d{2}\b|\bQ[1-4]\b", re.IGNORECASE)
_DELTA_LOOKBEHIND = 30    # chars before the number scanned for delta language
_PERIOD_PROXIMITY = 40    # chars either side scanned for a year/quarter marker
_CONCEPT_LOOKBEHIND = 25  # chars before the concept word scanned for the amount

# Associating a label with the amount it describes.
#
# The naive rule -- "nearest dollar figure to the label" -- is wrong, and wrong in
# a way that fires constantly on correct reports. Real examples that defeated it:
#
#   "TTM EBIT of $88.35M on a verified Enterprise Value of $8.91B"
#   "Cash & Short-Term Investments of $453.58M vs. Total Debt of $7.61M"
#
# In both, an unrelated amount sits just BEFORE the label and the correct one just
# after, so "nearest" picks the wrong one and reports a 99% discrepancy against a
# perfectly accurate sentence. English almost always writes "<label> of $X", so:
#
#   1. An amount immediately BEFORE the label wins first -- no intervening words,
#      as in "$36.9 billion debt burden" or "($8.91B Enterprise Value)". Direct
#      adjacency is the strongest signal there is.
#   2. Otherwise take the first amount AFTER the label, but only when it is joined
#      to it by a short connector ("of", "was", ": ", a table pipe). A comma or a
#      long gap means a new clause has started and the amount belongs to something
#      else -- "...$7.61M total debt, giving it roughly $446 million in net cash"
#      must not report $446M as the debt.
#   3. Never associate across a line break, and cross at most one table-cell
#      boundary, so a label can reach its own cell but not a peer's column or a
#      figure on the row above.
# An amount belongs to a label only when the text BETWEEN them is pure connective
# tissue. Measuring that gap in characters proved far too blunt -- "$29.31 billion
# in total debt** against **$829 million in cash" has a short gap on both sides,
# and the character rule attributed the cash figure to the debt label. So the gap
# must match a whitelist instead: whitespace, markdown emphasis, table pipes, a
# parenthetical gloss, and a small set of linking verbs.
#
# Threshold words (above, below, exceeds, ...) are deliberately EXCLUDED. "total
# debt above $32B" is a sell-trigger condition, not a claim about today's level,
# and the sale advisory is written almost entirely in that form.
_NOISE = r"[\s\*_:|~\-–—]"
_PAREN = r"\([^)]{0,120}\)"
_FORWARD_CONNECTOR_RE = re.compile(
    rf"^(?:{_NOISE}|{_PAREN})*"
    rf"(?:(?:of|is|are|was|were|at|totals?|totall?ing|totall?ed|reached?|remains?|"
    rf"stands?|sits?|carries|carrying|holds?|comes?)\b(?:{_NOISE}|{_PAREN})*)*"
    rf"(?:(?:approximately|roughly|about|nearly|around|circa|some)\b(?:{_NOISE}|{_PAREN})*)?"
    rf"$",
    re.IGNORECASE,
)
# Preceding form: "$36.9 billion debt burden", "$29.31 billion in total debt".
_BACKWARD_CONNECTOR_RE = re.compile(rf"^(?:{_NOISE})*(?:(?:in|of)\b(?:{_NOISE})*)?$", re.IGNORECASE)

_FORWARD_SCAN = 140        # chars after the label searched for its amount
_BACKWARD_GAP_MAX = 12     # backstop on the preceding-amount gap
_CONNECTOR_GAP_MAX = 60    # backstop on the following-amount gap

# A line carrying three or more amounts is a comparison table -- either a
# multi-year history ("| Total Debt | $1.31B | $1.27B | $1.37B |") or a peer
# column set ("| Market Cap | $9.36B | $6.75B | $6.78B |"). The gate cannot tell
# which column is the current period or which company owns it, and a sweep of the
# stored corpus showed the column order is not even consistent between reports
# (some run oldest-first, some newest-first). Attributing any one cell to "the
# current figure" is therefore a guess, so these rows are skipped outright.
_COMPARISON_ROW_MIN_AMOUNTS = 3

# Peer discussion: "Akebia Therapeutics, Inc. (AKBA) ... ($327M market cap)" or
# "**CRVS** ($1.10B market cap) commands a far higher multiple". The amount belongs
# to the named peer, not the subject. Detected by a ticker earlier on the same line
# -- parenthesised or bold-emphasised -- that is not the subject's own symbol.
_PEER_TICKER_RE = re.compile(r"\(([A-Z]{2,5})\)|\*\*([A-Z]{2,5})\*\*")


def _line_bounds(text: str, pos: int) -> tuple:
    """Start and end offsets of the line containing `pos`."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return start, (len(text) if end == -1 else end)


def _amount_for_label(text: str, m: re.Match):
    """Locate the amount a label refers to. Returns (abs_start, abs_end, match) for
    the associated amount, or None when no amount can be attributed to this label.

    `m` is the label match. See the commentary above for the association rules."""
    # --- Backward: the amount butts directly against the label ---
    back_start = max(0, m.start() - _CONCEPT_LOOKBEHIND)
    back = text[back_start: m.start()]
    boundary = max(back.rfind("\n"), back.rfind("|"))
    if boundary != -1:
        back_start += boundary + 1
        back = text[back_start: m.start()]
    preceding = list(_MONEY_RE.finditer(back))
    if preceding:
        last = preceding[-1]
        gap = back[last.end():]
        if len(gap) <= _BACKWARD_GAP_MAX and _BACKWARD_CONNECTOR_RE.match(gap):
            return back_start + last.start(), back_start + last.end(), last

    # --- Forward: the dominant construction, "<label> of $X" ---
    region = text[m.end(): m.end() + _FORWARD_SCAN]
    line_end = region.find("\n")
    if line_end != -1:
        region = region[:line_end]
    # Allow reaching into the next table cell (label and value are usually adjacent
    # cells) but stop at the one after it, so peer columns are never picked up.
    cells = [c.start() for c in re.finditer(r"\|", region)]
    if len(cells) >= 2:
        region = region[:cells[1]]
    money = _MONEY_RE.search(region)
    if money:
        connector = region[:money.start()]
        if len(connector) <= _CONNECTOR_GAP_MAX and _FORWARD_CONNECTOR_RE.match(connector):
            return m.end() + money.start(), m.end() + money.end(), money
    return None


def _reconcile_agent_figures(text: str, candidate: dict, source: str) -> list:
    """Scan agent-written prose for dollar figures presented as total debt, market
    cap, or enterprise value, and flag any that deviate from the pipeline's own
    verified figures by more than RECONCILE_TOLERANCE.

    Deliberately narrow: it only checks the three figures the pipeline computes
    deterministically, and only when the sentence names one of them. It is a
    tripwire for the specific failure of a fabricated headline number propagating
    through the report chain, not a general-purpose fact checker -- so a clean
    result means 'no contradiction of our own basis', never 'everything is true'.
    """
    findings = []
    if not text:
        return findings
    verified = _verified_figures(candidate)
    if not verified:
        return findings

    # Never scan a previous run's warning table. In the live pipeline the gate sees
    # the analyst's raw output, which has no such section -- but re-checking an
    # already-stored report would otherwise read the table's own "stated vs
    # verified" columns as fresh claims and re-flag every one of them.
    text = text.split(_RECONCILIATION_HEADING)[0]
    subject = (candidate or {}).get("Symbol") or ""

    for field, (pattern, label) in _RECONCILED_FIELDS.items():
        truth = verified.get(field)
        if not truth:
            continue
        for m in re.finditer(pattern, text, re.IGNORECASE):
            line_start, line_end = _line_bounds(text, m.start())
            line = text[line_start:line_end]

            # Comparison table row -- cannot attribute a column. See above.
            if len(_MONEY_RE.findall(line)) >= _COMPARISON_ROW_MIN_AMOUNTS:
                continue

            # A peer named earlier on this line owns the figure, not the subject.
            peers = [a or b for a, b in _PEER_TICKER_RE.findall(text[line_start: m.start()])]
            if peers and peers[-1] != subject:
                continue

            located = _amount_for_label(text, m)
            if not located:
                continue
            money_start, money_end, money = located
            try:
                amount = float(money.group(1).replace(",", "")) * _MULTIPLIER[money.group(2).lower()]
            except (ValueError, KeyError):
                continue

            # Skip figures that are not claims about the current level (see above).
            if _DELTA_PREFIX_RE.search(text[max(0, money_start - _DELTA_LOOKBEHIND): money_start]):
                continue
            near = text[max(0, money_start - _PERIOD_PROXIMITY): money_end + _PERIOD_PROXIMITY]
            if _OTHER_PERIOD_RE.search(near):
                continue
            if _THRESHOLD_RE.search(text[max(0, money_start - _THRESHOLD_PROXIMITY): money_start]):
                continue

            deviation = abs(amount - truth) / truth
            if deviation > _FIELD_TOLERANCE.get(field, RECONCILE_TOLERANCE):
                findings.append({
                    "source": source,
                    "field": label,
                    "key": field,
                    "basis": _finding_basis(field),
                    "stated": amount,
                    "verified": truth,
                    "deviation_pct": round(deviation * 100, 1),
                    "context": " ".join(text[m.start(): money_end + 40].split()),
                })

    # One agent can repeat the same wrong number many times; report each distinct
    # (field, stated amount) pair once so the log stays readable.
    unique, seen = [], set()
    for f in findings:
        key = (f["field"], round(f["stated"]))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


# Not every verified figure has the same provenance, and the gate's wording used to
# pretend otherwise: every finding, market capitalisation included, was reported as
# "the filings show X". Debt and cash DO come off the balance sheet and are fixed
# until the next filing, so a disagreement there is a mistake. Market capitalisation
# is today's share price times shares outstanding, and enterprise value is built on
# it — those move every session, so a disagreement may be nothing but the passage of
# time. That distinction is invisible in a log line that calls both "the filings",
# and it matters most in exactly the place the gate is least trusted: a report being
# re-checked days after it was written (`refine.py`, `sale_advisory.py`,
# `buy_case.py`), where a market figure is EXPECTED to have moved.
_MARKET_DERIVED_FIELDS = {"LiveMarketCap", "EnterpriseValue"}


def _finding_basis(field_key: str) -> str:
    """'market price' for figures that move every session, else 'filings'."""
    return "market price" if field_key in _MARKET_DERIVED_FIELDS else "filings"


def reconciliation_warning(ticker: str, finding: dict, with_context: bool = False) -> str:
    """The log line for one reconciliation finding, phrased for its basis.

    One function rather than the four near-identical f-strings this replaced (here,
    `refine`, `sale_advisory`, `buy_case_agent`) — that duplication is how the
    misleading "the filings show" wording came to be repeated in four places at once.
    """
    head = (f"[{ticker}] RECONCILIATION: {finding['source']} states {finding['field']} "
            f"of {format_money(finding['stated'])}, ")
    if finding.get("basis") == "market price":
        body = (f"against {format_money(finding['verified'])} implied by the CURRENT "
                f"share price (off by {finding['deviation_pct']}%). Market figures move "
                f"every session — on a report being re-checked later, this may be the "
                f"passage of time rather than an error.")
    else:
        body = (f"but the filings show {format_money(finding['verified'])} "
                f"(off by {finding['deviation_pct']}%).")
    tail = f" Context: \"{finding['context']}\"" if with_context else ""
    return head + body + tail


def _format_reconciliation_section(findings: list) -> str:
    """Reader-facing note appended to the final report when the gate fired.

    Surfaced in the report rather than only logged: a reader has no other way to
    know that a figure in the bull or bear case above contradicts the pipeline's own.

    The Basis column is the point of the table's fourth column, not decoration. A
    'filings' row is a factual error in the report. A 'market price' row on a report
    checked later may be nothing more than the share price having moved since — the
    same distinction the critic is given in `critic_agent.py`, in the one other place
    a stale price can be mistaken for a wrong one."""
    if not findings:
        return ""
    market_rows = any(f.get("basis") == "market price" for f in findings)
    lines = [
        "",
        _RECONCILIATION_HEADING,
        "",
        "Some figures written by the analysis agents above disagree with the figures "
        "this pipeline computed itself. **Read the Basis column before concluding the "
        "report is wrong** — the two kinds of disagreement do not mean the same thing.",
        "",
        "| Section | Figure | Stated in report | Verified | Basis | Off by |",
        "| :--- | :--- | ---: | ---: | :--- | ---: |",
    ]
    for f in findings:
        basis = f.get("basis") or "filings"
        lines.append(
            f"| {f['source']} | {f['field']} | {format_money(f['stated'])} | "
            f"{format_money(f['verified'])} | {basis} | {f['deviation_pct']}% |"
        )
    lines += [
        "",
        "- **_filings_** — the verified figure came off the company's balance sheet "
        "and does not change until the next filing. A disagreement here is an error "
        "in the report, and any argument resting on that number is unreliable.",
    ]
    if market_rows:
        lines += [
            "- **_market price_** — the verified figure is derived from the share "
            "price, which changes every trading session (market capitalisation is the "
            "price times the number of shares; enterprise value is built on top of "
            "it). On a report checked some time after it was written, a gap here can "
            "simply mean the stock has moved since. Compare the dates before treating "
            "it as a mistake.",
        ]
    return "\n".join(lines) + "\n"
