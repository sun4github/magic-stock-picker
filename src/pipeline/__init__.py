"""Running the analysis: what one ticker costs, and what is written down about it.

    reuse.py      the fingerprint that decides whether a run can be skipped
    execution.py  driving the agents over one shared ADK session
    verdict.py    reading a finished report back into data, and stamping it
    analysis.py   analyze_ticker — one ticker, end to end

`main` keeps the CLI and the workflows that choose which tickers to run; this is
what those workflows call. The split is between deciding WHICH tickers to spend on
and spending on one.
"""
from .analysis import analyze_ticker
from .execution import (
    _run_pipeline_async,
    _run_sell_check,
    _run_sell_check_async,
    run_pipeline,
)
from .reuse import (
    _DOWNSTREAM_PROMPTS,
    _analysis_key,
    _prompt_version,
    _read_prompt_files,
)
from .verdict import (
    _BENCHMARK_CACHE,
    _JSON_FENCE_RE,
    _VERDICT_BASIS_RE,
    _benchmark_price,
    _extract_verdict,
    _git_sha,
    _split_verdict_basis,
    _with_run_header,
)

__all__ = [
    "_BENCHMARK_CACHE",
    "_DOWNSTREAM_PROMPTS",
    "_JSON_FENCE_RE",
    "_VERDICT_BASIS_RE",
    "_analysis_key",
    "_benchmark_price",
    "_extract_verdict",
    "_git_sha",
    "_prompt_version",
    "_read_prompt_files",
    "_run_pipeline_async",
    "_run_sell_check",
    "_run_sell_check_async",
    "_split_verdict_basis",
    "_with_run_header",
    "analyze_ticker",
    "run_pipeline",
]
