"""Run every test file in src/ and report exit codes.

Runs every test file in the suite, records exit code, elapsed time and the tail of
the output. Run once before the restructure and once after; the two reports must
agree on every exit code. Usage:

    python run_tests.py <output-file> [src-dir]
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
]

out_path = sys.argv[1]
src_dir = sys.argv[2] if len(sys.argv) > 2 else r"E:\repos\magic-stock-picker\src"

with open(out_path, "w", encoding="utf-8") as out:
    for name in TESTS:
        path = os.path.join(src_dir, name + ".py")
        if not os.path.exists(path):
            out.write(f"===== {name} =====\nMISSING {path}\n\n")
            out.flush()
            continue
        start = time.time()
        proc = subprocess.run(
            [sys.executable, "-u", path],
            cwd=src_dir, capture_output=True, text=True, timeout=900,
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
