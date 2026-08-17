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
```

**For a client on another machine** (OpenClaw, or any remote agent) — run this in a
terminal and leave it open. Ctrl+C stops it:

```bash
python -m agent_mcp.server --transport http --host 0.0.0.0 --port 8765
```

That is all the setup there is. The bearer token is read from `.env`; see
[the token section](#msp_mcp_token-one-secret-two-copies) to create one, and
[Configuring OpenClaw](#configuring-openclaw) for the client side. Starting it
automatically is [possible but fiddly on Windows](#starting-it-automatically-optional-and-the-fiddly-part)
— running it by hand is a perfectly good answer.

**For a client on this machine** (Claude Desktop, Claude Code), you do not run
anything: stdio means the client starts and stops the server itself. Just register
it:

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

## Things to say to it

Written as you would actually type them to an agent connected to this server. The
agent picks the tool; these are here to show what the surface is good for.

| What you say | What it does |
| :--- | :--- |
| *"What can the stock picker do?"* | `describe_capabilities` — the map, including what each operation costs |
| *"Analyse NVDA and email me the reports"* | `start_ticker_analysis(ticker="NVDA", email_report=True, confirm=True)` — ~$0.40, 2–5 min, reports arrive as attachments |
| *"What did the last screen turn up?"* | `get_rankings` — free, instant, no run needed |
| *"Read me the verdict on CROX"* | `read_report(ticker="CROX", kind="final")` |
| *"Is there a buy case for MRVL yet?"* | `read_report(ticker="MRVL", kind="buy_case")` |
| *"Has anything on my watchlist triggered?"* | `start_buy_check_sweep(dry_run=True)` first — free — then `confirm=True` |
| *"Should I still hold CROX?"* | `start_sell_check(ticker="CROX", run_id=…, confirm=True)` |
| *"How's that NVDA analysis going?"* | `job_status` / `job_log` |
| *"Stop that run"* | `job_cancel` — work already finished is kept |
| *"Refresh the screen"* | `start_screen` — free, 20–30 min |

Two phrasings worth having in muscle memory:

- **"…and email me the reports"** is the one that matters when you are away from the
  machine. Without it you get a job id, which is not an answer you can read on a phone.
- **"What would that cost?"** — every billed tool called without `confirm=True`
  returns a costed preview and spends nothing, so an agent can always check first.

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

### Getting the answer by email

`start_ticker_analysis(ticker, email_report=True, confirm=True)` mails everything the
run produced, to the address configured on the host:

| Attachment | Comes from |
| :--- | :--- |
| `<T>_Final_Report_<Verdict>.md` | `reports/` |
| `<T>_Bear_Case.md` | **the database** — never written to disk |
| `<T>_Bull_Case.md` | **the database** — never written to disk |
| `<T>_Sale_Advisory.md` | `reports/` |
| `<T>_Buy_Case.md` | `reports/`, `Watch` verdicts only |

The bear and bull cases are the only run artefacts the pipeline does not write to
`reports/` — it stores them in `agent_outputs`, which is where the web viewer reads
them from. An email assembled from the filesystem alone therefore omits the two
documents containing the actual *argument*: the verdict is the conclusion, these are
the reasoning. They are fetched by the run id printed in the report's own header, so
they are the pair the analyst was weighing rather than whatever is newest for that
ticker. If the database is unreachable the rest of the email still goes.

This is the mode for the case the whole HTTP transport exists to serve: someone asks
you about a ticker while you are nowhere near the machine, an agent runs the analysis
over Telegram or similar, and a job id is not a useful answer.

**The reports arrive as `.md` attachments.** The body is five lines — the verdict, the
price the report was written at, the analyst's opening sentence, what is attached, and
the reminder that a stored report is point-in-time:

```
CROX — Watch
Priced at $63.26 as of 2026-08-03 12:34, when the report was written.
Centene's business model makes its financial survival almost entirely dependent
on state and federal healthcare policies that are currently shifting toward…
Attached: CROX Final Report Watch, CROX Sale Advisory, CROX Buy Case.
A stored report is point-in-time — check the current price before acting on it.
```

The price and the opening sentence are parsed out of the attached report rather than
recomputed, so the summary cannot disagree with the document beside it.

To check delivery and formatting without paying for a run — and to re-send a report
you already have:

```bash
python tools/analyze_and_email.py CROX --email-existing
```

Add `--dry-run` to build the message and print what would be sent without sending it.

Two design points worth knowing:

- **The recipient is fixed in the host's `.env`** (`ALERT_EMAIL_TO`, or
  `REPORT_EMAIL_TO` to override) and there is no `to` parameter. A free-text
  destination on a tool reachable over the network would make anyone holding the
  bearer token able to send mail from your domain to anywhere.
- **The email is the last step of the job, not of the server.** Runs are detached and
  take minutes; if the server sent the mail, restarting it mid-run would silently
  lose the email. `tools/analyze_and_email.py` runs the same CLI command and then
  sends, so the run either completes and mails or does neither.

One wrinkle: a report reused from inside the 24-hour reuse window is not rewritten,
so there is nothing new to mail and the job says so. Pass `force=True` to regenerate
and send it.

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

## One billed job at a time — and a queue behind it

Billed operations are serialised server-wide: exactly one runs at a time. They share
one rolling daily spend ceiling and one run-log directory, and once several agents can
call this server, two overlapping pipeline runs stops being a theoretical problem — an
expensive one, whose logs interleave.

**A second request is queued, not refused.** Ask about two tickers a few seconds apart
over Telegram and both are accepted; the second returns immediately with a job id and
its position, and starts by itself when the first finishes. Nothing needs
re-submitting, and the caller does not have to invent a retry loop.

```
start_ticker_analysis("AAPL", confirm=True)  ->  status: started,  job_id: ticker-…-a1b2c3
start_ticker_analysis("MSFT", confirm=True)  ->  status: queued,   queue_position: 1
start_ticker_analysis("NVDA", confirm=True)  ->  status: queued,   queue_position: 2
```

`job_status` reports `QUEUED` with the position until it starts, then `RUNNING`.
`job_list` shows the whole queue in order, with who asked for each. `job_cancel` on a
queued job drops it before it spends anything — there is no process to kill.

Free operations are never blocked by any of this; waiting on an hour-long pipeline to
read a price snapshot would be absurd.

Two limits worth knowing. The queue holds **8** jobs; beyond that a submission is
refused rather than accepted, because every entry is real money waiting to be spent.
And the queue only advances while the server is running — see
[what closing the window does](#the-three-ways-to-run-it-and-what-closing-the-window-does).

### Checking before you run something by hand

The serialisation above governs work **this server launched**. A `python main.py …`
you type in another terminal is a different process that never touches the job store,
so the queue cannot see it and will happily start a job alongside it. They would then
share the database, the reports directory and the run logs.

`job_list` is the check. Free, instant, and safe to call at any time:

```
billed_slot : "free - a billed operation can start now"     -> nothing running
queue       : []                                             -> nothing pending
```

When something is in flight it reads:

```json
"billed_slot": { "in_use_by": "ticker-20260817-003149-921d0d", "operation": "ticker" },
"queue": [ { "position": 1, "label": "Analyse AMD", "requested_by": "openclaw" } ]
```

**Both empty means it is safe to start a manual run.** Ask an agent *"is the stock
picker busy?"* or *"anything running or queued?"*, or check straight from a terminal
without involving the server at all:

```bash
python -c "from agent_mcp import server; print(server.job_list()['billed_slot'])"
```

Note the check only works in one direction, and the asymmetry is worth holding in
mind:

| | Sees the other side? |
| :--- | :--- |
| You, about to start a manual run, checking `job_list` | **yes** — this is the case that works |
| The server, about to start a job while your manual run is going | **no** — it will start anyway |

Closing that second gap needs a lock file both entry points acquire, which means
adding the acquire/release to `src/main.py` — and would end the property that this
layer changes nothing in the pipeline package. Checking before you type costs nothing
and is the working habit until that trade is worth making.

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

### Why Host-header checking is off on a network bind

MCP ships DNS-rebinding protection, which validates the `Host` header against an
allowlist. **When a token is set and the bind is not loopback, this server turns it
off** — a deliberate default, and worth explaining because "Invalid Host header" is
otherwise a baffling failure.

That protection exists to stop a malicious web page driving a victim's browser at an
**unauthenticated** server on their own machine. A browser will attach cookies but
cannot invent a bearer token, so once a token is required the attack is already dead.

What it does cost is real, and it bit here. The allowlist can only be a **snapshot of
this machine's addresses taken at startup**, and those change:

```
server starts 00:41:45  ->  allowlist: 127.0.0.1, 192.168.1.100, Suneel-PC, …
Wi-Fi gets its DHCP lease at ~00:44  ->  192.168.4.115 now exists
client dials 192.168.4.115           ->  "Invalid Host header", forever
```

A laptop joining Wi-Fi after boot, a VPN coming up, Hyper-V renumbering — any of them
strands an interface the server will reject for the rest of its life, with no hint
that the cure is a restart. That is a confusing failure traded for no security.

Loopback binds and token-less binds keep the protection: that is the case it was
designed for, and `127.0.0.1` cannot go stale. To force an explicit allowlist back on
for a network bind, set `MSP_MCP_ALLOWED_HOSTS=host1,host2`; `*` disables it anywhere.

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
| `MSP_MCP_ALLOWED_HOSTS` | Comma-separated `Host` allowlist, or `*`. Only needed to turn Host checking back **on** for an authenticated network bind — see below |
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

### Running it manually — start here

**The simplest thing that works, and the one to reach for first.** Open a terminal,
run one command, leave it open. No scheduled task, no Startup folder, no elevation —
none of which is needed for a server you are happy to start yourself.

```bash
python -m agent_mcp.server --transport http --host 0.0.0.0 --port 8765
```

It runs in the foreground and logs to that window. **Ctrl+C stops it.** From the
repository root, and nothing else to configure — the token is read from `.env`.

You should see:

```
magic-stock-picker MCP server on http://0.0.0.0:8765/mcp
Authentication: bearer token required
Session mode: session-tracked
Health check: http://0.0.0.0:8765/health
```

Check it from another terminal, or a browser:

```bash
curl http://localhost:8765/health
```

```
{"status":"ok","server":"magic-stock-picker"}
```

Bind `127.0.0.1` instead of `0.0.0.0` if the client is on this same machine; the
server then refuses connections from the network entirely and needs no firewall rule.

### The three ways to run it, and what closing the window does

| | Command | Logs | Survives closing the window? |
| :--- | :--- | :--- | :--- |
| **Foreground** | `python -m agent_mcp.server --transport http --host 0.0.0.0 --port 8765` | in the window, live | **No** |
| **Minimized** | `start "" /min python -m agent_mcp.server --transport http --host 0.0.0.0 --port 8765 --stateless` | in its own minimized window | No — closing *that* window stops it |
| **Background (hidden)** | `python tools/mcp_service.py start` | `logs\mcp_server.log` | **Yes** |

On `start /min`, the empty `""` is the window **title**. `start` treats a lone quoted
argument as a title, so leaving it out makes it swallow the first part of your
command — the classic footgun with this builtin.

On Linux the equivalent of the third row is `nohup python -m agent_mcp.server
--transport http --host 0.0.0.0 --stateless >> logs/mcp_server.log 2>&1 &`.

**What closing the window actually does.** Windows sends `CTRL_CLOSE_EVENT` to every
process attached to that console; they get roughly five seconds, then they are killed.
The server is attached, so it dies. Two consequences that are easy to get backwards:

- **The server dying does not stop a run in flight.** Analyses are launched
  *detached* — no console of their own to lose — so a pipeline started through a tool
  keeps going, finishes, writes its reports, and sends its email if one was asked for.
- **What you lose is the watcher, not the work.** The thread that would have recorded
  the exit code died with the server, so that job reads `ORPHANED` afterwards rather
  than `SUCCEEDED`. The report is on disk either way; `job_log` shows how it ended.

**A queued job is the exception, and it is worth knowing.** A job still waiting for
its turn has no process yet, so there is nothing detached to survive. It stays
`QUEUED` on disk and is picked up the next time the server starts — which the server
does automatically at startup, logging `Resumed queued job …`. So nothing is lost, but
nothing progresses either while the server is down.

### Starting it automatically (optional, and the fiddly part)

Everything below is convenience. If manual start suits you, skip it — nothing here
changes what the server does.

Be warned that this is where the friction is on Windows: **an ordinary user may
create a `WEEKLY` scheduled task but not an `ONLOGON` or `ONSTART` one.** Both are
refused with a bare `ERROR: Access is denied` through `schtasks` and PowerShell's
`Register-ScheduledTask` alike. That is precisely why
`tools\setup_weekly_jobs.cmd` installs the buy-check sweep without elevation and a
long-running server cannot be installed the same way.

So there are two routes, and `tools\setup_mcp_server.cmd` picks whichever it can:

| | Elevated prompt | Ordinary prompt |
| :--- | :--- | :--- |
| Installs | a boot-start scheduled task | a Startup-folder launcher |
| Starts | before anyone logs in | when you log in |
| Manage with | `schtasks` | `python tools/mcp_service.py …` |

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

If the client is on another machine, open the port once, from an **Administrator**
prompt (`netsh advfirewall` needs elevation):

```bat
netsh advfirewall firewall add rule name="MagicStockPicker MCP" dir=in action=allow protocol=TCP localport=8765 profile=private remoteip=localsubnet
```

`profile=private remoteip=localsubnet` is doing real work and is worth keeping. Without
it the rule applies to every profile, so the port stays open on any network the machine
later joins — a hotel or coffee-shop Wi-Fi included — with the bearer token as the only
thing in front of a server that spends money. Scoped this way it is reachable from your
own LAN and nowhere else. Remove it with `netsh advfirewall firewall delete rule
name="MagicStockPicker MCP"`.

**A LAN rule only helps if the client is on the LAN.** If OpenClaw runs in the cloud
rather than on your network, it cannot reach a `192.168.x.x` address at all and no
firewall rule changes that — see below.

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

### If the client is not on your LAN

The firewall rule above assumes OpenClaw is on the same network. If it runs anywhere
else — a cloud host, a phone on mobile data — it cannot reach a private address at
all, and the rule is beside the point. Three ways to close that gap, in the order
worth trying:

| | What it is | Trade-off |
| :--- | :--- | :--- |
| **Tailscale / WireGuard** | A private network both machines join | Best fit. No port exposed to the internet, no certificate to manage, and the server keeps binding a private address |
| **Cloudflare Tunnel / ngrok** | An outbound tunnel that publishes an HTTPS URL | Easy, gives you TLS, but the endpoint is genuinely on the internet with only the bearer token in front of it |
| **Port forwarding on the router** | Expose 8765 directly | Not recommended. Plain HTTP, so the token crosses the internet in clear text, and the port is scanned within hours |

Note the second and third put a money-spending server on the public internet. If you
go that way, use a long token, keep the `toolFilter` exclusions, and prefer the tunnel
over the forward — a tunnel at least gives you TLS, so the token is not readable in
transit.

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
