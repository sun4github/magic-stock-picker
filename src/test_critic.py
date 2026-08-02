"""Tests for the critic/analyst refinement loop's deterministic parts.

Runs offline — no model calls, no database, no billed work:

    python test_critic.py

Everything under test here decides either what a reader is TOLD about a report's
standing or how much the loop is allowed to SPEND, and all of it is Python reading
model prose. The two failures that matter:

  - a critique the parser reads as clean when it is not, which would stamp
    "the critic agreed" on a report carrying a blocking objection;
  - a spend projection that lets the loop start a round it cannot finish.
"""
import main
import refine
import critic_agent as ca


# --- Fixtures ------------------------------------------------------------------
_CLEAN = """## Independent Verification
Checked FMP news (nothing material since the report), and searched for the
distribution contract. Clean.

## Prior Findings Status
No prior findings.

## Findings
No findings.

## Verdict Check
Yes — the verdict follows from the evidence.

## Critic Verdict
CRITIC VERDICT: AGREE
The report's reasoning holds and the verdict follows from it.
"""

_BLOCKING = """## Independent Verification
Checked FMP news. The Q3 guidance cut on 12 May is not reflected anywhere.

## Findings
### Finding 1 — verdict rests on a rumoured acquisition
- **Severity:** BLOCKING
- **Type:** speculative catalyst load-bearing
- **Where:** Final Verdict, first paragraph
- **Why it is wrong:** removing the rumoured deal collapses the Buy case.
- **Evidence:** reasoning only
- **Required fix:** re-weigh without the rumoured deal, or move to Watch.

### Finding 2 — jargon left unexplained
- **Severity:** MINOR
- **Type:** jargon left unexplained
- **Where:** Bull Case summary
- **Why it is wrong:** "operating leverage" is never explained.
- **Evidence:** reasoning only
- **Required fix:** add a plain-English gloss.

## Critic Verdict
CRITIC VERDICT: REVISE
One blocking finding stands.
"""

# The dangerous shape: a critic that lists a material objection and then declares
# agreement anyway. Resolving that toward AGREE would ship a report the critic's own
# text says is not supportable.
_CONTRADICTORY = """## Findings
### Finding 1 — priced in asserted, not argued
- **Severity:** MATERIAL
- **Type:** priced in asserted
- **Where:** Bear Case summary
- **Why it is wrong:** no evidence offered that the market has absorbed it.
- **Required fix:** show what tells you it is priced in, or drop the claim.

## Critic Verdict
CRITIC VERDICT: AGREE
Close enough.
"""

# Format drift: different heading depth, no bold markers, colon after the number.
_DRIFTED = """## Findings
#### Finding 1: seasonality error
Severity: MATERIAL
Type: seasonality error
The trigger compares fiscal Q3 with fiscal Q2 in a tax preparer.

## Critic Verdict
CRITIC VERDICT: REVISE
"""

# No structure at all and no verdict line — a formatting failure, not agreement.
_UNSTRUCTURED = """The report leans on a rumoured deal. Severity: BLOCKING, in my view.
It should be reweighed.
"""

_MINOR_ONLY = """## Findings
### Finding 1 — a small wording problem
- **Severity:** MINOR
- **Type:** jargon left unexplained
- **Required fix:** gloss the term.

## Critic Verdict
CRITIC VERDICT: AGREE
Only a minor point remains, so the report stands.
"""


def _verdict(text):
    findings = ca.parse_findings(text)
    return ca.extract_critic_verdict(text, findings)[0], findings


def _parsing_cases():
    """(name, predicate) — each returns True when the parser behaved."""
    clean_v, clean_f = _verdict(_CLEAN)
    block_v, block_f = _verdict(_BLOCKING)
    contra_v, contra_f = _verdict(_CONTRADICTORY)
    drift_v, drift_f = _verdict(_DRIFTED)
    unstr_v, unstr_f = _verdict(_UNSTRUCTURED)
    minor_v, minor_f = _verdict(_MINOR_ONLY)
    return [
        ("clean review -> AGREE", clean_v == "AGREE"),
        ("clean review -> no findings", clean_f == []),
        ("blocking review -> REVISE", block_v == "REVISE"),
        ("blocking review -> both findings parsed", len(block_f) == 2),
        ("severities read off correctly",
         [f["severity"] for f in block_f] == ["BLOCKING", "MINOR"]),
        ("finding titles survive the em dash",
         block_f[0]["title"] == "verdict rests on a rumoured acquisition"),
        ("finding type captured", block_f[0]["type"] == "speculative catalyst load-bearing"),
        # The whole point of cross-checking the declared verdict against severities.
        ("declared AGREE with a MATERIAL finding is forced to REVISE", contra_v == "REVISE"),
        ("the override is explained",
         bool(ca.extract_critic_verdict(_CONTRADICTORY, contra_f)[1])),
        # MINOR never blocks: a critic that cannot agree over a wording nit burns the
        # budget and the report ends up stamped un-agreed for nothing.
        ("MINOR-only review still AGREEs", minor_v == "AGREE"),
        ("format drift still yields a finding", len(drift_f) == 1),
        ("format drift still reads the severity", drift_f[0]["severity"] == "MATERIAL"),
        ("unstructured critique is not read as clean", unstr_f != []),
        ("unstructured critique -> REVISE", unstr_v == "REVISE"),
        # A missing verdict line is a formatting failure, never evidence of agreement.
        ("no verdict line + blocking severity -> REVISE",
         ca.extract_critic_verdict("Severity: BLOCKING", [{"severity": "BLOCKING"}])[0] == "REVISE"),
        # The instruction quotes "CRITIC VERDICT: AGREE" in its own format spec, so an
        # echoed example must not outrank the real answer at the end.
        ("last verdict line wins over an echoed example",
         ca.extract_critic_verdict(
             "Format: CRITIC VERDICT: AGREE\n...\nCRITIC VERDICT: REVISE", [])[0] == "REVISE"),
    ]


def _split_cases():
    body = "## Recent Quarter Check\nRevenue fell 4%.\n\n## Final Verdict\n**Verdict: Watch**\n"
    with_marker = body + f"\n{ca.RESPONSE_MARKER}\n1. FIXED — reweighed without the rumour.\n"
    r1, resp1 = ca.split_revision(with_marker)
    r2, resp2 = ca.split_revision(body)
    return [
        ("marker splits the report from the reply", r1 == body.strip()),
        ("the reply is captured", resp1.startswith("1. FIXED")),
        ("the marker never reaches the report", ca.RESPONSE_MARKER not in r1),
        # A missing marker costs one round of re-raised findings; discarding the
        # revision would cost the whole revision.
        ("a missing marker keeps the report", r2 == body.strip()),
        ("a missing marker yields an empty reply", resp2 == ""),
    ]


def _strip_cases():
    stored = (
        "> **Run ID:** `abc-123`  \n> **Ticker:** CROX\n\n"
        "## Magic Formula Metrics\n\nEarnings yield 12%.\n\n"
        "## Recent Quarter Check\nRevenue fell 4%.\n\n"
        "## Final Verdict\n**Verdict: Watch**\n\n"
        "## Data Reconciliation Warnings\n\n| a | b |\n"
    )
    out = refine.strip_generated_sections(stored)
    refined = (
        "> **Run ID:** `def-456`\n\n"
        "## Magic Formula Metrics\n\nx\n\n"
        "## Recent Quarter Check\nStill 4%.\n\n"
        f"{refine.CRITIC_SECTION_HEADING}\n\nThe critic agreed.\n"
    )
    out2 = refine.strip_generated_sections(refined)
    unknown = "## Some Other Shape\nNo mandated sections here.\n"
    return [
        ("run-id banner removed", not out.startswith(">")),
        ("deterministic metrics section removed", "## Magic Formula Metrics" not in out),
        ("reconciliation warnings removed", "Data Reconciliation" not in out),
        ("the analyst's prose is kept", out.startswith("## Recent Quarter Check")),
        ("the verdict survives", "**Verdict: Watch**" in out),
        # Refining a refined report must not nest one critic section inside another,
        # or the critic ends up reviewing its own previous review.
        ("a previous critic section is removed", refine.CRITIC_SECTION_HEADING not in out2),
        ("an unfamiliar report shape is passed through", "Some Other Shape" in
         refine.strip_generated_sections(unknown)),
    ]


def _budget_cases():
    """The ceiling must reserve a revision AND the critique that follows it."""
    est = refine._Estimator()
    seeded = est.full_round
    est.observe("critique", 0.40)
    est.observe("revision", 0.20)
    measured = est.full_round
    h = refine.ESTIMATE_HEADROOM
    # A turn that failed before billing anything must not be recorded as a cheap
    # round, or the next projection lets the loop start something it cannot finish.
    est.observe("critique", 0.0)
    zero_ignored = abs(est.critique - 0.40 * h) < 1e-9
    return [
        ("a full round reserves revision + critique",
         abs(seeded - (refine.SEED_CRITIQUE_USD + refine.SEED_REVISION_USD)) < 1e-9),
        ("measurements replace the seeds",
         abs(measured - (0.40 * h + 0.20 * h)) < 1e-9),
        ("a zero measurement is ignored (a failed turn is not a cheap one)", zero_ignored),
        ("spending inside the ceiling is allowed",
         refine._affordable(0.50, 0.40, 2.00, 0.0) == ""),
        ("a round that would breach the ceiling is refused",
         bool(refine._affordable(1.80, 0.40, 2.00, 0.0))),
        # The refinement ceiling is not the only one: the rolling daily window still
        # applies, and a session that ignored it would route around the guard that
        # exists to stop exactly this kind of ad-hoc spending.
        ("the rolling daily ceiling still binds",
         bool(refine._affordable(0.10, 0.20, 99.0, main.BUDGET_PER_DAY_USD - 0.15))),
        ("a comfortable day total does not block",
         refine._affordable(0.10, 0.20, 99.0, 0.0) == ""),
    ]


def _banner_cases():
    """What a reader is told must match what happened."""
    findings = [{"severity": "BLOCKING", "title": "x"}, {"severity": "MATERIAL", "title": "y"},
                {"severity": "MINOR", "title": "z"}]
    not_agreed = refine._not_agreed_banner(3, 1.42, findings, "the 3-round limit was reached")
    agreed = refine._agreed_banner(2, 0.88, [])
    return [
        ("un-agreed banner says so unambiguously", "has NOT agreed" in not_agreed),
        ("un-agreed banner counts blocking and material",
         "1 blocking and 1 material" in not_agreed),
        ("un-agreed banner gives the stopping reason", "3-round limit" in not_agreed),
        ("un-agreed banner reports the cost", "$1.42" in not_agreed),
        ("agreed banner says so", "agreed this report" in agreed),
        # Agreement is not a correctness claim, and the report must not read like one.
        ("agreed banner does not overclaim", "not a promise the call is right" in agreed),
        ("both banners use the same heading, so stripping finds either",
         not_agreed.startswith(refine.CRITIC_SECTION_HEADING)
         and agreed.startswith(refine.CRITIC_SECTION_HEADING)),
    ]


def _minor_carryover_cases():
    """A MINOR finding reaches the reader BECAUSE it did not trigger a revision.

    Taken from the first live run: the critic found that HRB's +17.4% quarterly net
    income growth was inflated by an $84.1M one-off tax settlement, graded it MINOR
    (operating profit and cash flow carried the thesis without it), and agreed. No
    revision ran, so the 'Required fix' was never applied — and the reader saw an
    unqualified +17.4% with the correction buried in the review. The banner is where
    that gets closed.
    """
    minor = ca.parse_findings(_MINOR_ONLY)
    hrb = [{
        "severity": "MINOR", "type": "Material omission",
        "title": "One-time tax settlement benefit in Q3 net income omitted",
        "finding": ("### Finding 1 — One-time tax settlement benefit\n"
                    "- **Severity:** MINOR\n"
                    "- **Why it is wrong:** growth was aided by a discrete tax benefit.\n"
                    "- **Evidence:** Q3 FY2026 Form 10-Q.\n"
                    "- **Required fix:** Add a brief parenthetical note in the Recent "
                    "Quarter Check stating that Q3 net income growth was aided by a "
                    "one-time $84.1 million tax settlement benefit.\n"),
    }]
    clean = refine._agreed_banner(1, 0.12, [])
    one = refine._agreed_banner(1, 0.12, hrb)
    two = refine._agreed_banner(2, 0.31, hrb + minor)
    # Blocking/material can never appear on an agreed report — extract_critic_verdict
    # forces REVISE — but if one ever leaked through it must not be listed as "minor".
    leaked = refine._agreed_banner(1, 0.12, [{"severity": "MATERIAL", "title": "leak",
                                              "finding": ""}])
    return [
        ("a clean agreement says nothing about minor points",
         "minor point" not in clean),
        ("one minor point is counted and named",
         "1 minor point." in one and "tax settlement benefit" in one),
        ("the reader is told it was NOT fixed", "not\nrevised for it" in one
         or "not revised for it" in one.replace("\n", " ")),
        ("the required fix is carried, not the critique of the prose",
         "$84.1 million tax settlement" in one and "growth was aided by a discrete" not in one),
        ("plurals agree", "2 minor points." in two and "revised for them" in two.replace("\n", " ")),
        ("a leaked blocking finding is not dressed up as minor", "minor point" not in leaked),
        # The honesty clause must survive: agreement is not a correctness claim.
        ("the not-a-promise caveat is still there", "not a promise the call is right" in one),
    ]


def _inlining_cases():
    """An inlined review must not outrank the container it is inlined under."""
    review = "## Findings\n### Finding 1 — x\n- **Severity:** MINOR\n#### deep\n"
    out = refine._demote_headings(review)
    assembled = refine._assemble({}, "## Recent Quarter Check\nbody\n", [],
                                 refine._not_agreed_banner(1, 0.1, [], "budget"),
                                 review, agreed=False)
    top_level = [l for l in assembled.splitlines() if l.startswith("## ")]
    return [
        ("top-level review headings are demoted", "#### Findings" in out),
        ("nested ones follow", "##### Finding 1 — x" in out),
        ("the demotion stops at h6", "###### deep" in out),
        ("body text is untouched", "- **Severity:** MINOR" in out),
        # The report's own outline must stay the analyst's five sections plus the
        # two this tool adds — never the critic's section names mixed in among them.
        ("the inlined review adds no top-level section",
         top_level == ["## Magic Formula Metrics", "## Recent Quarter Check",
                       refine.CRITIC_SECTION_HEADING]),
    ]


def _memory_cases():
    rows = [
        {"created_at": "2026-07-30T10:00:00+00:00", "iteration": 1, "severity": "BLOCKING",
         "title": "verdict rests on a rumour", "finding": "long text " * 200,
         "analyst_response": "1. FIXED — reweighed.", "status": "RESOLVED"},
    ]
    rendered = ca.format_past_corrections(rows)
    empty = ca.format_past_corrections([])
    return [
        ("empty memory reads as a first review", "first review" in empty),
        ("a stored finding is rendered", "verdict rests on a rumour" in rendered),
        ("its severity and status ride along",
         "BLOCKING" in rendered and "RESOLVED" in rendered),
        ("the analyst's reply is carried", "Analyst replied" in rendered),
        # This block is sent to BOTH agents on EVERY round, so an unbounded dump would
        # cost more than the round it exists to save.
        ("long findings are truncated", len(rendered) < 2000),
    ]


def run():
    groups = [
        ("critique parsing & agreement", _parsing_cases()),
        ("revision / response split", _split_cases()),
        ("stripping generated sections", _strip_cases()),
        ("spend projection", _budget_cases()),
        ("reader-facing banners", _banner_cases()),
        ("minor findings carried to the reader", _minor_carryover_cases()),
        ("inlining the critic's review", _inlining_cases()),
        ("long-term memory rendering", _memory_cases()),
    ]
    failures = []
    total = 0
    for label, cases in groups:
        print(f"--- {label} ---")
        for name, ok in cases:
            total += 1
            failures += [] if ok else [name]
            print(f"{'PASS' if ok else 'FAIL'}  {name}")
        print()

    print(f"{total - len(failures)}/{total} passed.")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
