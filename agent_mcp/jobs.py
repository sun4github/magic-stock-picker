"""Background job runner for the CLI operations.

Why jobs at all, rather than tools that just return an answer: a full pipeline run
takes the best part of an hour and the screener alone takes 20-30 minutes. No MCP
client waits that long for a tool result, and a caller reaching this server over the
network has an HTTP timeout and possibly a proxy between it and here. So every
operation that is not instant is *started* and then *polled* - `start_*` hands back
a job id immediately, and `job_status` / `job_log` report on it afterwards.

Why subprocesses, rather than importing the pipeline and calling it:

  * `src/main.py` is both the CLI entry point and the module every other entry point
    imports, and it says so at the top of the file: importing it builds the agent
    objects, reads the config, attaches handlers to a logger singleton, and aliases
    itself in `sys.modules` to stop a second copy loading. That is fine once per
    process and wrong in a server that must run many operations over days.
  * Two of the CLI flags (`--top-n`, `--no-budget`) work by rebinding module globals.
    In-process, one job's flags would leak into the next one's run.
  * A subprocess that hangs or dies takes nothing with it. In-process, it would take
    the server - and with it every other caller's job.
  * It keeps the promise that the CLI is unchanged and remains the only way the
    pipeline is invoked. This layer is a caller of the CLI, not a fork of it.

Job state lives in files, not memory, so it survives a server restart and so two
processes serving the same repo see the same jobs.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

#: Where job metadata and captured output live. Overridable so a deployment can put
#: them on a larger or faster volume than the checkout.
JOBS_DIR = Path(os.environ.get("MSP_MCP_JOB_DIR", PROJECT_ROOT / "agent_mcp" / ".jobs"))

RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
ORPHANED = "ORPHANED"
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, ORPHANED})

_IS_WINDOWS = os.name == "nt"

# Guards the read-modify-write of metadata files and the "one billed job" check.
# Both are short critical sections; nothing slow happens while it is held.
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _interpreter() -> str:
    """The Python that runs the pipeline.

    Defaults to the one running this server, which is right when the MCP layer is
    installed alongside the agent. `MSP_PYTHON` covers the case where the server runs
    in its own thin environment and the agent has a heavier one of its own.
    """
    return os.environ.get("MSP_PYTHON") or sys.executable


def _meta_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _log_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.log"


def _write_meta(meta: dict) -> None:
    """Write metadata atomically.

    A reader polling `job_status` while the monitor thread records completion must
    never catch a half-written file, so the write goes to a temporary name and is
    then renamed over the target.
    """
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    target = _meta_path(meta["job_id"])
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(target)


def _read_meta(job_id: str) -> Optional[dict]:
    path = _meta_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    """Is this process still running?

    Only consulted for jobs whose monitor thread we lost - that is, jobs started
    before a server restart. On the ordinary path the monitor records the exit code
    and nothing needs to guess.
    """
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _reconcile(meta: dict) -> dict:
    """Correct the state of a job this process is no longer watching.

    A job marked RUNNING whose monitor thread died with a previous server has no one
    left to record its exit code. If the process is gone, the honest answer is
    ORPHANED - we know it finished but not how - and the log tail is where the
    caller finds out. If it is still alive, RUNNING is still true.
    """
    if meta.get("state") != RUNNING:
        return meta
    if meta.get("monitored_by") == os.getpid():
        return meta
    if _pid_alive(int(meta.get("pid") or 0)):
        return meta
    with _LOCK:
        fresh = _read_meta(meta["job_id"]) or meta
        if fresh.get("state") != RUNNING:
            return fresh
        fresh["state"] = ORPHANED
        fresh["ended_at"] = _now()
        fresh["detail"] = (
            "The server restarted while this job was running, so its exit code was "
            "never observed. Read the end of the log to see how far it got."
        )
        _write_meta(fresh)
        return fresh


def _monitor(job_id: str, proc: subprocess.Popen, log_file) -> None:
    """Wait for a job to finish and record how it ended."""
    try:
        exit_code = proc.wait()
    finally:
        try:
            log_file.close()
        except OSError:
            pass
    with _LOCK:
        meta = _read_meta(job_id)
        if not meta:
            return
        # A cancel already recorded the outcome; the non-zero exit it caused is not
        # a failure and must not be relabelled as one.
        if meta.get("state") == CANCELLED:
            meta["exit_code"] = exit_code
        else:
            meta["state"] = SUCCEEDED if exit_code == 0 else FAILED
            meta["exit_code"] = exit_code
        meta["ended_at"] = _now()
        meta["monitored_by"] = None
        _write_meta(meta)


def running_billed_job() -> Optional[dict]:
    """The billed job currently in flight, if there is one.

    Billed operations are serialised. They share one rolling daily spend ceiling and
    one run-log directory, and with several network clients able to start work
    independently, two overlapping pipeline runs is a plausible accident rather than
    a theoretical one - an expensive accident, and one whose logs interleave.
    """
    for meta in list_jobs(limit=200):
        if meta.get("state") == RUNNING and meta.get("bills"):
            return meta
    return None


def launch(
    *,
    operation: str,
    argv: Sequence[str],
    cwd: Path,
    label: str,
    bills: bool,
    requested_by: Optional[str] = None,
) -> dict:
    """Start an operation and return its job record.

    `argv` is the command after the interpreter, e.g. `["main.py", "--screen-only"]`.
    """
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = f"{operation}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    command = [_interpreter(), *argv]

    env = os.environ.copy()
    # Unbuffered, or the log file stays empty for minutes at a time and `job_log`
    # reports nothing while the job is plainly working.
    env["PYTHONUNBUFFERED"] = "1"
    # The reports and log lines contain em dashes, arrows and check marks. Writing
    # them through a redirected pipe on Windows otherwise picks the ANSI code page
    # and dies on the first one with a UnicodeEncodeError - a crash caused purely by
    # being run from here rather than from a terminal.
    env["PYTHONIOENCODING"] = "utf-8"

    log_file = open(_log_path(job_id), "w", encoding="utf-8", buffering=1)
    log_file.write(f"$ {' '.join(shlex.quote(c) for c in command)}\n")
    log_file.write(f"# cwd: {cwd}\n# started: {_now()}\n\n")
    log_file.flush()

    # Own process group, so cancelling kills the whole tree rather than orphaning
    # whatever the run had spawned.
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0
    popen_kwargs = {"creationflags": creationflags} if _IS_WINDOWS else {"start_new_session": True}

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,  # nothing here is interactive; never block on a prompt
            env=env,
            **popen_kwargs,
        )
    except OSError as exc:
        log_file.close()
        raise RuntimeError(f"Could not start `{' '.join(command)}`: {exc}") from exc

    meta = {
        "job_id": job_id,
        "operation": operation,
        "label": label,
        "state": RUNNING,
        "bills": bills,
        "command": " ".join(shlex.quote(c) for c in command),
        "cwd": str(cwd),
        "pid": proc.pid,
        "started_at": _now(),
        "ended_at": None,
        "exit_code": None,
        "requested_by": requested_by,
        "monitored_by": os.getpid(),
        "log_file": str(_log_path(job_id)),
        "detail": None,
    }
    with _LOCK:
        _write_meta(meta)

    threading.Thread(
        target=_monitor, args=(job_id, proc, log_file), name=f"monitor-{job_id}", daemon=True
    ).start()
    return meta


def status(job_id: str) -> Optional[dict]:
    meta = _read_meta(job_id)
    if meta is None:
        return None
    meta = _reconcile(meta)
    if meta.get("started_at"):
        started = datetime.fromisoformat(meta["started_at"])
        end = datetime.fromisoformat(meta["ended_at"]) if meta.get("ended_at") else datetime.now(timezone.utc)
        meta["elapsed_seconds"] = int((end - started).total_seconds())
    return meta


def list_jobs(limit: int = 25, state: Optional[str] = None) -> list:
    if not JOBS_DIR.exists():
        return []
    metas = []
    for path in JOBS_DIR.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # a metadata file caught mid-write; the next poll will see it
        metas.append(_reconcile(meta))
    metas.sort(key=lambda m: m.get("started_at") or "", reverse=True)
    if state:
        metas = [m for m in metas if m.get("state") == state.upper()]
    return metas[:limit]


def read_log(job_id: str, tail_lines: int = 80, grep: Optional[str] = None) -> dict:
    """Return the tail of a job's captured output.

    Reads the last slice of the file rather than the whole thing - a pipeline run's
    log is megabytes, and the caller almost always wants the end of it.
    """
    path = _log_path(job_id)
    if not path.exists():
        return {"error": f"No log for job '{job_id}'."}

    size = path.stat().st_size
    window = 512 * 1024
    with open(path, "rb") as handle:
        if size > window:
            handle.seek(size - window)
            handle.readline()  # discard the partial line the seek landed inside
        text = handle.read().decode("utf-8", errors="replace")

    lines = text.splitlines()
    if grep:
        needle = grep.lower()
        lines = [line for line in lines if needle in line.lower()]
    truncated = len(lines) > tail_lines
    return {
        "job_id": job_id,
        "log_bytes": size,
        "lines_returned": min(len(lines), tail_lines),
        "truncated": truncated or size > window,
        "matched_filter": grep,
        "log": "\n".join(lines[-tail_lines:]),
    }


def cancel(job_id: str) -> dict:
    """Stop a running job, killing the whole process tree."""
    with _LOCK:
        meta = _read_meta(job_id)
        if meta is None:
            return {"error": f"No such job '{job_id}'."}
        meta = _reconcile(meta)
        if meta.get("state") in TERMINAL_STATES:
            return {
                "job_id": job_id,
                "state": meta["state"],
                "detail": "Already finished; nothing to cancel.",
            }
        pid = int(meta.get("pid") or 0)

        # Record the intent BEFORE killing anything. Killing first loses a race: the
        # monitor thread wakes on the non-zero exit the kill causes and writes FAILED
        # before this function gets to write CANCELLED, so a run the operator stopped
        # deliberately is reported back to them as one that broke. Marking it first
        # means the monitor sees CANCELLED and preserves it.
        meta["state"] = CANCELLED
        meta["ended_at"] = _now()
        meta["detail"] = (
            "Cancelled by request. Work already completed is kept - the pipeline "
            "writes each ticker's report and database rows as it finishes them."
        )
        _write_meta(meta)

    killed = False
    if _IS_WINDOWS:
        # terminate() ends only the direct child, leaving the pipeline's own
        # subprocesses running and still spending. /T takes the tree.
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
        killed = result.returncode == 0
        detail = (result.stderr or result.stdout).strip()
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed, detail = True, ""
        except (ProcessLookupError, PermissionError) as exc:
            killed, detail = False, str(exc)

    if not killed:
        # The kill failed, so the process may still be running and still spending.
        # Say so rather than leaving a CANCELLED record that overstates what happened.
        with _LOCK:
            current = _read_meta(job_id) or meta
            current["detail"] = (
                f"Cancellation requested but the process could not be killed: {detail}. "
                f"It may still be running as PID {pid} - check it by hand."
            )
            _write_meta(current)
            meta = current

    return {"job_id": job_id, "state": meta["state"], "killed": killed, "detail": meta.get("detail")}


def prune(keep: int = 100) -> dict:
    """Delete the metadata and logs of the oldest finished jobs."""
    metas = [m for m in list_jobs(limit=10_000) if m.get("state") in TERMINAL_STATES]
    removed = 0
    for meta in metas[keep:]:
        for path in (_meta_path(meta["job_id"]), _log_path(meta["job_id"])):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        removed += 1
    return {"removed": removed, "kept": min(len(metas), keep)}
