"""Dump mcp_server's two public contracts to a text file, for diffing.

`mcp_server` is answerable to two audiences that a refactor can break independently:

  1. **MCP clients**, which see the 33 `@mcp.tool()` registrations — name,
     description, and JSON input schema. A tool that silently stops registering
     (a module never imported, a decorator left behind) does not raise anything;
     it just disappears from the server's advertised list.
  2. **The rest of this codebase**, which imports the same functions directly and
     calls them as plain Python. `main`, `refine`, `buy_case`, `sale_advisory` and
     the cron scripts all do this.

So both are recorded here. Run before a move and after it, and diff:

    python tools/snapshot_tool_surface.py before.txt
    ... do the move ...
    python tools/snapshot_tool_surface.py after.txt
    diff before.txt after.txt

Defining modules are deliberately NOT recorded — that is the one thing a move is
supposed to change. Everything else must not move: names, signatures, defaults,
docstrings, and the advertised schema.

No database is touched. Importing `mcp_server` opens no connection (the schema
bootstrap is lazy), and nothing here calls a tool.
"""
import asyncio
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import mcp_server  # noqa: E402


def section(out, title):
    out.write("\n" + "=" * 78 + f"\n== {title}\n" + "=" * 78 + "\n")


def snapshot(path):
    with open(path, "w", encoding="utf-8") as out:
        section(out, "registered MCP tools")
        tools = asyncio.run(mcp_server.mcp.list_tools())
        out.write(f"count = {len(tools)}\n")
        for tool in sorted(tools, key=lambda t: t.name):
            out.write(f"\n--- {tool.name}\n")
            out.write(f"description:\n{tool.description}\n")
            out.write("inputSchema:\n")
            out.write(json.dumps(tool.inputSchema, indent=2, sort_keys=True) + "\n")

        # Two sections, because they are held to different standards.
        #
        # The functions and constants are the contract: another module imports them
        # and calls them, so a changed signature, a changed default or a lost
        # docstring is a real break. Private helpers are included — test_verdict_ledger
        # imports `_num`, and "an underscore means nobody else calls this" is exactly
        # the assumption a refactor must not be allowed to make silently.
        #
        # The modules and classes are not a contract. `os`, `json`, `numpy` and
        # `psycopg2.extras.Json` are visible on this module only because it imports
        # them at the top, and some are not even used. A split that stops re-exporting
        # them changes this section and breaks nothing, so it is recorded apart, to be
        # read rather than required to match.
        section(out, "python API: functions and constants")
        names = sorted(n for n in dir(mcp_server) if not n.startswith("__"))
        plain = [n for n in names
                 if not inspect.ismodule(getattr(mcp_server, n))
                 and not inspect.isclass(getattr(mcp_server, n))]
        out.write(f"count = {len(plain)}\n")
        for name in plain:
            obj = getattr(mcp_server, name)
            if inspect.isfunction(obj):
                try:
                    sig = str(inspect.signature(obj))
                except (TypeError, ValueError):
                    sig = "(unavailable)"
                doc = inspect.getdoc(obj) or ""
                out.write(f"\n--- def {name}{sig}\n{doc}\n")
            else:
                # Module-level constants: the field maps, embedding dimensions and
                # page sizes the tools are tuned with. Sorted where the container
                # has no inherent order, so a set's hash-randomised repr does not
                # diff against itself between two runs.
                value = obj
                if isinstance(value, dict):
                    value = sorted((k, repr(v)) for k, v in value.items())
                elif isinstance(value, (set, frozenset)):
                    value = sorted(value)
                shown = repr(value)
                # Objects with no __repr__ of their own print their memory address,
                # which differs on every run — the FastMCP server instance itself is
                # one. Record the type: that it is still the same KIND of object is
                # the part a move can break.
                if " at 0x" in shown:
                    shown = f"<{type(obj).__module__}.{type(obj).__qualname__}>"
                out.write(f"\n--- {name} = {shown}\n")

        section(out, "python API: re-exported modules and classes (not a contract)")
        for name in names:
            obj = getattr(mcp_server, name)
            if inspect.ismodule(obj):
                out.write(f"module {name}\n")
            elif inspect.isclass(obj):
                out.write(f"class  {name}\n")


if __name__ == "__main__":
    snapshot(sys.argv[1])
    print(f"wrote {sys.argv[1]}")
