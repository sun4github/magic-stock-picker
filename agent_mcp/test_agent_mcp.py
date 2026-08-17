"""Tests for the MCP layer.

These cover the layer's own logic - the confirmation gate, the billed-job lock,
argument validation, the job lifecycle and the read-only readers. They deliberately
do NOT run the pipeline: every test that launches a job launches a trivial Python
command instead, so the suite is free, offline, and finishes in seconds.

Run from the repository root:

    python agent_mcp/test_agent_mcp.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Point the job store at a scratch directory BEFORE importing jobs, so a test run
# never touches the operator's real job history.
_TMP = tempfile.mkdtemp(prefix="agent_mcp_tests_")
os.environ["MSP_MCP_JOB_DIR"] = _TMP

# Printed BEFORE the import, and flushed, because that import is the slowest thing
# in the file - `mcp` pulls in pydantic, starlette, uvicorn and httpx, about a
# thousand modules. On a machine whose antivirus is scanning every file on read it
# can take minutes, and with the banner printed afterwards the suite looks hung when
# it is only loading. One line here is the difference between "it does nothing" and
# "it is still importing".
print("agent_mcp test suite: importing (this is the slow part)...", flush=True)
_IMPORT_STARTED = time.time()

from agent_mcp import catalog, jobs, library, server  # noqa: E402

print(f"  imports done in {time.time() - _IMPORT_STARTED:.1f}s", flush=True)

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not condition else ""))


# ---------------------------------------------------------------- catalogue

def test_catalogue() -> None:
    print("\nCatalogue")
    check("every operation has a unique key", len(catalog.BY_KEY) == len(catalog.OPERATIONS))
    check("every operation maps to a unique tool", len(catalog.BY_TOOL) == len(catalog.OPERATIONS))

    # The discovery tool's promise is that it never describes a tool that is not
    # there. That only holds if the catalogue and the registered tools agree.
    import asyncio

    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    missing = [op.tool for op in catalog.OPERATIONS if op.tool not in registered]
    check("every catalogued operation is a registered tool", not missing, f"missing: {missing}")

    billed_without_cost = [op.key for op in catalog.OPERATIONS if op.bills and not op.cost_usd]
    check("every billed operation carries a cost estimate", not billed_without_cost,
          f"{billed_without_cost}")

    free_with_cost = [op.key for op in catalog.OPERATIONS if not op.bills and op.cost_usd]
    check("no free operation claims a cost", not free_with_cost, f"{free_with_cost}")

    low, high = catalog.estimate_batch_cost(30)
    check("batch cost scales with ticker count", low == 11.1 and high == 16.5, f"{low}/{high}")
    check("cost formatting collapses a single figure", catalog.format_cost((0.1, 0.1)) == "~$0.10")
    check("free cost renders as free", catalog.format_cost(None).startswith("free"))


# --------------------------------------------------------- confirmation gate

def test_confirmation_gate() -> None:
    print("\nConfirmation gate")
    before = len(jobs.list_jobs(limit=500))

    for tool, kwargs in (
        (server.start_pipeline, {}),
        (server.start_analysis_from_csv, {"top_n": 5}),
        (server.start_ticker_analysis, {"ticker": "CROX"}),
        (server.start_critic_review, {"ticker": "CROX"}),
        (server.start_sale_advisory, {"ticker": "CROX"}),
        (server.start_buy_case, {"ticker": "CROX"}),
        (server.start_sell_check, {"ticker": "CROX"}),
        (server.start_buy_check, {"ticker": "CROX"}),
        (server.start_buy_check_sweep, {}),
    ):
        result = tool(**kwargs)
        check(f"{tool.__name__} previews without confirm", result.get("status") == "preview",
              f"got {result.get('status')}")
        check(f"{tool.__name__} preview names its cost", bool(result.get("estimated_cost")))

    after = len(jobs.list_jobs(limit=500))
    check("no preview started a job", before == after, f"{before} -> {after}")

    # The cost shown must track the arguments, or the preview is theatre.
    small = server.start_analysis_from_csv(top_n=5)
    large = server.start_analysis_from_csv(top_n=30)
    check("preview cost scales with top_n",
          small["estimated_cost_usd"][1] < large["estimated_cost_usd"][1])

    check("preview shows the exact command", "--top-n 5" in small["command"], small["command"])


def test_free_operations_need_no_confirm() -> None:
    print("\nFree operations")
    # A dry-run sweep makes no model calls, so demanding confirmation would be asking
    # the caller to approve spending nothing.
    result = server.start_buy_check_sweep(dry_run=True)
    check("dry-run sweep starts without confirm", result.get("status") == "started",
          f"got {result.get('status')}")
    if result.get("status") == "started":
        jobs.cancel(result["job_id"])

    check("dry-run sweep is not billed", not catalog.BY_KEY["buy_check_sweep"].bills is False)
    check("ledger snapshot is free", catalog.BY_KEY["ledger_snapshot"].bills is False)
    check("screen is free", catalog.BY_KEY["screen"].bills is False)
    check("growth study is free", catalog.BY_KEY["growth_persistence"].bills is False)


# ------------------------------------------------------------- validation

def test_argument_validation() -> None:
    print("\nArgument validation")
    for bad in ("../../etc/passwd", "A B", "", "CROX;rm -rf /", "TOOLONGTICKERNAME"):
        result = server.start_ticker_analysis(ticker=bad, confirm=True)
        check(f"rejects ticker {bad!r}", result.get("status") == "error", f"got {result}")

    check("accepts a dotted symbol", library._safe_ticker("BRK.B") == "BRK.B")
    check("upper-cases a symbol", library._safe_ticker("crox") == "CROX")

    check("rejects top_n below 1",
          server.start_pipeline(top_n=0, confirm=True).get("status") == "error")
    check("rejects a non-positive budget",
          server.start_critic_review(ticker="CROX", max_budget_usd=0, confirm=True).get("status") == "error")
    check("rejects max_rounds below 1",
          server.start_critic_review(ticker="CROX", max_rounds=0, confirm=True).get("status") == "error")
    check("rejects an unknown report kind", "error" in server.read_report(ticker="CROX", kind="nope"))
    check("rejects an unknown job state", "error" in server.job_list(state="SLEEPING"))


# ---------------------------------------------------------- job lifecycle

def _launch_trivial(seconds: float = 0, bills: bool = False, code: int = 0) -> dict:
    """Start a harmless job that behaves like a run without being one."""
    return jobs.launch(
        operation="test",
        argv=["-c", f"import time,sys; print('working'); time.sleep({seconds}); sys.exit({code})"],
        cwd=PROJECT_ROOT,
        label="test job",
        bills=bills,
    )


def _await_terminal(job_id: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = jobs.status(job_id)
        if meta and meta["state"] in jobs.TERMINAL_STATES:
            return meta
        time.sleep(0.1)
    return jobs.status(job_id) or {}


def test_job_lifecycle() -> None:
    print("\nJob lifecycle")
    meta = _launch_trivial()
    check("launch returns a job id", bool(meta.get("job_id")))
    check("a new job is RUNNING", meta["state"] == jobs.RUNNING)

    final = _await_terminal(meta["job_id"])
    check("a clean exit is SUCCEEDED", final.get("state") == jobs.SUCCEEDED, f"{final.get('state')}")
    check("exit code is recorded", final.get("exit_code") == 0)
    check("elapsed time is reported", "elapsed_seconds" in final)

    log = jobs.read_log(meta["job_id"])
    check("stdout is captured", "working" in log.get("log", ""))
    check("the log records the command", "$ " in log.get("log", ""))

    failed = _await_terminal(_launch_trivial(code=3)["job_id"])
    check("a non-zero exit is FAILED", failed.get("state") == jobs.FAILED, f"{failed.get('state')}")
    check("the failing exit code is kept", failed.get("exit_code") == 3)

    check("an unknown job id is reported", "error" in server.job_status("no-such-job"))
    check("an unknown job's log is reported", "error" in jobs.read_log("no-such-job"))


def test_log_filtering() -> None:
    print("\nLog filtering")
    meta = _launch_trivial()
    _await_terminal(meta["job_id"])
    filtered = jobs.read_log(meta["job_id"], grep="working")
    check("grep keeps matching lines", "working" in filtered["log"])
    check("grep drops non-matching lines", "cwd:" not in filtered["log"])
    check("grep is reported back", filtered["matched_filter"] == "working")
    check("tail_lines is bounded", jobs.read_log(meta["job_id"], tail_lines=1)["lines_returned"] == 1)


def test_cancellation() -> None:
    print("\nCancellation")
    meta = _launch_trivial(seconds=20)
    time.sleep(0.5)
    result = jobs.cancel(meta["job_id"])
    check("cancel reports CANCELLED", result.get("state") == jobs.CANCELLED, f"{result}")

    final = _await_terminal(meta["job_id"])
    check("state stays CANCELLED after the process dies", final.get("state") == jobs.CANCELLED,
          f"{final.get('state')}")
    # The monitor sees a non-zero exit from the kill; relabelling that as FAILED
    # would tell the operator their run broke when in fact they stopped it.
    check("a cancelled job is never relabelled FAILED", final.get("state") != jobs.FAILED)
    check("cancelling a finished job is harmless", "state" in jobs.cancel(meta["job_id"]))
    check("cancelling an unknown job errors", "error" in jobs.cancel("no-such-job"))


def test_billed_job_lock() -> None:
    print("\nBilled-job serialisation")
    holder = _launch_trivial(seconds=8, bills=True)
    time.sleep(0.3)

    # Queued, not refused. Two questions arriving seconds apart over Telegram are
    # both things the person wants answered.
    second = server.start_ticker_analysis(ticker="CROX", confirm=True)
    check("a second billed job is queued, not refused",
          second.get("status") == "queued", f"{second.get('status')}")
    check("the queued job reports its position", second.get("queue_position") == 1,
          f"{second.get('queue_position')}")
    check("the queued job names what it waits behind",
          (second.get("waiting_behind") or {}).get("job_id") == holder["job_id"])
    check("a queued job still gets a job id", bool(second.get("job_id")))

    # Submitted immediately after `second`, i.e. within the same second. Ordering
    # must still be correct: this is the ordinary case when two messages arrive back
    # to back, and a second-resolution timestamp made it a coin toss.
    third = server.start_ticker_analysis(ticker="MSFT", confirm=True)
    check("a third queues behind the second", third.get("queue_position") == 2,
          f"{third.get('queue_position')}")

    listing = server.job_list()
    check("job_list exposes the queue in order",
          [q["job_id"] for q in listing["queue"]] == [second["job_id"], third["job_id"]],
          f"{[q['job_id'] for q in listing['queue']]}")
    check("positions are distinct", len({q["position"] for q in listing["queue"]}) == 2,
          f"{[q['position'] for q in listing['queue']]}")
    check("same-second submissions get distinct timestamps",
          jobs.status(second["job_id"])["queued_at"]
          != jobs.status(third["job_id"])["queued_at"],
          f"{jobs.status(second['job_id'])['queued_at']}")

    # Free work must not be blocked by the billed lock - it costs nothing and waiting
    # on an hour-long pipeline to read a snapshot would be absurd.
    free = server.start_ledger_snapshot()
    check("a free job runs alongside a billed one", free.get("status") == "started", f"{free}")
    if free.get("status") == "started":
        jobs.cancel(free["job_id"])

    # Cancelling a queued job must not try to kill a pid it does not have.
    dropped = jobs.cancel(third["job_id"])
    check("a queued job cancels without a process", dropped.get("state") == jobs.CANCELLED,
          f"{dropped}")
    check("cancelling a queued job spends nothing",
          "nothing spent" in (dropped.get("detail") or ""), f"{dropped.get('detail')}")
    check("cancelling reorders the queue", len(jobs.queued_jobs()) == 1)

    # CRITICAL: `second` is a real `main.py CROX` command. Before the queue existed a
    # second billed submission was REFUSED, so nothing was ever created and these
    # assertions cost nothing. Now it is queued - and freeing the slot would DISPATCH
    # it, running a genuine $0.37-0.55 analysis from the test suite. Drop it first.
    # Nothing in this file may leave a real pipeline command reachable by the
    # dispatcher; that is what `_launch_trivial` is for.
    jobs.cancel(second["job_id"])
    check("no real pipeline command is left queued", not jobs.queued_jobs(),
          f"{[m['label'] for m in jobs.queued_jobs()]}")

    # The dispatch path itself is exercised with a harmless command instead.
    waiter = _launch_trivial(seconds=1, bills=True)
    check("the stand-in queues behind the holder",
          jobs.status(waiter["job_id"])["state"] == jobs.QUEUED)

    jobs.cancel(holder["job_id"])
    deadline = time.time() + 20
    while time.time() < deadline:
        if jobs.status(waiter["job_id"])["state"] != jobs.QUEUED:
            break
        time.sleep(0.2)
    check("the queued job starts when the slot frees",
          jobs.status(waiter["job_id"])["state"] in (jobs.RUNNING, jobs.SUCCEEDED),
          f"{jobs.status(waiter['job_id'])['state']}")

    _await_terminal(waiter["job_id"])
    check("the slot frees once it finishes", jobs.running_billed_job() is None)


def test_queue_drains_in_order() -> None:
    print("\nQueue drains in order")
    a = _launch_trivial(seconds=2, bills=True)
    time.sleep(0.3)
    b = _launch_trivial(seconds=2, bills=True)
    c = _launch_trivial(seconds=2, bills=True)

    check("only the first runs", jobs.status(a["job_id"])["state"] == jobs.RUNNING)
    check("the rest wait", [jobs.status(j["job_id"])["state"] for j in (b, c)]
          == [jobs.QUEUED, jobs.QUEUED])

    deadline = time.time() + 40
    while time.time() < deadline:
        if all(jobs.status(j["job_id"])["state"] in jobs.TERMINAL_STATES for j in (a, b, c)):
            break
        time.sleep(0.3)

    states = [jobs.status(j["job_id"])["state"] for j in (a, b, c)]
    check("every queued job eventually ran", states == [jobs.SUCCEEDED] * 3, f"{states}")
    check("each produced its own output",
          all("working" in jobs.read_log(j["job_id"])["log"] for j in (a, b, c)))

    # The whole point of serialising: they must not have overlapped.
    sa, sb, sc = (jobs.status(j["job_id"]) for j in (a, b, c))
    check("they never overlapped",
          sb["started_at"] >= sa["ended_at"] and sc["started_at"] >= sb["ended_at"],
          f"{sa['ended_at']} / {sb['started_at']} / {sb['ended_at']} / {sc['started_at']}")


def test_queue_depth_limit() -> None:
    print("\nQueue depth limit")
    holder = _launch_trivial(seconds=8, bills=True)
    time.sleep(0.3)
    filled = [_launch_trivial(seconds=1, bills=True) for _ in range(jobs.MAX_QUEUE_DEPTH)]
    check("the queue fills to its limit", len(jobs.queued_jobs()) == jobs.MAX_QUEUE_DEPTH,
          f"{len(jobs.queued_jobs())}")

    overflow = server.start_ticker_analysis(ticker="CROX", confirm=True)
    check("beyond the limit is refused rather than queued",
          overflow.get("status") == "error", f"{overflow.get('status')}")
    check("the refusal explains the limit",
          "queued" in (overflow.get("error") or "").lower(), f"{overflow.get('error')}")

    for job in filled + [holder]:
        jobs.cancel(job["job_id"])
    time.sleep(0.5)


def test_job_listing() -> None:
    print("\nJob listing")
    listing = server.job_list(limit=5)
    check("listing returns jobs", isinstance(listing.get("jobs"), list))
    check("listing reports the billed slot", "billed_slot" in listing)
    check("limit is honoured", len(listing["jobs"]) <= 5)
    check("state filter is honoured",
          all(j["state"] == jobs.SUCCEEDED for j in server.job_list(state="SUCCEEDED")["jobs"]))


# ----------------------------------------------------------- read-only side

def test_library() -> None:
    print("\nRead-only library")
    parsed = library._parse_report_name("CROX_Final_Report_Watch.md")
    check("parses ticker from a filename", parsed and parsed["ticker"] == "CROX")
    check("parses kind from a filename", parsed and parsed["kind"] == "final")
    check("parses the verdict", parsed and parsed["verdict"] == "Watch")

    advisory = library._parse_report_name("MRVL_Sale_Advisory.md")
    check("parses an advisory", advisory and advisory["kind"] == "sale_advisory")
    check("an advisory has no verdict", advisory and advisory["verdict"] is None)

    refined = library._parse_report_name("FISV_Refined_Report_Watch.md")
    check("distinguishes refined from final", refined and refined["kind"] == "refined")
    check("ignores non-reports", library._parse_report_name("notes.txt") is None)

    listing = server.list_reports(limit=5)
    check("lists reports", isinstance(listing.get("reports"), list))
    check("reports the available kinds", "kinds_available" in listing)

    missing = server.read_report(ticker="ZZZZ", kind="final")
    check("a missing report explains itself", "error" in missing and "hint" in missing)

    config = server.get_screen_config()
    if "error" not in config:
        peg = config["peg_thresholds"]
        check("reads max_peg from config", peg["max_peg"] is not None)
        check("derives the implied P/E ceiling", peg["implied_hard_pe_ceiling"] is not None,
              f"{peg}")
        check("budget ceilings are exposed", config.get("budget") is not None)


def test_report_windowing() -> None:
    print("\nReport windowing")
    listing = library.list_reports(limit=1)
    if not listing["reports"]:
        print("  SKIP  no stored reports to window")
        return
    entry = listing["reports"][0]
    first = library.read_report(entry["ticker"], entry["kind"], max_chars=200)
    check("windows a long report", first["returned_chars"] <= 200)
    if first["total_chars"] > 200:
        check("offers the next offset", first["next_offset"] == 200, f"{first['next_offset']}")
        second = library.read_report(entry["ticker"], entry["kind"], max_chars=200, offset=200)
        check("continues from the offset", second["content"] != first["content"])
    else:
        check("a short report has no next offset", first["next_offset"] is None)


# --------------------------------------------------------------- discovery

def test_discovery() -> None:
    print("\nDiscovery tool")
    overview = server.describe_capabilities()
    check("overview lists every operation",
          len(overview["operations"]) == len(catalog.OPERATIONS))
    check("overview states the two rules", len(overview["two_rules"]) == 2)

    for topic in ("operations", "costs", "workflows", "jobs", "library", "safety"):
        result = server.describe_capabilities(topic)
        check(f"topic '{topic}' resolves", result.get("topic") == topic, f"{result.get('topic')}")

    single = server.describe_capabilities("refine")
    check("an operation key resolves to that operation",
          single.get("operation", {}).get("tool") == "start_critic_review")

    check("an unknown topic falls back to the overview",
          server.describe_capabilities("nonsense")["topic"] == "overview")

    # The costs topic is the one a caller trusts before spending; it must agree with
    # what the tools would actually charge.
    costs = server.describe_capabilities("costs")
    listed = {row["tool"]: row["cost"] for row in costs["operations"]}
    mismatched = [
        op.tool for op in catalog.OPERATIONS
        if listed.get(op.tool) != catalog.format_cost(op.cost_usd)
    ]
    check("the costs topic matches the catalogue", not mismatched, f"{mismatched}")


def test_email_report_option() -> None:
    print("\nEmailed reports")
    plain = server.start_ticker_analysis(ticker="CROX")
    mailed = server.start_ticker_analysis(ticker="CROX", email_report=True)

    check("without email_report it runs main.py directly",
          "main.py" in plain["command"] and "analyze_and_email" not in plain["command"],
          plain["command"])
    check("with email_report it runs the emailing wrapper",
          "tools/analyze_and_email.py" in mailed["command"], mailed["command"])
    check("the wrapper still receives the ticker", " CROX" in mailed["command"],
          mailed["command"])

    flagged = server.start_ticker_analysis(ticker="CROX", email_report=True,
                                           skip_buy_case=True, force=True)
    check("flags are passed through to the wrapper",
          "--skip-buy-case" in flagged["command"] and "--force" in flagged["command"],
          flagged["command"])

    # The recipient is fixed on the host. A network-reachable tool that accepted a
    # destination would let anyone holding the bearer token send mail from the
    # user's domain to anywhere.
    import inspect

    params = inspect.signature(server.start_ticker_analysis).parameters
    offenders = [p for p in params if p in ("to", "email", "recipient", "email_to")]
    check("no recipient can be passed in", not offenders, f"{offenders}")

    check("emailing is still gated by confirm", mailed.get("status") == "preview",
          f"{mailed.get('status')}")

    wrapper = PROJECT_ROOT / "tools" / "analyze_and_email.py"
    check("the wrapper the tool names actually exists", wrapper.exists(), str(wrapper))


def test_no_budget_flag_is_unreachable() -> None:
    print("\nWithheld flags")
    # --no-budget removes the pipeline's spend ceiling. It exists on the CLI for a
    # person who has decided to lift it; it must not be one tool call away from an
    # agent that cannot be billed. Asserted behaviourally - against the commands the
    # tools actually build - rather than by grepping the source, which also matches
    # the flag's name where describe_capabilities explains why it is withheld.
    import inspect

    # Every billed tool, called with every argument it accepts, at its most permissive.
    permissive = (
        (server.start_pipeline, {"top_n": 5, "skip_sale_advisor": True,
                                 "skip_buy_case": True, "force": True}),
        (server.start_analysis_from_csv, {"top_n": 5, "csv_path": "x.csv",
                                          "skip_sale_advisor": True, "skip_buy_case": True,
                                          "force": True}),
        (server.start_ticker_analysis, {"ticker": "CROX", "company": "Crocs", "force": True,
                                        "skip_sale_advisor": True, "skip_buy_case": True}),
        (server.start_critic_review, {"ticker": "CROX", "company": "Crocs", "run_id": "r1",
                                      "max_budget_usd": 5.0, "max_rounds": 4}),
        (server.start_sale_advisory, {"ticker": "CROX", "company": "Crocs", "run_id": "r1"}),
        (server.start_buy_case, {"ticker": "CROX", "company": "Crocs", "run_id": "r1"}),
        (server.start_sell_check, {"ticker": "CROX", "company": "Crocs", "run_id": "r1"}),
        (server.start_buy_check, {"ticker": "CROX", "company": "Crocs", "run_id": "r1"}),
        (server.start_buy_check_sweep, {}),
    )
    for tool, kwargs in permissive:
        command = tool(**kwargs).get("command", "")
        check(f"{tool.__name__} never emits --no-budget", "--no-budget" not in command, command)

    # And no tool offers it as a parameter, so it cannot arrive by another route.
    offenders = [
        name for name, fn in vars(server).items()
        if name.startswith("start_") and callable(fn)
        and any("budget" in p and "max_budget" not in p
                for p in inspect.signature(fn).parameters)
    ]
    check("no tool exposes a budget-disabling parameter", not offenders, f"{offenders}")


def main() -> int:
    print("=" * 70)
    print("agent_mcp test suite")
    print(f"job store: {_TMP}")
    print("=" * 70)

    test_catalogue()
    test_confirmation_gate()
    test_free_operations_need_no_confirm()
    test_argument_validation()
    test_job_lifecycle()
    test_log_filtering()
    test_cancellation()
    test_billed_job_lock()
    test_queue_drains_in_order()
    test_queue_depth_limit()
    test_job_listing()
    test_library()
    test_report_windowing()
    test_discovery()
    test_email_report_option()
    test_no_budget_flag_is_unreachable()

    print("\n" + "=" * 70)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
