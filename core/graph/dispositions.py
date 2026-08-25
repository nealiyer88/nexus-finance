"""Terminal disposition -> Postgres `approval_decisions.disposition` mapping.

`MAPPING` is an identity mapping over the terminal values of the SQLite
`pending_decisions.status` CHECK constraint — that CHECK's value list minus
its single non-terminal "still pending" value (identified as whichever
value the column's `DEFAULT` names, since a freshly enqueued row starts
non-terminal). Both the CHECK's migration file and its value list are
located and parsed at import time by listing `db/migrations/` and scanning
for the `pending_decisions` table's `status` column — never by a filename
or value written into this module. `tests/test_dispositions.py` performs
the equivalent derivation independently (parsing `db/schema.sql`'s
`approval_decisions.disposition` CHECK too) and asserts the two sets
coincide, confirming this is a pure identity mapping with no translation,
aliasing, or case-normalization layer.

The mapping covers the full terminal set: every terminal value found is a
key, regardless of whether anything in the tree currently produces it.
Feature 10c's approvals writer imports `MAPPING` to translate a resolved
`pending_decisions.status` into the value it writes to
`approval_decisions.disposition`.

`APPROVAL_STATUS` is the single terminal value that denotes "this match
was approved" — the status Stage 6's CONFIRMED path records. It, too, is
derived rather than transcribed: the graph DDL (`db/schema_sqlite.sql`)
records the act of approving an edge in `entity_edges` columns named
`<status>_<suffix>` (the column naming *who* approved the edge), and
exactly one terminal value from the CHECK appears as such a prefix. Both
sides of that intersection come from DDL text; no terminal value is
written into this module, and a change to either DDL that breaks the
one-to-one correspondence raises at import time rather than silently
picking a wrong value.

No database is required to import or use this module.
"""

from __future__ import annotations

import re
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"

# Matches: status TEXT NOT NULL DEFAULT 'x' CHECK (status IN ('a', 'b', ...))
# tolerant of whitespace/newlines between the column name and its CHECK.
_STATUS_COLUMN_RE = re.compile(
    r"status\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'(?P<default>[^']+)'"
    r"\s*CHECK\s*\(\s*status\s+IN\s*\((?P<values>[^)]*)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def _locate_pending_decisions_migration() -> Path:
    """Find the SQLite-dialect migration that defines `pending_decisions`.

    Listed from `db/migrations/` at call time; never a hardcoded filename.
    """
    for path in sorted(_MIGRATIONS_DIR.glob("*_sqlite.sql")):
        text = path.read_text()
        if "CREATE TABLE" in text and "pending_decisions" in text and _STATUS_COLUMN_RE.search(text):
            return path
    raise RuntimeError("no db/migrations/*_sqlite.sql file defines pending_decisions.status")


def _terminal_set() -> frozenset[str]:
    text = _locate_pending_decisions_migration().read_text()
    match = _STATUS_COLUMN_RE.search(text)
    assert match is not None
    default = match.group("default")
    values = {v.strip().strip("'") for v in match.group("values").split(",")}
    return frozenset(values - {default})


TERMINAL_SET: frozenset[str] = _terminal_set()

# Identity mapping over the terminal set — see module docstring.
MAPPING: dict[str, str] = {value: value for value in TERMINAL_SET}


_GRAPH_DDL = Path(__file__).resolve().parents[2] / "db" / "schema_sqlite.sql"

_EDGES_TABLE_RE = re.compile(
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+entity_edges\s*\((?P<body>.*?)\n\s*\);",
    re.IGNORECASE | re.DOTALL,
)


def _edge_column_name_prefixes() -> frozenset[str]:
    """Leading `<prefix>_` segment of every `entity_edges` column name."""
    text = _GRAPH_DDL.read_text()
    match = _EDGES_TABLE_RE.search(text)
    if match is None:
        raise RuntimeError(f"{_GRAPH_DDL.name} does not define an entity_edges table")
    prefixes = set()
    for line in match.group("body").splitlines():
        column = line.strip().split(" ", 1)[0].strip(",")
        if "_" in column:
            prefixes.add(column.split("_", 1)[0])
    return frozenset(prefixes)


def _approval_status() -> str:
    """The terminal status that records an approval — derived, never written.

    See the module docstring: the intersection of the terminal set with
    the `entity_edges` column-name prefixes must be exactly one value.
    """
    candidates = TERMINAL_SET & _edge_column_name_prefixes()
    if len(candidates) != 1:
        raise RuntimeError(
            "cannot derive the approval status: expected exactly one terminal "
            "value to name an entity_edges column prefix, found "
            f"{len(candidates)} ({sorted(candidates)})"
        )
    return next(iter(candidates))


APPROVAL_STATUS: str = _approval_status()
