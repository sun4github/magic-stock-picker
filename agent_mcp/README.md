# MCP server — the agent as tools

An [MCP](https://modelcontextprotocol.io) server that exposes every
magic-stock-picker CLI operation as a tool, so another agent — Claude Desktop,
Claude Code, or something running on a different machine entirely — can run the
screener, analyse a ticker, order a critic review, and read the reports that come
out, without a shell.

It is a **layer over** the pipeline, not part of it. Every tool here shells out to
the same command a person would type, from the same directory, with the same flags.
Nothing in `src/` imports anything from this package, and nothing here imports the
pipeline. **The CLI keeps working exactly as before whether this server is running
or not** — and stays the single implementation of every behaviour, so the two can
never drift apart.

> **Not the same thing as [`src/mcp_server.py`](../src/mcp_server.py).** That is also
> an MCP server, one layer down: it is the *agents'* toolbelt — fetch a filing, read
> FMP metrics, write a database row — consumed by the LLM agents *inside* a run. This
> one is the *operator's* toolbelt: start a run, watch it, read what it produced.
> They never talk to each other.

## Quick start

```bash
pip install -r agent_mcp/requirements.txt
python -m agent_mcp.server
```

That serves over stdio, which is what a local client wants. For Claude Desktop or
Claude Code, register it:

```json
{
  "mcpServers": {
    "magic-stock-picker": {
      "command": "python",
      "args": ["-m", "agent_mcp.server"],
      "cwd": "/path/to/magic-stock-picker"
    }
  }
}
```

Then ask the client to call **`describe_capabilities`** — it explains the rest.

## The two rules

Everything else here follows from these.

**1. Tools that spend money require `confirm=True`.** Called without it they return a
costed preview — the exact command, the estimate, the duration, what it will write —
and spend nothing. A full pipeline run is $11–13; an agent that can't be billed
should have to say so twice.

```
start_pipeline(top_n=30)                 -> preview: "$11.10-$16.50", spent nothing
start_pipeline(top_n=30, confirm=True)   -> started:  job_id
```

**2. `start_*` returns a job id, not a result.** Operations run for 2 to 65 minutes,
far longer than any MCP client will hold a tool call open. The work continues in its
own process — surviving this server restarting — and you poll it:

```
job_status(job_id)   state, elapsed time, exit code
job_log(job_id)      tail of the output, optionally filtered
job_cancel(job_id)   stop it; everything already finished is kept
```

## The tools

**Discovery** — `describe_capabilities(topic)`. Topics: `overview`, `operations`,
`costs`, `workflows`, `jobs`, `library`, `safety`, or any operation key for that one
in full. It is generated from the same catalogue the tools are built from, so it can
never quote a cost or a precondition that differs from the one actually enforced.

**Operations** — each launches a job.

| Tool | CLI equivalent | Cost | Time |
| :--- | :--- | :--- | :--- |
| `start_screen` | `main.py --screen-only` | free | 20–30 min |
| `start_pipeline` | `main.py` | $11–13 | 50–65 min |
| `start_analysis_from_csv` | `main.py --from-csv` | $0.37/ticker ($11–13 for 30) | 25–40 min |
| `start_ticker_analysis` | `main.py TICKER` | $0.37–0.55 | 2–5 min |
| `start_critic_review` | `refine.py TICKER` | $0.10–2.45 | 3–15 min |
| `start_sale_advisory` | `sale_advisory.py TICKER` | $0.08–0.10 | 1–3 min |
| `start_buy_case` | `buy_case.py TICKER` | $0.12–0.18 | 1–3 min |
| `start_sell_check` | `main.py --sell-check` | $0.06–0.10 | 1–3 min |
| `start_buy_check` | `main.py --buy-check` | $0.06–0.10 | 1–3 min |
| `start_buy_check_sweep` | `cron/run_buy_check_sweep.py` | free–$0.40 | 1–10 min |
| `start_ledger_snapshot` | `cron/run_outcome_snapshot.py` | free | 1–5 min |
| `start_growth_persistence_study` | `tools/analyze_growth_persistence.py` | free | 10–25 min |

**Jobs** — `job_status`, `job_log`, `job_list`, `job_cancel`.

**Read-only** — `list_reports`, `read_report`, `get_rankings`, `get_screen_config`.
Immediate, free, no subprocess. These matter more than they look: a client reaching
this server over the network shares no filesystem with it, and without them could
start a run and never read what the run produced.

## What it deliberately does not expose

- **`--no-budget`**, on any command. It exists on the CLI for a person who has
  decided to lift a spend ceiling. Removing the ceiling should not be one tool call
  away from an agent that cannot be billed. There is a test asserting no tool can
  emit it.
- **Editing `config.yaml`.** Threshold changes affect every future screen, and two of
  the PEG thresholds are multiplicatively linked — raise `max_peg` alone and you have
  silently raised the screen's hard P/E ceiling too. `get_screen_config` reads them,
  including the implied ceiling; changing them stays a deliberate edit.

The pipeline's own guards still apply underneath all of this: the per-run and rolling
24-hour ceilings from `config.yaml` halt a run cleanly rather than overspending.

## One billed job at a time

Billed operations are serialised server-wide. A second is refused and told which job
holds the slot. They share one rolling daily ceiling and one run-log directory, and
once several agents can call this server, two overlapping pipeline runs stops being a
theoretical problem — an expensive one, whose logs interleave. Free operations are
never blocked; waiting on an hour-long pipeline to read a price snapshot would be
absurd.

## Over the network

```bash
export MSP_MCP_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
python -m agent_mcp.server --transport http --host 0.0.0.0 --port 8765
```

Clients present `Authorization: Bearer <token>`; anything else gets a 401.

**Binding a non-loopback address without a token is refused outright.** These tools
spend real money and reach a production database — an open one on a network is not a
configuration this server will adopt. If you'd rather not manage a token, bind
`127.0.0.1` and reach it through an SSH tunnel.

The bearer check is deliberately the smallest thing that is actually correct: a
constant-time compare against a shared secret. For anything multi-tenant, put a real
proxy in front and keep this as the backstop. Note also that the token is the only
thing between a caller and a $13 run — treat it like a credential, and prefer
`MSP_MCP_TOKEN` over `--token`, which is visible to anyone who can list processes.

Set `requested_by` on `start_*` calls when several agents share one server; it is
recorded on the job and shown in `job_list`, which is how you answer "who started
this?".

### Environment

| Variable | Does |
| :--- | :--- |
| `MSP_MCP_TOKEN` | Bearer token clients must present (HTTP transports) |
| `MSP_MCP_ALLOWED_HOSTS` | Comma-separated `Host` allowlist, if the default (bind host plus loopback) is wrong for your proxy |
| `MSP_MCP_JOB_DIR` | Where job metadata and logs live. Default `agent_mcp/.jobs/` |
| `MSP_PYTHON` | Interpreter used to run the pipeline, if not the one running this server |

`MSP_PYTHON` is what lets this layer live in its own thin environment — it needs only
`mcp` and `pyyaml`, none of the agent stack — while driving a pipeline installed
somewhere heavier.

## Tests

```bash
python agent_mcp/test_agent_mcp.py
```

111 assertions covering the confirmation gate, the billed-job lock, argument
validation, the job lifecycle, cancellation, the readers and the discovery tool. They
never run the pipeline — every test that launches a job launches a trivial command
instead — so the suite is free, offline, and finishes in seconds. The job store is
redirected to a temporary directory, so running them never touches real job history.

## Files

| File | Holds |
| :--- | :--- |
| `server.py` | The FastMCP server: all 21 tools, the confirmation gate, the transports |
| `catalog.py` | The operation catalogue — cost, duration, caveats. Data, not behaviour |
| `jobs.py` | Launching, polling, tailing and cancelling background jobs |
| `library.py` | Read-only access to reports, rankings and config |
| `test_agent_mcp.py` | The suite above |

The design rationale — why subprocesses rather than imports, why jobs rather than
blocking calls, and the traps in both — is in
[`docs/13-mcp-server.md`](../docs/13-mcp-server.md).
