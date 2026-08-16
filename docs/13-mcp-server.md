# 13 — The MCP tool layer

**Source:** [`agent_mcp/server.py`](../agent_mcp/server.py),
[`agent_mcp/jobs.py`](../agent_mcp/jobs.py),
[`agent_mcp/catalog.py`](../agent_mcp/catalog.py),
[`agent_mcp/library.py`](../agent_mcp/library.py)

**Operator-facing README:** [`agent_mcp/README.md`](../agent_mcp/README.md)

This doc is the *why*. For what the tools are and how to run the server, read the
README above; this covers the three design decisions that shape the code, the traps
each one avoids, and what is deliberately withheld.

## Two MCP servers, and why they are not the same thing

The repository now contains two, at different layers. Conflating them is the first
mistake available here, so before anything else:

| | [`src/mcp_server.py`](../src/mcp_server.py) (doc [03](03-mcp-tools-and-persistence.md)) | `agent_mcp/` (this doc) |
| :--- | :--- | :--- |
| Whose toolbelt | the **agents'** | the **operator's** |
| Called by | the LLM agents, inside a run | a client outside the system |
| Tools like | `fetch_sec_10k_data`, `db_store_final_report` | `start_pipeline`, `job_status`, `read_report` |
| Granularity | one fetch, one row | one whole run |
| Imported by | `main.py`, `critic_agent.py`, … | **nothing** |

They never call each other, and nothing in `src/` knows this package exists. The
dependency runs one way and only through the process boundary described below.

## Decision 1 — subprocesses, not imports

Every operation runs the CLI as a child process
([`jobs.launch`](../agent_mcp/jobs.py), line 210) rather than importing the pipeline
and calling a function. That looks like the clumsier option and is not, for four
reasons.

**Importing `main` is not free, and never was.** It configures the run logger and
pulls the entire agent stack in behind it — google-adk, the tool layer, pandas,
edgartools, a little under a thousand modules — as a side effect of the import
itself. That is the right shape for a process which does one run and exits. A server
that must stay up for days and launch many runs is not that process.

> This argument used to be stronger, and it is worth recording why it is not. Before
> the `src/` restructure, `main.py` was also the shared plumbing every other entry
> point imported, and it carried a `sys.modules.setdefault("main", …)` alias to stop
> a second copy of itself executing — which would otherwise have given a second set
> of agent objects and a second `FileHandler` on the same logger singleton. The
> restructure moved the shared code into `settings`, `agents`, `usage`, `report`,
> `pipeline`, `persistence` and `run_log`, none of which import `main`, so the cycle
> and its guard are both gone (see the header comment in
> [`src/main.py`](../src/main.py)). The import is merely heavy now, not hazardous.
> The next three reasons are the load-bearing ones.

**Two flags work by rebinding module globals.** `--top-n` and `--no-budget` both
reassign module-level names in the `__main__` block
([`src/main.py:761-772`](../src/main.py)). In-process, one job's flags would leak
into the next one's run — a `--top-n 5` invocation quietly capping the 30-ticker run
that followed it.

**Isolation.** A run that hangs on a wedged HTTP call, or dies on an unhandled
exception, takes its own process with it and nothing else. In-process it would take
the server, and with it every other caller's job.

**It makes the CLI promise structural rather than aspirational.** The requirement is
that the pipeline keeps working exactly as before. Because this layer's only route to
the pipeline is the same command line a person types, that holds by construction:
there is one implementation of every behaviour, the CLI's, and it is exercised
identically whether the call came from a terminal or from a tool. `git status` after
this layer landed showed one new directory and no modifications under `src/`.

### Two traps in the subprocess itself

Both are in [`jobs.launch`](../agent_mcp/jobs.py):

**`PYTHONIOENCODING=utf-8`.** The reports and log lines are full of em dashes, arrows
and check marks. Writing them through a redirected pipe on Windows otherwise picks
the ANSI code page and dies on the first one with a `UnicodeEncodeError` — a crash
caused purely by being run from here rather than from a terminal, which is the worst
kind to debug.

**`PYTHONUNBUFFERED=1`.** Without it the log file stays empty for minutes at a time
and `job_log` reports nothing while the job is plainly working.

`stdin` is `DEVNULL`, so nothing can ever block waiting on a prompt no one is there
to answer, and the child gets its own process group so a cancel takes the whole tree.

## Decision 2 — jobs, not blocking calls

A pipeline run takes about an hour; the screener alone takes 20–30 minutes. No MCP
client waits that long for a tool result, and a caller reaching the server over the
network has an HTTP timeout and possibly a proxy in between. So `start_*` launches
and returns a job id, and `job_status` / `job_log` report afterwards.

**Job state lives in files, not memory** — `agent_mcp/.jobs/<job_id>.json` alongside
`<job_id>.log`. That survives the server restarting, and lets two processes serving
the same checkout see the same jobs. Metadata is written to a temporary name and
renamed over the target ([`_write_meta`](../agent_mcp/jobs.py), line 85), because a
caller polling `job_status` while the monitor thread records completion must never
catch a half-written file.

**The `ORPHANED` state exists for one specific honesty problem.** A job marked
`RUNNING` whose monitor thread died with a previous server has no one left to record
its exit code. [`_reconcile`](../agent_mcp/jobs.py) (line 143) checks whether the pid
is still alive: if it is, `RUNNING` is still true; if it is not, the honest answer is
that we know it finished but not how. Reporting `SUCCEEDED` or `FAILED` there would be
a guess presented as a fact, so it reports `ORPHANED` and points at the log tail.

### The cancellation race, and why the ordering is what it is

[`jobs.cancel`](../agent_mcp/jobs.py) (line 348) writes `CANCELLED` to the metadata
**before** killing the process, which reads backwards until you trace the alternative.
Kill first, and the monitor thread wakes on the non-zero exit the kill caused and
writes `FAILED` before `cancel` reaches its own write — so a run the operator stopped
deliberately is reported back to them as one that broke. Recording the intent first
means [`_monitor`](../agent_mcp/jobs.py) (line 171) sees `CANCELLED` and preserves it,
keeping the exit code alongside. The test suite covers this specific ordering; it was
a real failure before it was a test.

On Windows the kill is `taskkill /T /F`, not `Popen.terminate()`. `terminate()` ends
only the direct child, leaving whatever the run had spawned still running and still
spending.

## Decision 3 — a catalogue the discovery tool reads

[`catalog.py`](../agent_mcp/catalog.py) is data: one `Operation` per CLI command,
carrying its cost range, typical duration, what it writes, its preconditions and its
one worthwhile caveat. Both the tool wrappers and `describe_capabilities` read from
it.

That is the entire point. A discovery tool is only worth having if it cannot lie, and
a hand-written description of what a tool costs drifts from what the tool charges the
first time a price changes. Here a cost is written once and enforced and advertised
from the same place. The suite asserts the agreement directly — every catalogued
operation resolves to a registered tool, and the `costs` topic matches the catalogue
entry for every operation.

Costs are the figures already published in the root README and `config.yaml`, not
fresh guesses, and ranges are kept as ranges: an estimate that pretends to more
precision than its source has is worse than one that shows its width.

## The confirmation gate

Every operation that makes billed LLM calls requires `confirm=True`. Without it,
[`_preview`](../agent_mcp/server.py) (line 78) returns the exact command, the
estimate, the duration and what it will write — and spends nothing.

This is the one piece of this layer with no CLI counterpart, and it is here because
the caller changed. A person typing `python main.py` has already decided to spend $13.
An agent calling `start_pipeline` may be exploring, may have misread a request, and
cannot be billed for getting it wrong. The preview makes the money visible at exactly
the moment the decision is made, and costs nothing to produce.

`_launch` ([line 100](../agent_mcp/server.py)) is the single path every `start_*` tool
goes through, so the gate cannot be bypassed by a tool forgetting to check it.

## One billed job at a time

[`running_billed_job`](../agent_mcp/jobs.py) (line 196) serialises billed work
server-wide; a second attempt is refused and told which job holds the slot. Billed
operations share one rolling daily spend ceiling and one run-log directory, and once
several agents can reach the same server — see the network section below — two
overlapping pipeline runs stops being a theoretical problem. Free operations are
never blocked by it.

The pipeline's own guards still sit underneath: the per-run and rolling 24-hour
ceilings from `config.yaml`, which halt cleanly rather than overspending (doc
[05](05-guardrails-cost-and-reuse.md)). This layer adds a gate, it does not replace
one.

## What is withheld, and why

**`--no-budget`, on every command.** It exists on the CLI for a person who has decided
to lift a spend ceiling for one invocation. Removing a ceiling should not be one tool
call away from a caller who cannot be billed. The test asserts it behaviourally —
every billed tool is called with every argument at its most permissive and the
resulting command checked — rather than by grepping the source, which also matches the
flag where `describe_capabilities` explains why it is unavailable.

**Editing `config.yaml`.** A threshold change affects every future screen, and two of
the PEG thresholds are multiplicatively linked: `max_peg × max_growth_rate_for_peg ×
100` is a hard P/E ceiling for the whole screen, so raising one alone silently raises
the ceiling too (root README, "One trap when adjusting").
[`get_screen_config`](../agent_mcp/library.py) returns that implied ceiling as a
computed field precisely so a reader sees the constraint instead of having to derive
it — but changing it stays a deliberate human edit.

## Reaching it over the network

The HTTP transport is bearer-token authenticated
([`_BearerAuth`](../agent_mcp/server.py)), and **binding a non-loopback address
without a token is refused outright** rather than warned about. These tools spend real
money and reach a production database; an open one on a network is not a configuration
this server will adopt.

Two details worth knowing:

- MCP's DNS-rebinding protection rejects *every* request when `allowed_hosts` is empty
  and protection is enabled, so `_serve_http` populates the list explicitly for a
  non-loopback bind (`MSP_MCP_ALLOWED_HOSTS` overrides it for a proxy).
- Ticker arguments are validated against `^[A-Za-z0-9.\-]{1,12}$`
  ([`library._safe_ticker`](../agent_mcp/library.py)) and **rejected rather than
  sanitised** — a caller that sent `../../etc/passwd` has a bug worth surfacing, and
  it is the only caller-supplied value that reaches both a path and a command line.

The read-only tools matter disproportionately here: a remote caller shares no
filesystem with the server, so without `read_report` it could start a run and never
read what the run produced.

`MSP_PYTHON` lets this layer live in its own environment — it needs only `mcp` and
`pyyaml`, none of the agent stack — while driving a pipeline installed somewhere
heavier. That is the same separation `requirements-cron.txt` makes for the ledger
snapshot job, for the same reason.

## Tests

```bash
python agent_mcp/test_agent_mcp.py
```

111 assertions, free and offline: every test that launches a job launches a trivial
Python command instead of the pipeline, and the job store is redirected to a temporary
directory so a test run never touches real job history. They cover the confirmation
gate (including that no preview starts a job, and that quoted cost tracks the
arguments), the billed-job lock, argument validation, the job lifecycle, the
cancellation ordering above, report-name parsing and windowing, and the
catalogue/discovery agreement.
