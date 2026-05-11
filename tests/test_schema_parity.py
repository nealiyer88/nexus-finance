"""Schema parity test.

Asserts that the four shared tables in db/schema.sql (Postgres) and
db/schema_sqlite.sql (SQLite, V1 dormant) declare the same column names.
Types are allowed to differ (UUID/JSONB on Postgres vs. TEXT on SQLite).

Tables checked:
  - canonical_entities
  - entity_aliases
  - entity_edges
  - system_references
"""

from __future__ import annotations

import pathlib
import re

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PG_SCHEMA = REPO_ROOT / "db" / "schema.sql"
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"

SHARED_TABLES = [
    "canonical_entities",
    "entity_aliases",
    "entity_edges",
    "system_references",
]

# Tokens that begin a non-column constraint clause inside CREATE TABLE.
_CONSTRAINT_KEYWORDS = {
    "PRIMARY",
    "UNIQUE",
    "FOREIGN",
    "CHECK",
    "CONSTRAINT",
}

# Columns that are intentionally Postgres-only or SQLite-only and excluded
# from parity comparison.
# system_references.tenant_id: Postgres only (multi-tenant operational store);
# the SQLite graph store is single-tenant per V1 spec.
EXCLUDE: dict[str, set[str]] = {
    "canonical_entities": {"tenant_id"},
    "entity_aliases": set(),
    "entity_edges": set(),
    "system_references": {"tenant_id"},
}


def _strip_sql_comments(sql: str) -> str:
    """Remove `-- ...` line comments so they don't leak into column parsing."""
    return re.sub(r"--[^\n]*", "", sql)


def _extract_table_block(sql: str, table: str) -> str:
    """Return the parenthesised body of `CREATE TABLE <table> (...)`."""
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + re.escape(table) + r"\s*\((.*?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(_strip_sql_comments(sql))
    if not m:
        raise AssertionError(f"CREATE TABLE for {table!r} not found")
    return m.group(1)


def _split_top_level_commas(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _column_names(sql: str, table: str) -> list[str]:
    body = _extract_table_block(sql, table)
    cols: list[str] = []
    for part in _split_top_level_commas(body):
        stripped = part.strip()
        if not stripped:
            continue
        first = stripped.split(None, 1)[0].upper().strip(",")
        if first in _CONSTRAINT_KEYWORDS:
            continue
        name = stripped.split(None, 1)[0].strip('"').strip(",")
        cols.append(name)
    return cols


@pytest.fixture(scope="module")
def pg_sql() -> str:
    return PG_SCHEMA.read_text()


@pytest.fixture(scope="module")
def sqlite_sql() -> str:
    return SQLITE_SCHEMA.read_text()


@pytest.mark.parametrize("table", SHARED_TABLES)
def test_column_parity(pg_sql: str, sqlite_sql: str, table: str) -> None:
    pg_cols = set(_column_names(pg_sql, table)) - EXCLUDE[table]
    lite_cols = set(_column_names(sqlite_sql, table)) - EXCLUDE[table]
    missing_in_sqlite = pg_cols - lite_cols
    missing_in_pg = lite_cols - pg_cols
    assert not missing_in_sqlite and not missing_in_pg, (
        f"Column parity mismatch for {table}: "
        f"missing_in_sqlite={sorted(missing_in_sqlite)}, "
        f"missing_in_pg={sorted(missing_in_pg)}"
    )
