"""Reads the agent prompts in `prompts/` and composes them into instructions.

WHY THE PROMPTS ARE FILES
-------------------------
They used to be Python string literals spread through main.py — the analyst's
was ~185 lines assembled from four sources 200 lines apart, and answering "what
is the judge actually told?" meant reading code. They are prose, they are the
part of this system most often tuned, and they are edited by reading rather than
by programming. So they live in `prompts/*.md`, one file per agent, in the order
the model sees them.

TWO KINDS OF MARKER APPEAR IN THOSE FILES
-----------------------------------------
`<<NAME>>`  — a shared block, expanded here. The recency and verified-figures
              mandates go to several agents, and a rule that must hold for bear,
              bull and analyst alike should not be maintained in three places.
              `prompts/mandates/*.md` holds one copy of each.

`{name}`    — a session-state key, expanded by ADK at call time, NOT here. These
              are the data envelopes ({bear_data}, {verified_figures}, ...) and
              they are deliberately left in the .md so each file shows the whole
              prompt including where its inputs land.

THE BRACE RULE, AND WHY IT IS CHECKED RATHER THAN DOCUMENTED
------------------------------------------------------------
main.py used to carry the comment "Neither file contains '{' so both are safe to
embed in state-templated instructions". That was stricter than the truth and, being
a comment, could not stop anyone breaking it. ADK's actual behaviour (see
google/adk/utils/instructions_utils.py) is to scan for `{+[^{}]*}+` and:

  * leave the text alone if it is not a valid state name — so the literal JSON in
    the verdict-basis mandate, `[{"factor_key": ...}]`, passes through untouched;
  * substitute it if the name IS seeded in session state;
  * raise KeyError at call time if the name is a valid identifier that was never
    seeded, unless it is written `{name?}`.

That last case is the hazard: a prompt reading "return {ticker} in {json} format"
would crash mid-run, per ticker, after the run had already been paid for. So
`_check_placeholders` rejects any unknown key at load time, where the failure is
free and obvious.
"""
import os
import re

import yaml

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
MANDATES_DIR = os.path.join(PROMPTS_DIR, "mandates")

# Every key the pipeline seeds into session state before an agent runs, plus the
# `output_key`s earlier agents hand forward. A `{placeholder}` outside this set is
# a typo, and ADK would turn it into a KeyError on a live billed run.
#
# Keep in step with the seeding in `analyze_ticker` and with each agent's
# `output_key`. It is deliberately a hand-maintained list: deriving it from the
# code would make the check agree with whatever the code does, including when the
# code is wrong, which is the one thing a guard must not do.
KNOWN_STATE_KEYS = frozenset({
    # seeded directly before the graph runs
    "ticker", "company_name", "sec_data", "metrics_data", "screen_context",
    "verified_figures", "quarterly_data", "price_data",
    # handed forward between agents via output_key
    "bear_data", "bull_data", "final_report", "sale_data",
    # seeded by the standalone check flows
    "sale_conditions", "buy_conditions", "analysis_purpose"
})

_PLACEHOLDER = re.compile(r"{+[^{}]*}+")
_SENTINEL = re.compile(r"<<([A-Z_]+)>>")


def _read(path: str) -> str:
    """Read a prompt file, normalising line endings to '\\n'.

    Normalising matters more than it looks. The reuse fingerprint
    (`_prompt_version`) hashes the assembled instruction text, so anything that
    changes those bytes invalidates every stored report and makes the next
    --from-csv re-analyze the full list instead of reusing it. Opening a prompt in
    an editor that saves CRLF would otherwise do exactly that — same prose, ~200
    changed bytes, a fresh fingerprint and a re-billed run, with nothing in the
    diff to explain why. The fingerprint should move when the WORDS move."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n").replace("\r", "\n")


def _factor_keys() -> str:
    """The verdict ledger's controlled vocabulary, as the mandate renders it.

    Read from specs/config.yaml here rather than passed in from main, so the
    prompt layer owns its own inputs and can be exercised without importing the
    pipeline. config.yaml stays the single home of the vocabulary itself."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "specs", "config.yaml")
    with open(config_path, "r") as fh:
        config = yaml.safe_load(fh) or {}
    taxonomy = (config.get("verdict_ledger", {}) or {}).get("factor_taxonomy") or []
    return ", ".join(str(k).strip() for k in taxonomy if str(k).strip())


def load_mandate(name: str) -> str:
    """One shared prompt block from prompts/mandates/, fully expanded."""
    text = _read(os.path.join(MANDATES_DIR, f"{name}.md"))
    if "<<FACTOR_KEYS>>" in text:
        text = text.replace("<<FACTOR_KEYS>>", _factor_keys())
    return text


# Sentinel name -> mandate file. The names are the ones the constants had in
# main.py, so a prompt file reads the way the Python did.
_MANDATE_FILES = {
    "RECENCY_MANDATE": "recency",
    "VERIFIED_FIGURES_MANDATE": "verified-figures",
    "VERDICT_BASIS_MANDATE": "verdict-basis",
    "REFERENCE_DATA_BLOCKS": "reference-data-blocks",
    "RESEARCH_SOURCE_BLOCKS": "research-source-blocks",
}


def _check_placeholders(text: str, source: str) -> None:
    """Fail at load time on a `{placeholder}` that ADK could not resolve."""
    for match in _PLACEHOLDER.finditer(text):
        name = match.group().lstrip("{").rstrip("}").strip()
        if name.endswith("?"):          # ADK treats these as optional
            continue
        if not name.isidentifier():     # literal JSON, prose braces — left alone by ADK
            continue
        if name not in KNOWN_STATE_KEYS:
            raise ValueError(
                f"{source}: '{{{name}}}' is not a seeded session-state key. ADK would "
                f"raise KeyError for it partway through a billed run. Either seed it, "
                f"write it '{{{name}?}}' to allow it to be missing, or fix the typo. "
                f"Known keys: {', '.join(sorted(KNOWN_STATE_KEYS))}"
            )


def load_prompt(name: str) -> str:
    """Compose one agent's full instruction from prompts/<name>.

    Expands `<<MANDATE>>` sentinels, leaves `{state_key}` for ADK, and validates
    the result before any of it can reach a model."""
    text = _read(os.path.join(PROMPTS_DIR, name))

    def expand(match: re.Match) -> str:
        key = match.group(1)
        if key not in _MANDATE_FILES:
            raise ValueError(
                f"{name}: unknown marker <<{key}>>. Known markers: "
                f"{', '.join(sorted(_MANDATE_FILES))}"
            )
        return load_mandate(_MANDATE_FILES[key])

    text = _SENTINEL.sub(expand, text)
    _check_placeholders(text, name)
    return text
