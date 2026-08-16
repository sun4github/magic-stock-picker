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
python -m agent_mcp.server --transport http --host 0.0.0.0 --port 8765
```

Clients present `Authorization: Bearer <token>`; anything else gets a 401.

**Binding a non-loopback address without a token is refused outright.** These tools
spend real money and reach a production database — an open one on a network is not a
configuration this server will adopt. If you'd rather not manage a token, bind
`127.0.0.1` and reach it through an SSH tunnel.

The bearer check is deliberately the smallest thing that is actually correct: a
constant-time compare against a shared secret. For anything multi-tenant, put a real
proxy in front and keep this as the backstop.

Set `requested_by` on `start_*` calls when several agents share one server; it is
recorded on the job and shown in `job_list`, which is how you answer "who started
this?".

### `MSP_MCP_TOKEN`: one secret, two copies

This is the part worth being precise about, because the token lives in **two places
and both are required**. It is not issued by any service — you invent a random
string, and both ends have to hold the same one:

| Copy | Lives in | What it does |
| :--- | :--- | :--- |
| **Server's** | `.env` in the repo root (or a real environment variable) | the value the server **compares against**. Never leaves the machine |
| **Client's** | `openclaw.json` → `headers.Authorization` | the value the client **sends over the wire**, on every request |

The client puts it in an HTTP header; the server checks that header against its own
copy with a constant-time compare and returns 401 on any mismatch. Same idea as an
API key: the presenter needs it to present, the verifier needs it to verify.

**Generate one:**

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Server side** — put it in `.env` alongside `FMP_API_KEY` and the rest. The server
reads that file explicitly at startup, so it works under a service manager, where the
environment is whatever the scheduler happened to hand the process:

```env
MSP_MCP_TOKEN=paste-the-generated-string-here
```

`.env` is already gitignored, so the token is not committed. A real environment
variable still wins over the file, which is what lets a systemd `Environment=` or a
container secret override it without editing anything.

Prefer either over `--token`, which is visible to anyone who can list processes.

**Rotating it:** change both copies and restart the server. Changing only one gives
every call a 401 — which, if you ever see one, is almost always what has happened.

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

## Does it need to be running all the time?

**It depends entirely on the transport, and the transport depends on where the client
is.**

| | stdio | HTTP |
| :--- | :--- | :--- |
| Who starts the server | the **client** does, as a child process | **you** do, ahead of time |
| Always-on? | no — it starts and stops with the client | **yes** |
| Client must be | on this machine | anywhere |
| Auth | none needed (a pipe, not a socket) | bearer token |
| Good for | Claude Desktop / Claude Code on the same box | OpenClaw, or any remote agent |

**For a remote agent, HTTP — so yes, it has to be running before the client connects,
and stay running.** There is no way for a client on another machine to spawn a process
on this one.

Two properties make the always-on case work, and both are worth knowing:

**Jobs outlive the server.** Launched runs are detached into their own process group,
so restarting the server does not kill a pipeline mid-run. What is lost is the monitor
thread watching it, which is exactly what the `ORPHANED` state reports — the run
finished, but this server was not there to see how. A server that stays up avoids that;
a server that crashes does not lose your $13 run.

**Job state is on disk, not in memory.** So a restart loses nothing else, and
`--stateless` is safe: pass it and a restart never strands a client holding a session
id the server has forgotten.

### As a Windows service — the scripted way

```bat
tools\setup_mcp_server.cmd
```

Run it once, from anywhere. Same conventions as `tools/setup_weekly_jobs.cmd`: it
derives the repo root from its own location, finds an interpreter (rejecting the
Microsoft Store stub), smoke-tests that the dependencies import, and only then
registers anything.

It also handles the token: if `.env` has no `MSP_MCP_TOKEN` it generates one and
appends it, and if there already is one it leaves it alone — so re-running never
invalidates a client that is already configured. At the end it prints the OpenClaw
config with the token filled in and this machine's IPv4 addresses to choose from.

Pass a port as the first argument if 8765 is taken: `tools\setup_mcp_server.cmd 9000`.

The registered task starts at boot, runs as you (so it reads the same `.env`), has
its execution time limit removed — the default would kill a long-running server after
three days — and restarts up to three times if it stops.

```bat
schtasks /Run    /TN "MagicStockPicker-MCP"     REM start it now
schtasks /Query  /TN "MagicStockPicker-MCP" /V /FO LIST
schtasks /End    /TN "MagicStockPicker-MCP"     REM stop
schtasks /Delete /TN "MagicStockPicker-MCP" /F  REM remove
```

If the client is on another machine, open the port once:

```bat
netsh advfirewall firewall add rule name="MagicStockPicker MCP" dir=in action=allow protocol=TCP localport=8765
```

For real crash supervision — restart on exit, backoff, log rotation — NSSM
(`nssm install MagicStockPickerMCP`) is better than Task Scheduler, which launches
rather than supervises.

### As a Linux service (systemd)

```ini
[Unit]
Description=Magic Stock Picker MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=msp
WorkingDirectory=/home/msp/magic-stock-picker
Environment=MSP_MCP_TOKEN=your-token-here
ExecStart=/usr/bin/python3 -m agent_mcp.server --transport http --host 0.0.0.0 --stateless
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now magic-stock-picker-mcp
```

Prefer an `EnvironmentFile=` over an inline `Environment=` for the token — a unit file
is world-readable by default.

### Health check

`GET /health` needs no token and reports liveness only:

```bash
curl http://localhost:8765/health
# {"status":"ok","server":"magic-stock-picker"}
```

It deliberately reveals nothing about jobs; that is `job_list`, behind the token.

## Configuring OpenClaw

OpenClaw keeps MCP servers in `~/.openclaw/openclaw.json` under `mcp.servers`.

**Remote (the normal case)** — the server is running on the box with the pipeline.
The `Authorization` value is the client's copy of the token: the same string that is
in the server's `.env`, sent on every request.

```json
{
  "mcp": {
    "servers": {
      "magic-stock-picker": {
        "url": "http://<pipeline-host>:8765/mcp",
        "transport": "streamable-http",
        "headers": {
          "Authorization": "Bearer PASTE_THE_SAME_TOKEN_AS_IN_SERVER_DOTENV"
        },
        "requestTimeoutMs": 30000,
        "connectionTimeoutMs": 5000,
        "toolFilter": {
          "include": [],
          "exclude": ["start_pipeline", "start_analysis_from_csv"]
        }
      }
    }
  }
}
```

Or from the CLI:

```bash
openclaw mcp set magic-stock-picker '{"url":"http://<pipeline-host>:8765/mcp","transport":"streamable-http","headers":{"Authorization":"Bearer YOUR_TOKEN"},"toolFilter":{"include":[],"exclude":["start_pipeline","start_analysis_from_csv"]}}'
openclaw mcp probe magic-stock-picker --json
```

The `toolFilter` is doing real work — see [below](#why-that-toolfilter).

**A 30-second `requestTimeoutMs` is plenty**, which is the whole payoff of the job
model: no tool here blocks. `start_pipeline` returns a job id in milliseconds even
though the run it started takes an hour. Had these been blocking calls, no timeout
value would have worked.

**Same machine?** Use stdio instead and skip the service entirely — OpenClaw will
start and stop it for you:

```json
{
  "mcp": {
    "servers": {
      "magic-stock-picker": {
        "command": "python",
        "args": ["-m", "agent_mcp.server"],
        "cwd": "/path/to/magic-stock-picker"
      }
    }
  }
}
```

### Why that `toolFilter`

`confirm=True` protects against an accidental call. It does not protect against a
*confident* one — an agent that has read the preview, decided the spend is justified,
and confirmed. For the two operations that cost $11–13 that is a decision worth
keeping with a person, so the config above hides them from the model entirely. An
agent cannot confirm a tool it cannot see.

What stays available after that exclusion:

| Still there | Gone |
| :--- | :--- |
| `describe_capabilities` | `start_pipeline` |
| `start_screen` (free) | `start_analysis_from_csv` |
| `start_ticker_analysis` (~$0.37–0.55) | |
| `start_critic_review`, `start_buy_case`, `start_sale_advisory` | |
| `start_sell_check`, `start_buy_check`, `start_buy_check_sweep` | |
| `start_ledger_snapshot`, `start_growth_persistence_study` (free) | |
| all four `job_*` and all four read-only tools | |

So the agent can still screen for free, analyse a single name for cents, run the
checks and sweeps, and read everything — it just cannot commit the two double-digit
batch runs, which stay on the command line where you type them.

Tighten or loosen to taste. A **read-only** deployment, useful for an agent that
should be able to discuss the research but never start anything:

```json
"toolFilter": {
  "include": ["describe_capabilities", "list_reports", "read_report",
              "get_rankings", "get_screen_config",
              "job_status", "job_log", "job_list"],
  "exclude": []
}
```

Note this is a *client-side* filter: it governs what that client's model is offered,
not what the server will do. Another client with a different filter, or the same token
and a raw HTTP call, still reaches everything. It is a usability guardrail on top of
the server's own gates, not a replacement for them — the server-side protections
(`confirm=True`, the billed-job lock, the budget ceilings, `--no-budget` being
unexposed) are what actually hold.

### If you run more than one client

Point them all at **one** server rather than giving each its own. The billed-job lock
is enforced per job store, so a single shared server means "one billed run at a time"
actually holds across all of them. Set `requested_by` on `start_*` calls so `job_list`
can answer who started what.

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
