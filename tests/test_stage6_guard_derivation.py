"""Meta-test for feature 10c: proves the source-reading grep-guards
`tests/test_resolution.py` runs over `core/graph/resolution.py` (and
its Signal-B3 / append-only siblings) are still in force, unmodified,
after this feature's Stage 6 call-site edit.

No regex, substring, or path is transcribed here — that would defeat
the point. Instead this imports the shipped guard test module and
re-invokes its guard functions directly: they ARE the derivation. A
sweep of every `test_*` function in that module that reads
`resolution.py`'s source picks up all three guards automatically
(the Postgres-token regex, the Signal-B3 ledger-table substring, and
the append-only UPDATE/DELETE substring pair) without hardcoding how
many there are or what they check.
"""

from __future__ import annotations

import inspect

import tests.test_resolution as guard_module

RESOLUTION_PATH_MARKERS = ("resolution.py", "ENTITY_STORE_PATH", "training_data.py")


def _guard_test_functions():
    """Every top-level `test_*` function in `tests/test_resolution.py`
    whose source mentions one of the modules under guard."""
    found = []
    for name in dir(guard_module):
        if not name.startswith("test_"):
            continue
        fn = getattr(guard_module, name)
        if not inspect.isfunction(fn):
            continue
        src = inspect.getsource(fn)
        if any(marker in src for marker in RESOLUTION_PATH_MARKERS):
            found.append((name, fn))
    return found


def test_all_resolution_source_guards_discovered_and_pass() -> None:
    guards = _guard_test_functions()
    assert len(guards) >= 3, (
        f"expected at least 3 source-reading guards over resolution.py, found {len(guards)}: "
        f"{[name for name, _ in guards]}"
    )
    for name, fn in guards:
        # Every guard discovered here takes no fixtures — call directly.
        fn()
