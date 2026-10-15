"""Run every test file in the repository and report exit codes.

Runs every test file in the suite, records exit code, elapsed time and the tail of
the output. Originally written for the src/ restructure — run once before and once
after, and the two reports must agree on every exit code — and kept since as the one
command that runs everything. Usage:

    python run_tests.py <output-file> [src-dir]

Most suites live in src/ and run from there; see EXTERNAL_TESTS for the ones that
do not.
"""
import os
import subprocess
import sys
import time

TESTS = [
    "test_guards",
    "test_reconciliation",
    "test_verdict_ledger",
    "test_critic",
    "test_buy_case",
    "test_screen_gates",
    "test_buy_check_sweep",
    "test_external_sync",
    "test_momentum_gate",
]

# Suites that live outside src/ and run from somewhere else. Held separately because
# every entry above resolves against src_dir and runs with src/ as its working
# directory; these each carry their own. Without this the MCP layer's suite is
# invisible to the one command that claims to run every test.
#   (label, path relative to the repo root, working directory relative to the root)
EXTERNAL_TESTS = [
    ("agent_mcp/test_agent_mcp", "agent_mcp/test_agent_mcp.py", "."),
]

out_path = sys.argv[1]
src_dir = sys.argv[2] if len(sys.argv) > 2 else r"E:\repos\magic-stock-picker\src"
repo_root = os.path.dirname(os.path.abspath(src_dir.rstrip("\\/")))

CASES = [(name, os.path.join(src_dir, name + ".py"), src_dir) for name in TESTS]
CASES += [
    (label, os.path.join(repo_root, rel), os.path.join(repo_root, cwd))
    for label, rel, cwd in EXTERNAL_TESTS
]

with open(out_path, "w", encoding="utf-8") as out:
    for name, path, workdir in CASES:
        if not os.path.exists(path):
            out.write(f"===== {name} =====\nMISSING {path}\n\n")
            out.flush()
            continue
        start = time.time()
        proc = subprocess.run(
            [sys.executable, "-u", path],
            cwd=workdir, capture_output=True, text=True, timeout=900,
        )
        elapsed = time.time() - start
        combined = (proc.stdout or "") + (proc.stderr or "")
        tail = "\n".join(combined.strip().splitlines()[-8:])
        out.write(f"===== {name} =====\n")
        out.write(f"exit={proc.returncode} elapsed={elapsed:.0f}s\n")
        out.write(tail + "\n\n")
        out.flush()
        print(f"{name}: exit={proc.returncode} {elapsed:.0f}s", flush=True)
    out.write("ALL DONE\n")
print("ALL DONE", flush=True)
