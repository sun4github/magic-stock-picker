"""The independent critic, the analyst's reviser, and the parsers that sit between
them.

This module holds only the two agents and the deterministic text handling around
them. The loop that runs them, pays for them, and persists what they produce lives
in `refine.py` — the same split as `main.py`'s agent definitions vs.
`_run_pipeline_async`, and for the same reason: the orchestration needs to be
readable on its own.

## Why a critic at all

A final report is a Buy/Watch/Avoid call a real person may act on with their own
savings. The pipeline already runs an adversarial pair (bear then bull) and a
neutral judge, but every one of those roles works from the SAME pre-fetched
evidence and the same prompt lineage, so a fallacy in how that evidence is weighed
has nothing standing outside it to catch it. The reconciliation gate catches wrong
FIGURES; nothing catches wrong REASONING.

The critic is deliberately outside that lineage:

- its own instruction file (`critic-instructions.md`), written without reference
  to the analyst's prompt, so it is not primed to accept the analyst's framing;
- its own tools (FMP news/profile/metrics/quarterly + Tavily), so it can check the
  report's load-bearing claims against the world rather than against the same
  cached blob the analyst read;
- no visibility of the analyst's instruction at all — it is given the finished
  report, the two source cases, and the verified figures, and nothing else.

## Why the reviser reuses the analyst's instruction verbatim

`reviser_agent` is built from `main.analyst_agent.instruction` plus a revision
block. That is not laziness — it is the point. The report that comes out of a
refinement round must obey exactly the same rules as one that comes out of the
pipeline: same five sections, same verdict vocabulary, same basket framing, same
plain-English standard. Re-stating those rules here would create a second copy
that drifts, and the first symptom would be a refined report that is subtly
different in kind from an unrefined one — the hardest sort of inconsistency to
notice, because both look fine in isolation.
"""
import os
import re
import time
import asyncio
import uuid

from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService
from google.genai import types

import main
from mcp_server import (
    fmp_stock_news,
    fmp_company_profile,
    fmp_metrics_extractor,
    fmp_quarterly_trends,
    web_search_tool,
)

logger = main.logger

# The critic's own instruction set. Like the other instruction files it contains no
# '{' or '}', so it is safe to embed in a state-templated ADK instruction.
_critic_path = os.path.join(os.path.dirname(__file__), "critic-instructions.md")
with open(_critic_path, "r", encoding="utf-8") as f:
    critic_instructions = f.read()


# --- Blocks shared by both agents ---------------------------------------------
# The critic sees the report under review, the two cases the analyst was given, and
# the same deterministic ground truth every pipeline agent gets. It does NOT see the
# analyst's instruction: a critic told how the report was supposed to be written
# grades against that spec instead of against reality, and the failure being hunted
# here (a verdict that does not follow from its own evidence) is invisible from
# inside the spec.
_CRITIC_CONTEXT_BLOCKS = (
    "<MAGIC_FORMULA_CONTEXT>\n{screen_context}\n</MAGIC_FORMULA_CONTEXT>\n\n"
    "<VERIFIED_FIGURES>\n{verified_figures}\n</VERIFIED_FIGURES>\n\n"
    "<QUARTERLY_DATA>\n{quarterly_data}\n</QUARTERLY_DATA>\n\n"
    "<BEAR_CASE>\n{bear_data}\n</BEAR_CASE>\n\n"
    "<BULL_CASE>\n{bull_data}\n</BULL_CASE>\n\n"
    "<PAST_CORRECTIONS>\n{past_corrections}\n</PAST_CORRECTIONS>\n\n"
    "<ANALYST_RESPONSE>\n{analyst_response}\n</ANALYST_RESPONSE>\n\n"
    "<REPORT_UNDER_REVIEW>\n{report_under_review}\n</REPORT_UNDER_REVIEW>\n"
)

critic_agent = LlmAgent(
    name="critic_agent",
    model=main.AGENT_MODEL,
    instruction=(
        critic_instructions
        + "\n\n## This review\n"
        "You are reviewing the finished research report for {company_name} ({ticker}), "
        "reproduced in REPORT_UNDER_REVIEW below. BEAR_CASE and BULL_CASE are the two "
        "arguments the analyst was given — check the report's summaries against them. "
        "PAST_CORRECTIONS holds findings from earlier review rounds (this session and "
        "earlier sessions for this company); ANALYST_RESPONSE holds the analyst's reply "
        "to the immediately preceding round, and is empty on the first round.\n\n"
        "Use your tools before writing findings. A review with no tool calls in it has "
        "verified nothing and will be treated as one.\n\n"
        + _CRITIC_CONTEXT_BLOCKS
    ),
    tools=[fmp_stock_news, web_search_tool, fmp_company_profile,
           fmp_metrics_extractor, fmp_quarterly_trends],
    output_key="critic_review",
    # Same reasoning as the pipeline agents: everything this agent reads is templated
    # from state, so replaying a transcript would only duplicate it at full price.
    include_contents=main.PIPELINE_INCLUDE_CONTENTS,
)


# Separator between the revised report and the analyst's point-by-point reply to the
# critic. A single agent turn has to carry both: the report must keep EXACTLY the
# five sections the analyst prompt mandates (a sixth "response to the critic" section
# would leak the argument between the two agents into the reader's report), while the
# critic must see the reply or it will re-raise every rejected finding for as many
# rounds as the budget allows. So the reply rides along after a marker and is split
# off in Python before anything is stored.
RESPONSE_MARKER = "<<<RESPONSE TO CRITIC>>>"

reviser_agent = LlmAgent(
    name="reviser_agent",
    model=main.AGENT_MODEL,
    instruction=(
        # Identical rules to the pipeline analyst — see the module docstring.
        main.analyst_agent.instruction
        + "\n\n## You are REVISING an existing report\n"
        "You already produced the report in PRIOR_REPORT below. An independent critic, "
        "who did not write it and has its own research tools, has reviewed it; its "
        "findings are in CRITIC_REVIEW. PAST_CORRECTIONS holds findings from earlier "
        "rounds, including earlier sessions for this same company.\n\n"
        "Rewrite the report IN FULL. Not a diff, not a changelog — the complete report, "
        "with exactly the same five sections and every rule above still applying. It "
        "replaces the previous one entirely, and a reader will see only your new "
        "version.\n\n"
        "For each finding, do one of exactly two things:\n"
        "- **Fix it.** Change the reasoning, the figure, or the verdict so the objection "
        "no longer applies. Fixing a finding means changing the report, not adding a "
        "sentence acknowledging the objection while leaving the argument intact.\n"
        "- **Rebut it.** If the critic is wrong, keep your position. You are not "
        "required to defer — a critic can misread a report or a figure. But a rebuttal "
        "must give the specific reason, and if the reason is something a reader also "
        "needs, put it in the report too.\n\n"
        "Changing the verdict is allowed and expected when the findings warrant it. A "
        "revision that fixes every objection but leaves an unsupportable verdict "
        "standing has fixed nothing. Equally, do not downgrade a verdict merely to "
        "placate the critic: the reader is a mid-career investor who can accept "
        "moderate, well-reasoned risk, and a wrongly cautious call costs them an "
        "opportunity just as a wrongly confident one costs them money.\n\n"
        "Do NOT reintroduce anything listed in PAST_CORRECTIONS as already corrected. "
        "That is what those records exist to prevent.\n\n"
        "## After the report — your reply to the critic\n"
        "When the report is complete, write this marker on a line of its own:\n"
        f"{RESPONSE_MARKER}\n"
        "and after it, one short numbered line per finding: its number, whether you "
        "FIXED or REBUTTED it, and in one sentence what you changed or why you "
        "disagree. This trailer is stripped out before the report is stored — it is "
        "read by the critic on the next round, never by the investor. Without it the "
        "critic cannot tell a rejected finding from an ignored one and will raise it "
        "again, which costs another paid round.\n\n"
        "<PAST_CORRECTIONS>\n{past_corrections}\n</PAST_CORRECTIONS>\n\n"
        "<CRITIC_REVIEW>\n{critic_review}\n</CRITIC_REVIEW>\n\n"
        "<PRIOR_REPORT>\n{report_under_review}\n</PRIOR_REPORT>\n"
    ),
    tools=[],
    output_key="revised_report",
    include_contents=main.PIPELINE_INCLUDE_CONTENTS,
)


# --- Parsing the critic's output ----------------------------------------------
# Every one of these is deterministic Python over the model's prose. The loop's
# stopping condition, its cost, and what a reader is told about the report's status
# all hang off them, so none of it is left to a second model call.

# Matched anywhere, then the LAST match wins: the instruction quotes the words
# "CRITIC VERDICT: AGREE" in its own format spec, and a model that echoes the format
# before filling it in would otherwise have its example read as its answer.
_CRITIC_VERDICT_RE = re.compile(r"critic\s*verdict[^A-Za-z]{0,12}(agree|revise)", re.IGNORECASE)
_FINDING_HEADER_RE = re.compile(r"^#{2,4}\s*Finding\b[^\n]*$", re.IGNORECASE | re.MULTILINE)
_SEVERITY_RE = re.compile(r"\*{0,2}Severity:?\*{0,2}\s*:?\s*\**\s*(BLOCKING|MATERIAL|MINOR)", re.IGNORECASE)
_TYPE_RE = re.compile(r"\*{0,2}Type:?\*{0,2}\s*:?\s*\**\s*([^\n]+)")
_NO_FINDINGS_RE = re.compile(r"^\s*no findings\.?\s*$", re.IGNORECASE | re.MULTILINE)

BLOCKING_SEVERITIES = ("BLOCKING", "MATERIAL")

# How much of one finding is kept in the database. Long enough to hold the defect,
# the evidence and the required fix; short enough that twenty of them replayed into a
# later prompt stay affordable.
_MAX_FINDING_CHARS = 3000


def parse_findings(review_text: str) -> list:
    """Extract the critic's findings as dicts of severity/type/title/finding.

    Format-tolerant on purpose. The instruction asks for a strict shape, but the
    loop's spend guard and the report's un-agreed banner both count severities, so a
    model that drifts to '#### Finding 2:' or drops the bold markers must not
    silently produce a findings list of zero — that would read as 'the critic found
    nothing' when it found several things.

    Returns [] only when the critic genuinely raised nothing.
    """
    text = review_text or ""
    headers = list(_FINDING_HEADER_RE.finditer(text))
    findings = []
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[h.start():end].strip()
        header_line = h.group(0).lstrip("#").strip()
        # "Finding 3 — misread the PEG" -> "misread the PEG"
        title = re.sub(r"^Finding\s*\d*\s*[-—:.]?\s*", "", header_line, flags=re.IGNORECASE).strip()
        sev = _SEVERITY_RE.search(block)
        typ = _TYPE_RE.search(block)
        findings.append({
            "severity": (sev.group(1).upper() if sev else "UNKNOWN"),
            "type": (typ.group(1).strip().strip("*").strip() if typ else "")[:80],
            "title": (title or "untitled finding")[:300],
            "finding": block[:_MAX_FINDING_CHARS],
        })

    if findings or _NO_FINDINGS_RE.search(text):
        return findings

    # No parsable findings and no explicit "No findings." — fall back to any severity
    # keyword in the text so a badly-formatted but genuinely critical review still
    # registers as one. Silently treating it as clean is the failure that matters.
    sev = _SEVERITY_RE.search(text)
    if sev:
        return [{
            "severity": sev.group(1).upper(),
            "type": "unparsed",
            "title": "unstructured critique (format not followed)",
            "finding": text[:_MAX_FINDING_CHARS],
        }]
    return []


# Pull one labelled line out of a finding block, stopping at the next bullet label
# ("- **Evidence:**"), the next heading, or the end.
def _labelled(pattern: str):
    return re.compile(
        rf"\*{{0,2}}{pattern}:?\*{{0,2}}\s*:?\s*(.+?)(?=\n\s*[-*]\s*\*{{0,2}}[A-Z]|\n#{{2,}}|\Z)",
        re.IGNORECASE | re.DOTALL,
    )


_REQUIRED_FIX_RE = _labelled("Required fix")
_WHY_WRONG_RE = _labelled("Why it is wrong")


def finding_summary(finding: dict, max_chars: int = 300) -> str:
    """One-line gist of a finding, for the reader-facing banner.

    Prefers the "Required fix" line over "Why it is wrong": a MINOR finding reaches
    the reader only when the analyst did NOT revise for it, so what the reader needs
    is the correction that was never applied, not the critique of the prose. Returns
    "" when neither label is present, and the caller falls back to the title alone.
    """
    block = finding.get("finding") or ""
    for rx in (_REQUIRED_FIX_RE, _WHY_WRONG_RE):
        m = rx.search(block)
        if m:
            text = " ".join(m.group(1).split())
            if text:
                return text[:max_chars]
    return ""


def extract_critic_verdict(review_text: str, findings: list) -> tuple:
    """Return (verdict, note) where verdict is 'AGREE' or 'REVISE'.

    The declared verdict is cross-checked against the severities the critic itself
    assigned, and the stricter of the two wins:

    - Declared AGREE while a BLOCKING/MATERIAL finding stands is a contradiction, and
      resolving it toward AGREE would ship a report the critic's own text says is not
      supportable. Forced to REVISE.
    - No parsable verdict line at all falls back to the severities, because a missing
      line is a formatting failure, not evidence of agreement.

    `note` is non-empty exactly when the declared verdict was overridden, so the
    caller can log why rather than leaving an unexplained extra round.
    """
    matches = _CRITIC_VERDICT_RE.findall(review_text or "")
    declared = matches[-1].upper() if matches else ""
    unresolved = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES]

    if not declared:
        verdict = "REVISE" if unresolved else "AGREE"
        return verdict, ("no 'CRITIC VERDICT:' line found; fell back to the "
                         f"{len(unresolved)} blocking/material finding(s) recorded")
    if declared == "AGREE" and unresolved:
        sev = ", ".join(sorted({f["severity"] for f in unresolved}))
        return "REVISE", (f"critic declared AGREE while {len(unresolved)} {sev} "
                          f"finding(s) stand; treated as REVISE")
    return declared, ""


def split_revision(raw_output: str) -> tuple:
    """Split the reviser's turn into (report, response_to_critic).

    The marker is written by the reviser and is not part of the report. A missing
    marker is tolerated — the whole turn becomes the report and the response is
    empty — because losing the reply costs one wasted round of re-raised findings,
    while treating a marker-less turn as a failure would throw away a good revision.
    """
    text = raw_output or ""
    if RESPONSE_MARKER not in text:
        return text.strip(), ""
    report, _, response = text.partition(RESPONSE_MARKER)
    return report.strip(), response.strip()


# --- Long-term memory rendering ------------------------------------------------
def format_past_corrections(rows: list) -> str:
    """Render stored critic findings as the PAST_CORRECTIONS prompt block.

    Kept compact deliberately. This block is sent to BOTH agents on EVERY round, so
    its size multiplies by the round count twice over; a verbatim dump of twenty
    3,000-character findings would cost more than the round it is trying to save.
    Each entry keeps what makes the finding recognisable — when, what, how it ended —
    and the first part of the text.
    """
    if not rows:
        return ("No previous critic findings are on record for this company. This is "
                "the first review.")
    lines = [
        "Findings from earlier review rounds for this company, newest first. Points "
        "already settled must not be re-raised or reintroduced; a point returning in "
        "a new draft is a regression and should be named as one.",
        "",
    ]
    for r in rows:
        when = (r.get("created_at") or "")[:10]
        status = r.get("status") or "OPEN"
        sev = r.get("severity") or "UNKNOWN"
        title = r.get("title") or "untitled"
        body = " ".join((r.get("finding") or "").split())[:600]
        lines.append(f"- [{when} | round {r.get('iteration')} | {sev} | {status}] {title}")
        if body:
            lines.append(f"  - {body}")
        resp = " ".join((r.get("analyst_response") or "").split())[:400]
        if resp:
            lines.append(f"  - Analyst replied: {resp}")
    return "\n".join(lines)


# --- Running one agent turn ----------------------------------------------------
async def _run_agent_async(agent, state: dict, message_text: str, usage: dict) -> dict:
    """One agent turn in its own throwaway session. Returns the resulting state.

    A fresh session per turn rather than one shared session for the whole loop.
    `include_contents='none'` means neither agent reads the transcript — both get
    everything through templated state — so a shared session would buy nothing and
    would cost a state-delta event per hand-off just to overwrite
    `report_under_review` with each revision. Building the state dict in Python
    instead makes each round's exact inputs plain to read, which matters when the
    question is why round 3 cost what it did.
    """
    session_service = InMemorySessionService()
    session_id = f"refine:{agent.name}:{uuid.uuid4().hex[:8]}"
    await session_service.create_session(
        app_name=main.APP_NAME, user_id="critic_loop", session_id=session_id, state=state,
    )
    runner = main._build_runner(agent, session_service)
    message = types.Content(role="user", parts=[types.Part(text=message_text)])
    async for event in runner.run_async(
        user_id="critic_loop", session_id=session_id, new_message=message
    ):
        main._add_event_usage(usage, event)
    final = await session_service.get_session(
        app_name=main.APP_NAME, user_id="critic_loop", session_id=session_id
    )
    return dict(final.state) if final else {}


def run_agent(agent, state: dict, message_text: str) -> tuple:
    """Run one agent turn with 429 backoff. Returns (output_text, usage).

    `usage` is created once, OUTSIDE the retry loop, so tokens burned on a
    rate-limited attempt are counted rather than dropped — the loop's budget guard
    reads this number, and a guard fed an understated cost is not a guard.
    """
    usage = main._new_usage()
    for attempt in range(main.MAX_AGENT_RETRIES):
        try:
            state_out = asyncio.run(_run_agent_async(agent, state, message_text, usage))
            return (state_out.get(agent.output_key, "") or ""), usage
        except Exception as e:
            if main._is_rate_limit_error(e) and attempt < main.MAX_AGENT_RETRIES - 1:
                wait = main.BASE_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    f"{agent.name} rate limited (429). Backing off {wait}s "
                    f"(attempt {attempt + 1}/{main.MAX_AGENT_RETRIES})."
                )
                time.sleep(wait)
                continue
            logger.error(f"{agent.name} failed: {e}")
            raise
    return "", usage
