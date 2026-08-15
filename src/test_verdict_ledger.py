"""Tests for the verdict ledger's parsing and coercion layer.

Runs offline — no model calls, no database, no network:

    python test_verdict_ledger.py

Why these exist. The ledger is a record of what we claimed and what happened
next, and it is read months later by someone deciding whether the system's
judgement can be trusted. Two things would quietly destroy that record:

  * a basis block that fails to parse, taking the factors down with it while the
    verdict row still lands — producing a verdict that looks reasoned and has no
    reasoning attached;
  * a NaN or a "$44.03" reaching a NUMERIC column, where NaN in particular is
    stored literally and poisons every average computed over that column
    afterwards.

Neither failure is visible at write time. Both are permanent. So the coercion
and the parser are tested against the shapes the screener and the model actually
emit, rather than against tidy ones.
"""
import json

from agents import analyst_agent
from pipeline import _extract_verdict, _split_verdict_basis
from settings import LEDGER_FACTOR_TAXONOMY
from mcp_server import _num


REPORT_HEAD = (
    "## Recent Quarter Check\nRevenue fell 4%.\n\n"
    "## Final Verdict\n**Verdict: Watch** — passes the screen but the case is thin.\n\n"
    "## What Would Make This Wrong\nRevenue reaccelerates in the quarter ending 30 Sep.\n"
)

BASIS = [
    {"factor_key": "customer_concentration", "stance": "BEAR", "materiality": "HIGH",
     "observation": "Top client is 31% of revenue. The contract renews in 2027."},
    {"factor_key": "catalyst_speculative", "stance": "BULL", "materiality": "LOW",
     "observation": "A rumoured contract could add revenue. Nothing is signed."},
]


def _report(basis, fenced=True, heading="## Verdict Basis"):
    body = json.dumps(basis, indent=1)
    block = f"```json\n{body}\n```" if fenced else body
    return f"{REPORT_HEAD}\n{heading}\n{block}\n"


def _num_cases():
    """(name, input, expected) — the shapes a screener DataFrame actually emits."""
    return [
        ("dollar-formatted price", "$44.03", 44.03),
        ("thousands separator", "1,234.5", 1234.5),
        ("percent-suffixed ratio", "152.71%", 152.71),
        ("plain float", 15.24, 15.24),
        ("integer rank", 3, 3.0),
        # The one that matters most: pandas writes NaN for any metric it could
        # not compute (PEG is absent for a company with negative invested
        # capital), and float('nan') inserted into NUMERIC is stored as the
        # literal NaN rather than NULL. Every AVG over that column is then NaN.
        ("pandas NaN -> NULL", float("nan"), None),
        ("infinity -> NULL", float("inf"), None),
        ("None stays None", None, None),
        ("empty string", "   ", None),
        ("unparseable text", "n/a", None),
    ]


def _parser_cases():
    """(name, report_text, expected factor_keys)"""
    unknown = [{"factor_key": "weird_new_thing", "stance": "BULL",
                "materiality": "LOW", "observation": "Novel. Not in the list."}]
    bad_stance = [{"factor_key": "insider_buying", "stance": "SIDEWAYS",
                   "materiality": "LOW", "observation": "Neither one nor the other."}]
    return [
        ("fenced json block", _report(BASIS),
         ["customer_concentration", "catalyst_speculative"]),
        # Models occasionally drop the fence. The data is perfectly good when
        # they do, and discarding it would lose an observation for a formatting
        # slip.
        ("unfenced json array", _report(BASIS, fenced=False),
         ["customer_concentration", "catalyst_speculative"]),
        ("heading at another level", _report(BASIS, heading="### Verdict Basis"),
         ["customer_concentration", "catalyst_speculative"]),
        # An off-vocabulary key is KEPT under OTHER_, not dropped: a dropped
        # observation is unrecoverable, an untidy key is a rename.
        ("unknown key kept as OTHER_", _report(unknown), ["OTHER_weird_new_thing"]),
        ("already-prefixed key not double-prefixed",
         _report([dict(unknown[0], factor_key="OTHER_already")]), ["OTHER_already"]),
        # Everything below must degrade to "no factors" WITHOUT losing the
        # report: the analysis is the product, the bookkeeping is not.
        ("no basis section", REPORT_HEAD, []),
        ("malformed json", REPORT_HEAD + "\n## Verdict Basis\n```json\n[{oops}]\n```\n", []),
        ("json object not array", REPORT_HEAD + '\n## Verdict Basis\n```json\n{"a": 1}\n```\n', []),
        ("empty array", _report([]), []),
        ("empty report", "", []),
    ]


def run():
    failures = []

    print("-- numeric coercion (mcp_server._num) --")
    for name, value, expected in _num_cases():
        got = _num(value)
        ok = got == expected or (got is None and expected is None)
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {got!r}")

    print()
    print("-- basis parsing (main._split_verdict_basis) --")
    for name, text, expected_keys in _parser_cases():
        cleaned, factors = _split_verdict_basis(text, "TEST")
        keys = [f["factor_key"] for f in factors]
        ok = keys == expected_keys
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {keys}")

    print()
    print("-- the report survives the split --")
    checks = []

    cleaned, factors = _split_verdict_basis(_report(BASIS), "TEST")
    checks.append(("basis block removed from the report",
                   "Verdict Basis" not in cleaned and "factor_key" not in cleaned))
    checks.append(("prose above it is untouched",
                   "## What Would Make This Wrong" in cleaned and "30 Sep" in cleaned))
    # The strip happens before _extract_verdict, the reconciliation gate and the
    # critic run. If it ever ate the verdict line, every downstream consumer
    # would silently fall back to the 'WATCH' default.
    checks.append(("verdict still extractable after the strip",
                   _extract_verdict(cleaned) == "WATCH"))
    checks.append(("stance and materiality normalised",
                   factors[0]["stance"] == "BEAR" and factors[0]["materiality"] == "HIGH"))

    # A malformed block must still leave a readable report behind, or one bad
    # JSON emission costs a report that was paid for.
    broken, _ = _split_verdict_basis(
        REPORT_HEAD + "\n## Verdict Basis\n```json\n[{oops}]\n```\n", "TEST")
    checks.append(("malformed basis still yields a clean report",
                   "## Final Verdict" in broken and "oops" not in broken))

    # An empty vocabulary would silently send the analyst a prompt asking it to
    # choose from a list that is not there, so the taxonomy has to be present
    # and the prompt has to actually carry it.
    checks.append(("taxonomy loaded from config", len(LEDGER_FACTOR_TAXONOMY) > 0))
    checks.append(("prompt carries the vocabulary",
                   "customer_concentration" in analyst_agent.instruction))
    checks.append(("prompt asks for the section",
                   "## Verdict Basis" in analyst_agent.instruction))
    # The screen's own entry criteria must stay out of the ledger: every
    # candidate has them, so recording them would swamp the base rates with a
    # constant that distinguishes nothing.
    checks.append(("screen criteria excluded from the vocabulary",
                   not any(k in LEDGER_FACTOR_TAXONOMY
                           for k in ("high_return_on_capital", "high_earnings_yield",
                                     "low_peg", "eps_growth"))))

    for name, ok in checks:
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {ok}")

    total = len(_num_cases()) + len(_parser_cases()) + len(checks)
    print()
    print(f"{total - len(failures)}/{total} passed.")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
