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
