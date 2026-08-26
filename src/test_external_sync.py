"""Tests for the external-pick sync's row shapes and benchmark range.

Runs offline — no model calls, no database, no network:

    python test_external_sync.py

WHY THESE EXIST. `sync_external_picks.py` reads rows straight out of psycopg2,
and for four commits two lines in it read the WRONG COLUMNS: `newly_closed`
gained a leading `stream` label when it started settling pipeline picks too, and
the code that built the benchmark date range went on indexing [2] and [5] for
the purchase and exit dates. It got the ticker and the benchmark price instead,
and every committed run with something sold ended on

    TypeError: unsupported operand type(s) for -: 'str' and 'datetime.timedelta'

The dry run unpacked the same rows by name, so it kept describing, correctly,
work it was then unable to do. That is the shape of failure worth testing for:
one where the safe mode passes and only the writing mode breaks.

So both queries now return namedtuples, and these tests pin the two things a
namedtuple alone does not: that its field count still matches its query's SELECT
list, and that the date range built from those rows covers what it must — and is
None, rather than an empty range, on a run that has nothing to price at all.
"""
import inspect
import re
from datetime import date
from decimal import Decimal

import sync_external_picks as sx


def _select_columns(func) -> int:
    """How many columns the FIRST SELECT in `func`'s SQL returns.

    Counts commas at paren depth zero, so `SUM(a * b) / NULLIF(SUM(c), 0)` is
    one column and not three. Crude, and only has to hold for the two queries
    here — its job is to notice a column being added or removed, which is
    exactly what went wrong.
    """
    src = inspect.getsource(func)
    body = re.search(r"\bSELECT\b(.*?)\n\s*FROM\b", src, re.S | re.I).group(1)
    depth, cols = 0, 1
    for ch in re.sub(r"--[^\n]*", "", body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            cols += 1
    return cols


def _closure(bought, exit_date, stream="external"):
    """A `newly_closed` row, in its real column order.

    Positional on purpose. Built by keyword these tests would pass even if the
    query and the namedtuple disagreed about order, which is the disagreement
    that caused the bug.
    """
    return sx.Closure(stream, "v-1", "BN", bought, Decimal("23.88"),
                      Decimal("399.90"), exit_date, Decimal("41.88"))


def _pending(purchase_date, exit_date=None):
    return sx.Pending("CIEN", purchase_date, Decimal("4"), Decimal("249.62"),
                      Decimal("998.48"), 1, exit_date is not None,
                      exit_date, Decimal("300.00") if exit_date else None)


def run() -> int:
    checks = []

    # A query and its namedtuple drifting apart is the whole bug. The * unpack
    # in each function makes that loud at runtime; this makes it loud in CI.
    checks.append(("Pending matches pending_purchases' SELECT",
                   len(sx.Pending._fields) == _select_columns(sx.pending_purchases)))
    checks.append(("Closure matches newly_closed's SELECT",
                   len(sx.Closure._fields) == _select_columns(sx.newly_closed)))

    # The regression itself. A settled position is priced at BOTH ends, so the
    # range has to reach back to a purchase that may be years older than
    # anything else on the run — 2022 here, and the sale is today.
    closures = [_closure(date(2022, 11, 22), date(2026, 8, 26))]
    checks.append(("a settled pick spans purchase to exit",
                   sx.benchmark_window([], closures)
                   == (date(2022, 11, 22), date(2026, 8, 26))))

    # Both ends must be dates, not whatever else sat in those columns. This is
    # the assertion the crashing line would have failed: `str - timedelta`.
    lo, hi = sx.benchmark_window([], closures)
    checks.append(("both ends of the range are dates",
                   isinstance(lo, date) and isinstance(hi, date)))

    # A purchase still held is priced only at its purchase date.
    checks.append(("a held purchase ends at its own date",
                   sx.benchmark_window([_pending(date(2026, 1, 6))], [])
                   == (date(2026, 1, 6), date(2026, 1, 6))))

    # A purchase mirrored ALREADY EXITED is graded buy-to-exit in the same run,
    # so its exit date has to be in the range too.
    checks.append(("a purchase mirrored already-exited reaches its exit",
                   sx.benchmark_window([_pending(date(2026, 1, 6), date(2026, 5, 1))], [])
                   == (date(2026, 1, 6), date(2026, 5, 1))))

    # Mixed: the earliest start and the latest end, across both lists.
    checks.append(("mixed run takes the outer bounds",
                   sx.benchmark_window([_pending(date(2026, 1, 6))], closures)
                   == (date(2022, 11, 22), date(2026, 8, 26))))

    # None, not an empty range. A run with only a reprice or an unmirror to do
    # needs no benchmark — repricing re-derives from marks already stored, and
    # unmirroring only deletes — and `min()` over an empty list would end the
    # run before it did the work it was called for.
    checks.append(("nothing to price asks for no range",
                   sx.benchmark_window([], []) is None))

    failures = []
    for name, ok in checks:
        failures += [] if ok else [name]
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {ok}")

    print()
    print(f"{len(checks) - len(failures)}/{len(checks)} passed.")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
