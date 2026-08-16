"""Install (or remove) a Startup-folder launcher for the MCP server.

The unelevated fallback for `tools\\setup_mcp_server.cmd`. Windows lets an ordinary
user create a WEEKLY scheduled task but not an ONLOGON or ONSTART one — both are
refused with a bare "Access is denied" that reads like a broken script rather than a
missing privilege. That is why the weekly buy-check sweep installs without elevation
and a long-running server does not.

The per-user Startup folder has no such restriction: anything in it runs when you log
in, with your profile, your environment and your `.env`. That is the same practical
result as an ONLOGON task on a workstation you log into.

Written in Python rather than emitted from the .cmd because the launcher contains
quotes, `&&` and `>>`, every one of which needs escaping in batch `echo`, and getting
one wrong produces a launcher that fails silently at next logon.

    python tools/install_mcp_startup.py --port 8765
    python tools/install_mcp_startup.py --remove
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_NAME = "MagicStockPicker-MCP.vbs"


def startup_dir() -> Path:
    """The current user's Startup folder, asked of Windows rather than assumed.

    The path is localised on non-English installs and relocatable by policy, so
    hard-coding "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup" is
    wrong often enough to matter.
    """
    if os.name != "nt":
        raise SystemExit("This installer is Windows-only. On Linux use the systemd unit "
                         "in agent_mcp/README.md.")
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


def build_launcher(python_exe: str, port: int, host: str) -> str:
    """A VBScript that starts the server with no console window.

    WScript.Shell.Run's second argument is the window style: 0 hides it. The third
    is bWaitOnReturn=False, so logon is not held up by a process designed never to
    exit. Output is appended to logs/mcp_server.log — a hidden process with nowhere
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
    # UTF-16 BOM, so a UTF-8 em dash here arrives as mojibake - harmless in a comment,
    # and not harmless the day someone puts a non-ASCII path in the command.
    return (
        "' Starts the magic-stock-picker MCP server at logon, hidden.\n"
        "' Installed by tools/install_mcp_startup.py - delete this file to disable.\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.Run "cmd /c {inner}", 0, False\n'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--python", default=sys.executable,
                        help="Interpreter the launcher should use (default: this one)")
    parser.add_argument("--remove", action="store_true", help="Delete the launcher")
    parser.add_argument("--start-now", action="store_true",
                        help="Also start the server immediately, the same way logon will")
    args = parser.parse_args()

    target = startup_dir() / LAUNCHER_NAME

    if args.remove:
        if target.exists():
            target.unlink()
            print(f"Removed {target}")
        else:
            print(f"Nothing to remove at {target}")
        return 0

    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    launcher = build_launcher(args.python, args.port, args.host)
    try:
        target.write_text(launcher, encoding="ascii")
    except UnicodeEncodeError:
        # A non-ASCII repo or interpreter path. UTF-16LE with a BOM is the encoding
        # the Windows Script Host detects reliably; plain UTF-8 is the one it does not.
        target.write_text(launcher, encoding="utf-16")

    print(f"Installed {target}")
    print(f"  runs at logon: python -m agent_mcp.server --host {args.host} --port {args.port}")
    print(f"  log:           {PROJECT_ROOT / 'logs' / 'mcp_server.log'}")
    print(f"  to disable:    python tools/install_mcp_startup.py --remove")

    if args.start_now:
        # Detached, so it outlives this process exactly as the logon launcher does.
        subprocess.Popen(
            ["wscript.exe", str(target)],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        print("\nStarted it now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
