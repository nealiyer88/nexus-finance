"""Fixture-load smoke test for the SQLite graph store schema.

Loads db/schema_sqlite.sql into an in-memory SQLite database and inserts
all entities from tests/fixtures/canonical_ground_truth.json into
canonical_entities + system_references. Asserts:

  - canonical_entities count == 44
  - system_references count == sum of source records across all entities
  - entity_aliases count == 0 (V1: aliases not populated by this test)
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "canonical_ground_truth.json"

SOURCE_TO_CATEGORY = {
    "quickbooks": "accounting",
    "ruddr": "psa",
}


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    try:
        yield c
    finally:
        c.close()


def test_fixture_loads_into_sqlite(conn: sqlite3.Connection) -> None:
    fixture = json.loads(FIXTURE.read_text())
    entities = fixture["canonical_entities"]

    expected_sys_refs = 0
    for ent in entities:
        conn.execute(
            """
            INSERT INTO canonical_entities (
                canonical_id, canonical_name, entity_type, entity_category,
                confidence, match_pattern, match_signals
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ent["canonical_id"],
                ent["canonical_name"],
                ent["entity_type"],
                ent["entity_category"],
                ent.get("confidence"),
                ent.get("pattern"),
                json.dumps(ent.get("match_signals", [])),
            ),
        )

        for source_key, source_rec in ent.get("sources", {}).items():
            category = SOURCE_TO_CATEGORY.get(source_key, source_key)
            conn.execute(
                """
                INSERT INTO system_references (
                    canonical_id, source, category, external_id, external_fields
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ent["canonical_id"],
                    source_key,
                    category,
                    source_rec["id"],
                    json.dumps(source_rec),
                ),
            )
            expected_sys_refs += 1

    conn.commit()

    canonical_count = conn.execute("SELECT COUNT(*) FROM canonical_entities").fetchone()[0]
    sys_ref_count = conn.execute("SELECT COUNT(*) FROM system_references").fetchone()[0]
    alias_count = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]

    assert canonical_count == 44, f"expected 44 canonical entities, got {canonical_count}"
    assert sys_ref_count == expected_sys_refs, (
        f"expected {expected_sys_refs} system_references, got {sys_ref_count}"
    )
    assert alias_count == 0, f"expected 0 entity_aliases, got {alias_count}"
