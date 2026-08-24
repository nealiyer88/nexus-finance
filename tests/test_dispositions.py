"""Tests for `core.graph.dispositions` (feature 10a).

No database required. Every set compared here is derived at test time by
parsing `db/schema.sql` and the SQLite pending-decisions migration
directly — no CHECK value or filename is transcribed as a literal.
"""

from __future__ import annotations

import pathlib
import re

from core.graph.dispositions import MAPPING

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"

_STATUS_COLUMN_RE = re.compile(
    r"status\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'(?P<default>[^']+)'"
    r"\s*CHECK\s*\(\s*status\s+IN\s*\((?P<values>[^)]*)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)

_DISPOSITION_CHECK_RE = re.compile(
    r"disposition\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*disposition\s+IN\s*\((?P<values>[^)]*)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_quoted_list(raw: str) -> set[str]:
    return {v.strip().strip("'") for v in raw.split(",") if v.strip()}


def _derive_terminal_set() -> set[str]:
    """Find the SQLite migration defining `pending_decisions.status` by
    listing `db/migrations/` and scanning content — never by filename."""
    for path in sorted(MIGRATIONS_DIR.glob("*_sqlite.sql")):
        text = path.read_text()
        if "pending_decisions" not in text:
            continue
        match = _STATUS_COLUMN_RE.search(text)
        if match:
            values = _parse_quoted_list(match.group("values"))
            return values - {match.group("default")}
    raise AssertionError("no db/migrations/*_sqlite.sql defines pending_decisions.status")


def _derive_check_set() -> set[str]:
    text = SCHEMA_SQL.read_text()
    match = _DISPOSITION_CHECK_RE.search(text)
    assert match is not None, "approval_decisions.disposition CHECK not found in db/schema.sql"
    return _parse_quoted_list(match.group("values"))


def test_mapping_keys_equal_terminal_set() -> None:
    terminal_set = _derive_terminal_set()
    assert set(MAPPING.keys()) == terminal_set


def test_mapping_values_subset_of_check_set() -> None:
    check_set = _derive_check_set()
    assert set(MAPPING.values()) <= check_set


def test_mapping_is_non_empty() -> None:
    assert len(MAPPING) > 0


def test_mapping_is_identity_and_sets_coincide() -> None:
    terminal_set = _derive_terminal_set()
    check_set = _derive_check_set()
    assert terminal_set == check_set
    for key, value in MAPPING.items():
        assert key == value
