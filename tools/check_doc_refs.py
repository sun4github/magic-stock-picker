"""Check the `file.py:line` references in docs/ still point at what they claim.

These docs are a guided read: they say "the reuse check is at `pipeline/analysis.py:96`"
and expect you to open that line. That only works if the number is true, and line
numbers rot on every edit — silently, because nothing executes a document.

So this executes them. For each reference it checks the file exists and the line is
in range; and where the reference sits beside the name of a symbol — which is the
house style, `` `_check_budget` (`usage.py:60`) `` — it checks the line actually
falls inside that symbol. That last check is the one with teeth: it catches a
reference that still points at a real line of a real file, but no longer at the
thing the sentence is about.

    python tools/check_doc_refs.py

Exits non-zero and lists what drifted. Regenerating the numbers is a separate
concern; this only tells you which ones are wrong, which is what stops a doc from
quietly decaying between reads.

Not checked: references to a REGION rather than a symbol ("the startup block,
`main.py:1-120`"). No symbol addresses those, so nothing here can verify them —
they are reported as unanchored so you at least know how many are taken on trust.
"""
import ast
import io
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

REF_RE = re.compile(
    r"(?:\\n|<br/>)?(?P<file>[A-Za-z_0-9/]+\.py):(?P<start>\d+)(?:-(?P<end>\d+))?")
NAME_RE = re.compile(r"`([A-Za-z_][A-Za-z_0-9]*)[`(]|\b([a-z_][a-z_0-9]{4,})\b")

# Must match the window the references were generated against, or a reference that
# is fine will read as unanchored and vice versa.
LOOK_BEHIND = 48
LOOK_AHEAD = 26


def symbols():
    """{name: [(relative_path, first_line, last_line)]}"""
    index = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", "reports", "backfills", "specs", "prompts")]
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn.startswith("test_"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, SRC).replace("\\", "/")
            try:
                tree = ast.parse(io.open(full, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = node.lineno
                    for dec in getattr(node, "decorator_list", []):
                        start = min(start, dec.lineno)
                    index[node.name].append((rel, start, node.end_lineno))
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            index[t.id].append((rel, node.lineno, node.end_lineno))
    return index


def line_counts():
    counts = {}
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, SRC).replace("\\", "/")
                try:
                    counts[rel] = len(io.open(full, encoding="utf-8").read().splitlines())
                except UnicodeDecodeError:
                    pass
    return counts


def main():
    index = symbols()
    unique = {k: v[0] for k, v in index.items() if len(v) == 1}
    counts = line_counts()

    docs = [os.path.join(ROOT, "docs", f)
            for f in sorted(os.listdir(os.path.join(ROOT, "docs"))) if f.endswith(".md")]
    docs.append(os.path.join(ROOT, "README.md"))

    problems, unanchored, ok = [], 0, 0

    for doc in docs:
        name = os.path.relpath(doc, ROOT).replace("\\", "/")
        text = io.open(doc, encoding="utf-8", newline="").read()
        for m in REF_RE.finditer(text):
            ref = m.group("file")
            start = int(m.group("start"))
            lineno = text[:m.start()].count("\n") + 1

            # A path relative to src/, which is how these are written.
            key = ref[4:] if ref.startswith("src/") else ref
            if key not in counts:
                if key in ("path/to/file.py",):        # the convention example
                    continue
                problems.append((name, lineno, m.group(0), "no such file under src/"))
                continue
            if start > counts[key]:
                problems.append((name, lineno, m.group(0),
                                 f"{key} has only {counts[key]} lines"))
                continue

            window = text[max(0, m.start() - LOOK_BEHIND): m.end() + LOOK_AHEAD]
            names = {a or b for a, b in NAME_RE.findall(window)}
            hits = {n: unique[n] for n in names if n in unique}
            if len(hits) != 1:
                unanchored += 1
                continue
            sym, (path, first, last) = next(iter(hits.items()))
            if path != key:
                problems.append((name, lineno, m.group(0),
                                 f"`{sym}` is in {path}, not {key}"))
            elif not (first <= start <= last):
                problems.append((name, lineno, m.group(0),
                                 f"`{sym}` spans {path}:{first}-{last}"))
            else:
                ok += 1

    total = ok + unanchored + len(problems)
    print(f"{total} references: {ok} verified against a symbol, "
          f"{unanchored} unanchored (region references, taken on trust), "
          f"{len(problems)} wrong")
    if problems:
        print()
        for doc, lineno, ref, why in problems:
            print(f"  {doc}:{lineno}  {ref}  — {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
