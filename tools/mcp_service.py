"""Manage the MCP server as a per-user background service on Windows.

    python tools/mcp_service.py status
    python tools/mcp_service.py start
    python tools/mcp_service.py stop
    python tools/mcp_service.py install     # run it at every logon
    python tools/mcp_service.py remove

Why this exists rather than a shell one-liner: the obvious way to stop the server
is to find whatever is listening on the port and kill it, and the obvious one-liner
for that (`Get-NetTCPConnection ... | ForEach-Object { Stop-Process -Id
$_.OwningProcess }`) only survives being typed at a cmd.exe prompt. Wrapped as
`powershell -Command "..."` and pasted into a PowerShell prompt, the OUTER shell
expands `$_` before the inner one ever sees it and the inner shell receives
`Stop-Process -Id .OwningProcess`. A command that depends on which shell it is
pasted into is a bad command to put in a README, so the shell is taken out of it.

`install` writes a launcher into the per-user Startup folder. That is the
unelevated route: Windows lets an ordinary user create a WEEKLY scheduled task -
which is why tools\\setup_weekly_jobs.cmd needs no elevation - but refuses ONLOGON
and ONSTART, the two triggers a long-running server actually wants, with a bare
"Access is denied". The Startup folder has no such restriction and gives the same
practical result on a workstation you log into. For a true boot-start task, run
tools\\setup_mcp_server.cmd from an Administrator prompt instead.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_NAME = "MagicStockPicker-MCP.vbs"
DEFAULT_PORT = 8765


# ---------------------------------------------------------------- discovery

def listening_pids(port: int) -> list:
    """PIDs listening on `port`, read out of `netstat -ano`.

    netstat rather than psutil so this needs no dependency the MCP layer does not
    already have, and rather than `Get-NetTCPConnection` so it does not care which
    shell invoked it.
    """
    if os.name != "nt":
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
        return sorted({int(m) for m in re.findall(rf":{port}\b.*?pid=(\d+)", out)})

    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        # proto  local            foreign  state       pid
        if len(parts) >= 5 and parts[0].upper().startswith("TCP") and parts[3].upper() == "LISTENING":
            local = parts[1]
            if local.rsplit(":", 1)[-1] == str(port):
                try:
                    pids.add(int(parts[4]))
                except ValueError:
                    continue
    return sorted(pids)


def health(port: int, timeout: float = 3.0):
    """Ask /health. Returns the parsed body, or None if nothing answered."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


# ------------------------------------------------------------------ startup

def startup_dir() -> Path:
    """The current user's Startup folder, asked of Windows rather than assumed.

    The path is localised on non-English installs and relocatable by policy, so
    hard-coding the English one is wrong often enough to matter.
    """
    if os.name != "nt":
        raise SystemExit("Windows-only. On Linux use the systemd unit in agent_mcp/README.md.")
    try:
        import ctypes.wintypes

        CSIDL_STARTUP = 7
        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_STARTUP, None, 0, buf)
        if buf.value:
            return Path(buf.value)
    except Exception:  # pragma: no cover - falls through to the env-var guess
        pass
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"


def launcher_path() -> Path:
    return startup_dir() / LAUNCHER_NAME


def build_launcher(python_exe: str, port: int, host: str) -> str:
    """A VBScript that starts the server with no console window.

    WScript.Shell.Run's second argument is the window style: 0 hides it. The third
    is bWaitOnReturn=False, so logon is not held up by a process designed never to
    exit. Output is appended to logs/mcp_server.log - a hidden process with nowhere
    to write its diagnostics is a process you cannot debug.
    """
    log = PROJECT_ROOT / "logs" / "mcp_server.log"
    inner = (
        f'cd /d ""{PROJECT_ROOT}"" && '
        f'""{python_exe}"" -m agent_mcp.server '
        f'--transport http --host {host} --port {port} --stateless '
        f'>> ""{log}"" 2>&1'
    )
    # ASCII only. The Windows Script Host reads a .vbs as ANSI unless it carries a
    # UTF-16 BOM, so a UTF-8 em dash arrives as mojibake - harmless in a comment,
    # and not harmless the day someone puts a non-ASCII path in the command.
    return (
        "' Starts the magic-stock-picker MCP server at logon, hidden.\n"
        "' Installed by tools/mcp_service.py - delete this file to disable.\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.Run "cmd /c {inner}", 0, False\n'
    )


def write_launcher(python_exe: str, port: int, host: str) -> Path:
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    target = launcher_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = build_launcher(python_exe, port, host)
    try:
        target.write_text(text, encoding="ascii")
    except UnicodeEncodeError:
        # A non-ASCII repo or interpreter path. UTF-16LE with a BOM is the encoding
        # the Script Host detects reliably; plain UTF-8 is the one it does not.
        target.write_text(text, encoding="utf-16")
    return target


# ------------------------------------------------------------------ actions

def do_status(port: int) -> int:
    pids = listening_pids(port)
    body = health(port)
    installed = launcher_path().exists() if os.name == "nt" else False

    print(f"Port {port}:      " + (f"listening (PID {', '.join(map(str, pids))})" if pids else "nothing listening"))
    print(f"/health:        " + (json.dumps(body) if body else "no answer"))
    print(f"Runs at logon:  " + (f"yes - {launcher_path()}" if installed else "no"))
    print(f"Server log:     {PROJECT_ROOT / 'logs' / 'mcp_server.log'}")
    if pids and not body:
        print("\nSomething holds the port but /health does not answer. It may still be "
              "starting, or the port belongs to another program.")
    return 0 if body else 1


def do_start(port: int, host: str, python_exe: str) -> int:
    if health(port):
        print(f"Already running on port {port}.")
        return 0
    if listening_pids(port):
        print(f"Port {port} is held by PID {listening_pids(port)[0]}, but /health does not "
              f"answer. Stop that first, or choose another port.")
        return 1

    # Started through the same launcher logon uses, so "start it now" and "start it
    # at logon" cannot drift into behaving differently.
    target = launcher_path() if launcher_path().exists() else write_launcher(python_exe, port, host)
    if os.name == "nt":
        subprocess.Popen(["wscript.exe", str(target)])
    else:  # pragma: no cover
        raise SystemExit("Windows-only. Use the systemd unit on Linux.")

    import time

    for _ in range(30):
        time.sleep(1)
        if health(port):
            print(f"Started. Listening on port {port}.")
            return 0
    print(f"Started the launcher, but nothing answered /health within 30s.\n"
          f"Check {PROJECT_ROOT / 'logs' / 'mcp_server.log'}.")
    return 1


def do_stop(port: int) -> int:
    pids = listening_pids(port)
    if not pids:
        print(f"Nothing listening on port {port}.")
        return 0
    for pid in pids:
        if os.name == "nt":
            # /T for the tree: the server is launched under a cmd wrapper, so
            # killing only the python process can leave the wrapper behind.
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                    capture_output=True, text=True)
            ok = result.returncode == 0
        else:  # pragma: no cover
            import signal

            try:
                os.kill(pid, signal.SIGTERM)
                ok = True
            except OSError:
                ok = False
        print(f"  PID {pid}: {'stopped' if ok else 'could not stop'}")
    return 0


def do_install(port: int, host: str, python_exe: str) -> int:
    target = write_launcher(python_exe, port, host)
    print(f"Installed {target}")
    print(f"  runs at logon: python -m agent_mcp.server --host {host} --port {port}")
    print(f"  log:           {PROJECT_ROOT / 'logs' / 'mcp_server.log'}")
    print(f"  start now:     python tools/mcp_service.py start")
    print(f"  remove:        python tools/mcp_service.py remove")
    return 0


def do_remove(port: int) -> int:
    target = launcher_path()
    if target.exists():
        target.unlink()
        print(f"Removed {target}")
    else:
        print(f"Nothing installed at {target}")
    if listening_pids(port):
        print("Note: a server is still running. Stop it with: "
              "python tools/mcp_service.py stop")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage the MCP server as a per-user background service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("action", choices=("status", "start", "stop", "restart",
                                           "install", "remove"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--python", default=sys.executable,
                        help="Interpreter the launcher should use (default: this one)")
    args = parser.parse_args()

    if args.action == "status":
        return do_status(args.port)
    if args.action == "start":
        return do_start(args.port, args.host, args.python)
    if args.action == "stop":
        return do_stop(args.port)
    if args.action == "restart":
        do_stop(args.port)
        return do_start(args.port, args.host, args.python)
    if args.action == "install":
        return do_install(args.port, args.host, args.python)
    return do_remove(args.port)


if __name__ == "__main__":
    sys.exit(main())
