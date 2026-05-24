"""Tests for Pipeline Stage 1: Deterministic Match."""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Optional

import pytest

from connectors.base import NormalizedEntity
from core.matching.deterministic import (
    ALIAS_EXACT_CEILING,
    EMAIL_CONFIDENCE,
    EMPLOYEE_ID_CONFIDENCE,
    deterministic_match,
)
from core.matching.types import DeterministicMatch


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    try:
        yield c
    finally:
        c.close()


def _insert_canonical(
    conn: sqlite3.Connection,
    canonical_id: str,
    canonical_name: str,
    entity_type: str = "client",
    entity_category: str = "organization",
    tenant_id: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO canonical_entities (
            canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (canonical_id, tenant_id, canonical_name, entity_type, entity_category, 0.95),
    )


def _insert_alias(
    conn: sqlite3.Connection,
    canonical_id: str,
    value: str,
    source: str = "canonical",
    category: str = "canonical",
    confidence: float = 1.0,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_aliases (canonical_id, value, source, category, confidence)
        VALUES (?, ?, ?, ?, ?)
        """,
        (canonical_id, value, source, category, confidence),
    )


def _insert_sysref(
    conn: sqlite3.Connection,
    canonical_id: str,
    source: str,
    category: str,
    external_id: str,
    external_fields: Optional[dict] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO system_references (
            canonical_id, source, category, external_id, external_fields
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            canonical_id,
            source,
            category,
            external_id,
            json.dumps(external_fields or {}),
        ),
    )


def _entity(
    *,
    normalized_name: str,
    source: str = "quickbooks",
    source_id: str = "QB-NEW",
    entity_category: str = "organization",
    email: Optional[str] = None,
    raw_record: Optional[dict] = None,
) -> NormalizedEntity:
    return NormalizedEntity(
        raw_name=normalized_name,
        normalized_name=normalized_name,
        entity_category=entity_category,
        source=source,
        category="accounting" if source == "quickbooks" else "psa",
        source_id=source_id,
        email=email,
        email_is_person=entity_category == "person",
        raw_record=raw_record or {},
        rules_applied=[],
    )


# ---------------------------------------------------------------------------
# Stage 1a: exact alias
# ---------------------------------------------------------------------------


def test_alias_exact_single_hit_uses_min_with_ceiling(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-A", "acme widgets")
    _insert_alias(conn, "CAN-A", "acme widgets co", confidence=0.85)
    conn.commit()

    match = deterministic_match(_entity(normalized_name="acme widgets co"), conn)

    assert match == DeterministicMatch(
        canonical_id="CAN-A", confidence=0.85, match_key_type="alias_exact"
    )


def test_alias_exact_caps_at_ceiling(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-B", "beta corp")
    _insert_alias(conn, "CAN-B", "beta corp inc", confidence=1.0)
    conn.commit()

    match = deterministic_match(_entity(normalized_name="beta corp inc"), conn)

    assert match is not None
    assert match.confidence == ALIAS_EXACT_CEILING
    assert match.match_key_type == "alias_exact"


def test_canonical_name_acts_as_seed_alias(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-C", "cenlar")
    conn.commit()

    match = deterministic_match(_entity(normalized_name="cenlar"), conn)

    assert match is not None
    assert match.canonical_id == "CAN-C"
    assert match.match_key_type == "alias_exact"
    assert match.confidence == ALIAS_EXACT_CEILING


def test_alias_exact_dedupes_same_canonical_from_seed_and_alias(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CAN-DUP", "duplo")
    _insert_alias(conn, "CAN-DUP", "duplo", confidence=0.8)
    conn.commit()

    match = deterministic_match(_entity(normalized_name="duplo"), conn)

    assert match is not None
    assert match.canonical_id == "CAN-DUP"
    assert match.match_key_type == "alias_exact"
    assert match.confidence == ALIAS_EXACT_CEILING


def test_alias_collision_returns_none(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-D", "delta one")
    _insert_canonical(conn, "CAN-E", "delta two")
    _insert_alias(conn, "CAN-D", "delta", confidence=0.9)
    _insert_alias(conn, "CAN-E", "delta", confidence=0.9)
    conn.commit()

    match = deterministic_match(_entity(normalized_name="delta"), conn)

    assert match is None


def test_no_alias_hit_returns_none(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-F", "foxtrot")
    conn.commit()

    match = deterministic_match(_entity(normalized_name="nonexistent"), conn)

    assert match is None


# ---------------------------------------------------------------------------
# Stage 1b: email (person-only, person-canonical-only)
# ---------------------------------------------------------------------------


def test_email_single_hit_person(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-P1", "bob chen", entity_type="person", entity_category="person"
    )
    _insert_sysref(
        conn,
        "CAN-P1",
        "ruddr",
        "psa",
        "RUDDR-020",
        {"email": "bob.chen@clientcorp.com"},
    )
    conn.commit()

    entity = _entity(
        normalized_name="robert chen",
        source="quickbooks",
        source_id="QB-NEW",
        entity_category="person",
        email="bob.chen@clientcorp.com",
    )
    match = deterministic_match(entity, conn)

    assert match == DeterministicMatch(
        canonical_id="CAN-P1",
        confidence=EMAIL_CONFIDENCE,
        match_key_type="email",
    )


def test_email_collision_returns_none(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-P2", "alice one", entity_type="person", entity_category="person"
    )
    _insert_canonical(
        conn, "CAN-P3", "alice two", entity_type="person", entity_category="person"
    )
    _insert_sysref(
        conn, "CAN-P2", "quickbooks", "accounting", "QB-100",
        {"email": "shared@inbox.com"},
    )
    _insert_sysref(
        conn, "CAN-P3", "ruddr", "psa", "RUDDR-100",
        {"email": "shared@inbox.com"},
    )
    conn.commit()

    entity = _entity(
        normalized_name="alice three",
        entity_category="person",
        email="shared@inbox.com",
    )
    match = deterministic_match(entity, conn)

    assert match is None


def test_email_match_to_org_canonical_rejected_for_person(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-O", "cenlar")
    _insert_sysref(
        conn, "CAN-O", "quickbooks", "accounting", "QB-001",
        {"email": "billing@cenlarfsb.com"},
    )
    conn.commit()

    entity = _entity(
        normalized_name="someone",
        entity_category="person",
        email="billing@cenlarfsb.com",
    )
    match = deterministic_match(entity, conn)

    assert match is None


def test_email_path_skipped_for_org_query(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-P5", "edna doe", entity_type="person", entity_category="person"
    )
    _insert_sysref(
        conn, "CAN-P5", "ruddr", "psa", "RUDDR-200",
        {"email": "edna@example.com"},
    )
    conn.commit()

    entity = _entity(
        normalized_name="edna",
        entity_category="organization",
        email="edna@example.com",
    )
    match = deterministic_match(entity, conn)

    assert match is None


def test_email_case_insensitive(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-P6", "ivan kim", entity_type="person", entity_category="person"
    )
    _insert_sysref(
        conn, "CAN-P6", "ruddr", "psa", "RUDDR-300",
        {"email": "Ivan.Kim@Example.COM"},
    )
    conn.commit()

    # Use a different normalized_name so alias_exact does NOT short-circuit;
    # this isolates the email-path case-insensitive behavior.
    entity = _entity(
        normalized_name="i kim",
        entity_category="person",
        email="ivan.kim@example.com",
    )
    match = deterministic_match(entity, conn)

    assert match is not None
    assert match.canonical_id == "CAN-P6"
    assert match.match_key_type == "email"


# ---------------------------------------------------------------------------
# Stage 1c: employee_id (person-only)
# ---------------------------------------------------------------------------


def test_employee_id_single_hit(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-EMP", "neal iyer", entity_type="person", entity_category="person"
    )
    _insert_sysref(
        conn, "CAN-EMP", "quickbooks", "accounting", "QB-EMP-7",
        {"employee_id": "E1234"},
    )
    conn.commit()

    entity = _entity(
        normalized_name="iyer",
        entity_category="person",
        raw_record={"employee_id": "E1234"},
    )
    match = deterministic_match(entity, conn)

    assert match == DeterministicMatch(
        canonical_id="CAN-EMP",
        confidence=EMPLOYEE_ID_CONFIDENCE,
        match_key_type="employee_id",
    )


def test_employee_id_collision_returns_none(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-EA", "a", entity_type="person", entity_category="person"
    )
    _insert_canonical(
        conn, "CAN-EB", "b", entity_type="person", entity_category="person"
    )
    _insert_sysref(
        conn, "CAN-EA", "quickbooks", "accounting", "QB-1",
        {"employee_id": "DUP"},
    )
    _insert_sysref(
        conn, "CAN-EB", "ruddr", "psa", "RUDDR-2",
        {"employee_id": "DUP"},
    )
    conn.commit()

    entity = _entity(
        normalized_name="x",
        entity_category="person",
        raw_record={"employee_id": "DUP"},
    )
    match = deterministic_match(entity, conn)

    assert match is None


def test_employee_id_skipped_for_org(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-ORG", "org corp")
    _insert_sysref(
        conn, "CAN-ORG", "quickbooks", "accounting", "QB-9",
        {"employee_id": "EMP-9"},
    )
    conn.commit()

    entity = _entity(
        normalized_name="other",
        entity_category="organization",
        raw_record={"employee_id": "EMP-9"},
    )
    match = deterministic_match(entity, conn)

    assert match is None


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


def test_tenant_scoping_blocks_cross_tenant_alias(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-TA", "acme", tenant_id="tenant-a")
    _insert_canonical(conn, "CAN-TB", "acme", tenant_id="tenant-b")
    conn.commit()

    match = deterministic_match(
        _entity(normalized_name="acme"), conn, tenant_id="tenant-a"
    )
    assert match is not None
    assert match.canonical_id == "CAN-TA"

    match_b = deterministic_match(
        _entity(normalized_name="acme"), conn, tenant_id="tenant-b"
    )
    assert match_b is not None
    assert match_b.canonical_id == "CAN-TB"

    no_filter = deterministic_match(_entity(normalized_name="acme"), conn)
    assert no_filter is None  # collision when no tenant filter


def test_no_tenant_filter_when_none(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-NT", "single", tenant_id="tenant-a")
    conn.commit()

    match = deterministic_match(_entity(normalized_name="single"), conn)
    assert match is not None
    assert match.canonical_id == "CAN-NT"


# ---------------------------------------------------------------------------
# Sub-stage ordering: alias hit short-circuits email / employee_id
# ---------------------------------------------------------------------------


def test_alias_hit_short_circuits_email(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-S1", "shared name", entity_type="person", entity_category="person"
    )
    _insert_canonical(
        conn, "CAN-S2", "other", entity_type="person", entity_category="person"
    )
    _insert_sysref(
        conn, "CAN-S2", "ruddr", "psa", "RUDDR-S2",
        {"email": "x@example.com"},
    )
    conn.commit()

    entity = _entity(
        normalized_name="shared name",
        entity_category="person",
        email="x@example.com",
    )
    match = deterministic_match(entity, conn)

    assert match is not None
    assert match.canonical_id == "CAN-S1"
    assert match.match_key_type == "alias_exact"
