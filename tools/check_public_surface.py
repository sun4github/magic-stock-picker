"""Assert that every name one module reaches for on another still resolves.

The refactor's method is to move code and re-export it under its old name, because
`refine`, `buy_case`, `buy_case_agent`, `sale_advisory`, `critic_agent`, the cron
scripts and the tests all reach into `main` and `mcp_server` by attribute —
`main._price_snapshot`, `mcp_server.db_log_verdict`. Python resolves those at CALL
time, so a name that stops being re-exported does not fail at import. It fails on
the one line of the one code path that uses it, possibly mid-run, possibly on the
Raspberry Pi at 3am, and possibly after money has been spent.

This finds every such reference statically — `import X; X.name` and
`from X import name` — resolves each against the imported module, and exits
non-zero listing whatever is missing. Run it after every move.

    python tools/check_public_surface.py

It cannot see a name assembled at runtime (`getattr(main, "_format_" + kind)`).
Nothing in this codebase does that today; if something starts, this stops covering
it.
"""
import ast
import os
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(os.path.dirname(os.path.abspath(__file__))).parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT / "src")

# The facade modules: the ones other modules import wholesale and reach into.
TARGETS = ["main", "mcp_server", "report", "settings", "data", "persistence",
           "mcp_app", "prompt_loader"]


def references():
    """{target_module: {name: [where it is referenced]}}"""
    found = defaultdict(lambda: defaultdict(list))
    for path in sorted(list((ROOT / "src").rglob("*.py"))
                       + list((ROOT / "tools").rglob("*.py"))):
        rel = path.relative_to(ROOT).as_posix()
        if path.stem in TARGETS and path.parent.name in ("src", ""):
            continue  # a module is allowed to define its own names
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            print(f"  ! could not parse {rel}: {e}")
            continue
        # Which target modules this file actually imports, and under what local
        # name. Without this, a local variable called `report` or `data` — both
        # common here — reads as a module reference and every `.get()` on it looks
        # like a missing export.
        bound = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in TARGETS:
                        bound[alias.asname or alias.name] = alias.name

        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in bound):
                found[bound[node.value.id]][node.attr].append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and node.module in TARGETS:
                for alias in node.names:
                    found[node.module][alias.name].append(f"{rel}:{node.lineno}")
    return found


def main():
    found = references()
    missing = []
    total = 0
    for target in sorted(found):
        module = __import__(target)
        names = found[target]
        total += len(names)
        gone = sorted(n for n in names if not hasattr(module, n))
        print(f"{target:<14} {len(names):>3} names referenced, {len(gone)} missing")
        for name in gone:
            missing.append((target, name, names[name]))

    print(f"\n{total} references checked across {len(found)} modules.")
    if missing:
        print(f"\n{len(missing)} MISSING:")
        for target, name, where in missing:
            print(f"  {target}.{name}")
            for w in where:
                print(f"      {w}")
        return 1
    print("All resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
