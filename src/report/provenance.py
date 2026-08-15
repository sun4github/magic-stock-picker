"""Stamping a stored document with the run that produced it.

Separate from the analytical sections because it is not one: it says nothing about
the company, only about the document. Every stored artifact gets it — the bear case,
the bull case, the sale advisory, the final report, the critic review, the buy case.

It lives in `report/` rather than in `pipeline/` for a structural reason. It is the
one thing `buy_case_agent` needed from `pipeline`, and `pipeline` calls
`buy_case_agent` for Phase E, so the two imported each other and the cycle had to be
broken with a deferred import inside the function. Nothing about prepending a
banner to a string belongs to the pipeline, so moving it here removes the cycle
rather than working around it.
"""


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
