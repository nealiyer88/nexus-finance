"""Tests for the transaction writer (feature 12a):
`core.ingestion.transactions`.

Pure SQLite, no database server, no network, no `DATABASE_URL`, no live
HTTP client. Schema is built from files — `db/schema_sqlite.sql`, then
every `*_sqlite.sql` migration under `db/migrations/`, sorted — following
the `REPO_ROOT / "db" / ...` path-constant + `executescript` pattern in
`tests/test_fixture_loads.py`.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import List, Optional

import pytest

from connectors.base import ConnectorInterface, DateRange, NormalizedTransaction
from connectors.quickbooks import QuickBooksConnector
from connectors.ruddr import RUDDRConnector
from core.graph.entity_store import count_amount_cooccurrence_periods
from core.ingestion.normalizer import normalize_entity
from core.ingestion.transactions import (
    TransactionIngestionSummary,
    derive_period,
    ingest_transactions,
    upsert_transaction,
)
from core.matching.scoring import BoostEntry, score_pair

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
TRANSACTIONS_MODULE = REPO_ROOT / "core" / "ingestion" / "transactions.py"

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
QB_ENTITIES_FIXTURE = FIXTURES_DIR / "qb_entities.json"
RUDDR_ENTITIES_FIXTURE = FIXTURES_DIR / "ruddr_entities.json"
QB_TXN_FIXTURE = FIXTURES_DIR / "qb_transactions.json"
RUDDR_TXN_FIXTURE = FIXTURES_DIR / "ruddr_time_entries.json"
GROUND_TRUTH_FIXTURE = FIXTURES_DIR / "canonical_ground_truth.json"

SOURCE_TO_CATEGORY = {"quickbooks": "accounting", "ruddr": "psa"}

TENANT = "tenant-12a-test"

WIDE_RANGE = DateRange(start="2026-01-01", end="2026-12-31")
NARROW_RANGE = DateRange(start="2026-03-01", end="2026-03-31")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    for migration in sorted(MIGRATIONS_DIR.glob("*_sqlite.sql")):
        c.executescript(migration.read_text())
    try:
        yield c
    finally:
        c.close()


def _qb_connector() -> QuickBooksConnector:
    return QuickBooksConnector(
        tenant_id=TENANT,
        transaction_fixture_path=str(QB_TXN_FIXTURE),
    )


def _ruddr_connector() -> RUDDRConnector:
    return RUDDRConnector(
        tenant_id=TENANT,
        transaction_fixture_path=str(RUDDR_TXN_FIXTURE),
    )


def _seed_graph_from_truth(
    conn: sqlite3.Connection,
    canonical_ids: Optional[set] = None,
    tenant_id: str = TENANT,
) -> None:
    """Seed `canonical_entities` + `system_references` from
    `canonical_ground_truth.json`, restricted to `canonical_ids` when given."""
    truth = json.loads(GROUND_TRUTH_FIXTURE.read_text())
    for canon in truth["canonical_entities"]:
        cid = canon["canonical_id"]
        if canonical_ids is not None and cid not in canonical_ids:
            continue
        conn.execute(
            """
            INSERT INTO canonical_entities (
                canonical_id, tenant_id, canonical_name, entity_type,
                entity_category, confidence
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                tenant_id,
                canon["canonical_name"],
                canon["entity_type"],
                canon["entity_category"],
                canon.get("confidence"),
            ),
        )
        for source, ref in canon["sources"].items():
            conn.execute(
                """
                INSERT INTO system_references (
                    canonical_id, source, category, external_id, external_fields
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (cid, source, SOURCE_TO_CATEGORY[source], ref["id"], None),
            )


def _canonical_id_for(source: str, external_id: str) -> str:
    truth = json.loads(GROUND_TRUTH_FIXTURE.read_text())
    for canon in truth["canonical_entities"]:
        ref = canon["sources"].get(source, {})
        if ref.get("id") == external_id:
            return canon["canonical_id"]
    raise AssertionError(f"no canonical entity maps {source}/{external_id}")


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------


def test_imports() -> None:
    from core.ingestion.transactions import (  # noqa: F401
        TransactionIngestionSummary,
        derive_period,
        ingest_transactions,
        upsert_transaction,
    )


# ---------------------------------------------------------------------------
# 2 / 3. Fixtures exist, non-empty, referentially sound
# ---------------------------------------------------------------------------


def test_fixtures_exist_and_are_nonempty_lists() -> None:
    qb_txns = json.loads(QB_TXN_FIXTURE.read_text())
    ruddr_txns = json.loads(RUDDR_TXN_FIXTURE.read_text())
    assert isinstance(qb_txns, list)
    assert isinstance(ruddr_txns, list)
    assert len(qb_txns) > 0
    assert len(ruddr_txns) > 0


def test_fixture_referential_integrity() -> None:
    qb_txns = json.loads(QB_TXN_FIXTURE.read_text())
    ruddr_txns = json.loads(RUDDR_TXN_FIXTURE.read_text())
    qb_entities = json.loads(QB_ENTITIES_FIXTURE.read_text())
    ruddr_entities = json.loads(RUDDR_ENTITIES_FIXTURE.read_text())

    qb_ids = {e["id"] for e in qb_entities}
    ruddr_ids = {e["id"] for e in ruddr_entities}
    assert qb_ids
    assert ruddr_ids

    qb_refs = {
        t.get("customer_ref") or t.get("vendor_ref")
        for t in qb_txns
        if t.get("customer_ref") or t.get("vendor_ref")
    }
    assert qb_refs
    assert qb_refs.issubset(qb_ids)

    ruddr_refs = {t["client_id"] for t in ruddr_txns if t.get("client_id")}
    assert ruddr_refs
    assert ruddr_refs.issubset(ruddr_ids)


# ---------------------------------------------------------------------------
# 4 / 5. Fixture-mode reads, both empty-return conditions
# ---------------------------------------------------------------------------


def test_connectors_read_transactions_in_fixture_mode() -> None:
    qb_txns = _qb_connector().read_transactions(WIDE_RANGE)
    ruddr_txns = _ruddr_connector().read_transactions(WIDE_RANGE)

    assert len(qb_txns) > 0
    assert len(ruddr_txns) > 0

    for txns in (qb_txns, ruddr_txns):
        for t in txns:
            assert isinstance(t, NormalizedTransaction)
            assert t.source_id
            assert t.txn_date
            assert isinstance(t.amount, float)
            assert t.amount == t.amount  # not NaN
            assert t.amount not in (float("inf"), float("-inf"))


def test_empty_return_conditions_preserved() -> None:
    # Condition (1) preserved for callers that only set the entity fixture.
    qb_entity_only = QuickBooksConnector(
        tenant_id=TENANT, fixture_path=str(QB_ENTITIES_FIXTURE)
    )
    ruddr_entity_only = RUDDRConnector(
        tenant_id=TENANT, fixture_path=str(RUDDR_ENTITIES_FIXTURE)
    )
    assert qb_entity_only.read_transactions(WIDE_RANGE) == []
    assert ruddr_entity_only.read_transactions(WIDE_RANGE) == []

    # Condition (2): no fixture at all, no http_client.
    qb_bare = QuickBooksConnector(tenant_id=TENANT)
    ruddr_bare = RUDDRConnector(tenant_id=TENANT)
    assert qb_bare.read_transactions(WIDE_RANGE) == []
    assert ruddr_bare.read_transactions(WIDE_RANGE) == []


# ---------------------------------------------------------------------------
# 6. Date-range filtering
# ---------------------------------------------------------------------------


def test_date_range_filtering() -> None:
    qb_wide = _qb_connector().read_transactions(WIDE_RANGE)
    qb_narrow = _qb_connector().read_transactions(NARROW_RANGE)
    ruddr_wide = _ruddr_connector().read_transactions(WIDE_RANGE)
    ruddr_narrow = _ruddr_connector().read_transactions(NARROW_RANGE)

    assert len(qb_wide) > 0
    assert len(qb_narrow) > 0
    assert len(qb_narrow) < len(qb_wide)

    assert len(ruddr_wide) > 0
    assert len(ruddr_narrow) > 0
    assert len(ruddr_narrow) < len(ruddr_wide)


# ---------------------------------------------------------------------------
# 7. Rows land
# ---------------------------------------------------------------------------


def test_rows_land(conn: sqlite3.Connection) -> None:
    summary = ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
    count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count > 0
    assert count == summary.inserted
    assert summary.read_total == summary.inserted + summary.updated + summary.skipped


# ---------------------------------------------------------------------------
# 8 / 9. Period derivation
# ---------------------------------------------------------------------------


def test_period_derivation_is_a_property_of_its_own_row(
    conn: sqlite3.Connection,
) -> None:
    ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
    ingest_transactions(_ruddr_connector(), conn, TENANT, WIDE_RANGE)

    rows = conn.execute("SELECT txn_date, period FROM transactions").fetchall()
    assert len(rows) > 0
    for txn_date, period in rows:
        assert period == txn_date[:7]

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    for txn_date, period in rows:
        if period == current_month:
            assert txn_date[:7] == current_month


def test_derive_period_rejects_never_guesses() -> None:
    with pytest.raises(ValueError):
        derive_period("")
    with pytest.raises(ValueError):
        derive_period("not-a-date")


# ---------------------------------------------------------------------------
# 10. Counterparty derivation
# ---------------------------------------------------------------------------


def test_counterparty_is_customer_client_reference(
    conn: sqlite3.Connection,
) -> None:
    ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
    ingest_transactions(_ruddr_connector(), conn, TENANT, WIDE_RANGE)

    qb_txns = json.loads(QB_TXN_FIXTURE.read_text())
    qb_customer_vendor_refs = {
        t.get("customer_ref") or t.get("vendor_ref")
        for t in qb_txns
        if t.get("customer_ref") or t.get("vendor_ref")
    }
    assert qb_customer_vendor_refs

    written_qb = {
        row[0]
        for row in conn.execute(
            "SELECT counterparty_source_id FROM transactions "
            "WHERE source = 'quickbooks' AND counterparty_source_id IS NOT NULL"
        ).fetchall()
    }
    assert written_qb
    assert written_qb.issubset(qb_customer_vendor_refs)

    ruddr_txns = json.loads(RUDDR_TXN_FIXTURE.read_text())
    resource_ids = {t["resource_id"] for t in ruddr_txns if t.get("resource_id")}
    client_ids = {t["client_id"] for t in ruddr_txns if t.get("client_id")}
    assert resource_ids
    assert client_ids
    assert resource_ids.isdisjoint(client_ids)

    written_ruddr = {
        row[0]
        for row in conn.execute(
            "SELECT counterparty_source_id FROM transactions "
            "WHERE source = 'ruddr' AND counterparty_source_id IS NOT NULL"
        ).fetchall()
    }
    assert written_ruddr
    assert written_ruddr.issubset(client_ids)
    assert written_ruddr.isdisjoint(resource_ids)


# ---------------------------------------------------------------------------
# 11 / 12. canonical_id resolution
# ---------------------------------------------------------------------------


def test_canonical_id_set_only_where_system_references_resolves(
    conn: sqlite3.Connection,
) -> None:
    _seed_graph_from_truth(conn)
    ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
    ingest_transactions(_ruddr_connector(), conn, TENANT, WIDE_RANGE)

    resolved = conn.execute(
        "SELECT source, counterparty_source_id FROM transactions "
        "WHERE canonical_id IS NOT NULL"
    ).fetchall()
    assert len(resolved) > 0
    for source, counterparty in resolved:
        row = conn.execute(
            "SELECT 1 FROM system_references WHERE source = ? AND external_id = ?",
            (source, counterparty),
        ).fetchone()
        assert row is not None


def test_canonical_id_all_null_against_empty_graph(conn: sqlite3.Connection) -> None:
    ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
    ingest_transactions(_ruddr_connector(), conn, TENANT, WIDE_RANGE)

    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert total > 0
    non_null = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE canonical_id IS NOT NULL"
    ).fetchone()[0]
    assert non_null == 0


def test_null_upgrades_to_resolved_on_rerun(conn: sqlite3.Connection) -> None:
    ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
    ingest_transactions(_ruddr_connector(), conn, TENANT, WIDE_RANGE)

    total_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert total_before > 0
    non_null_before = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE canonical_id IS NOT NULL"
    ).fetchone()[0]
    assert non_null_before == 0
    txn_ids_before = {
        row[0] for row in conn.execute("SELECT txn_id FROM transactions").fetchall()
    }

    _seed_graph_from_truth(conn)
    ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
    ingest_transactions(_ruddr_connector(), conn, TENANT, WIDE_RANGE)

    total_after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert total_after == total_before
    non_null_after = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE canonical_id IS NOT NULL"
    ).fetchone()[0]
    assert non_null_after > 0
    txn_ids_after = {
        row[0] for row in conn.execute("SELECT txn_id FROM transactions").fetchall()
    }
    assert txn_ids_after == txn_ids_before


# ---------------------------------------------------------------------------
# 13. Idempotency
# ---------------------------------------------------------------------------


def test_idempotency(conn: sqlite3.Connection) -> None:
    connector = _qb_connector()
    ingest_transactions(connector, conn, TENANT, WIDE_RANGE)

    count_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count_before > 0
    txn_ids_before = {
        row[0] for row in conn.execute("SELECT txn_id FROM transactions").fetchall()
    }
    assert txn_ids_before
    created_at_before = {
        row[0] for row in conn.execute("SELECT created_at FROM transactions").fetchall()
    }

    second = ingest_transactions(connector, conn, TENANT, WIDE_RANGE)

    count_after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count_after == count_before
    assert second.inserted == 0
    assert second.updated > 0
    txn_ids_after = {
        row[0] for row in conn.execute("SELECT txn_id FROM transactions").fetchall()
    }
    assert txn_ids_after == txn_ids_before
    created_at_after = {
        row[0] for row in conn.execute("SELECT created_at FROM transactions").fetchall()
    }
    assert created_at_after == created_at_before


# ---------------------------------------------------------------------------
# 14. INSERT OR REPLACE absent
# ---------------------------------------------------------------------------


def test_insert_or_replace_absent() -> None:
    text = TRANSACTIONS_MODULE.read_text()
    assert re.search(r"insert or replace", text, re.IGNORECASE) is None


# ---------------------------------------------------------------------------
# 15. Tenant requirement + isolation
# ---------------------------------------------------------------------------


def test_tenant_required_on_write_path(conn: sqlite3.Connection) -> None:
    qb_txns = _qb_connector().read_transactions(WIDE_RANGE)
    assert qb_txns
    with pytest.raises(ValueError):
        upsert_transaction(conn, qb_txns[0], None)
    with pytest.raises(ValueError):
        upsert_transaction(conn, qb_txns[0], "")
    with pytest.raises(ValueError):
        ingest_transactions(_qb_connector(), conn, None, WIDE_RANGE)
    with pytest.raises(ValueError):
        ingest_transactions(_qb_connector(), conn, "", WIDE_RANGE)


def test_distinct_tenants_do_not_collide(conn: sqlite3.Connection) -> None:
    tenant_a = "tenant-12a-alpha"
    tenant_b = "tenant-12a-beta"

    summary_a = ingest_transactions(_qb_connector(), conn, tenant_a, WIDE_RANGE)
    summary_b = ingest_transactions(_qb_connector(), conn, tenant_b, WIDE_RANGE)

    count_a = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE tenant_id = ?", (tenant_a,)
    ).fetchone()[0]
    count_b = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE tenant_id = ?", (tenant_b,)
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    assert count_a > 0
    assert count_b > 0
    assert count_a == summary_a.inserted
    assert count_b == summary_b.inserted
    assert total == count_a + count_b


# ---------------------------------------------------------------------------
# 16. Transaction neutrality
# ---------------------------------------------------------------------------


def test_no_commit_or_rollback_in_module() -> None:
    text = TRANSACTIONS_MODULE.read_text()
    assert re.search(r"\.commit\(|\.rollback\(", text) is None


def test_caller_rollback_leaves_no_trace(conn: sqlite3.Connection) -> None:
    conn.isolation_level = ""  # explicit transaction control
    count_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
    conn.rollback()

    count_after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count_after == count_before


# ---------------------------------------------------------------------------
# 17. B3 headline
# ---------------------------------------------------------------------------


def test_b3_stops_being_zero() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SQLITE_SCHEMA.read_text())
    for migration in sorted(MIGRATIONS_DIR.glob("*_sqlite.sql")):
        conn.executescript(migration.read_text())
    try:
        can_id = _canonical_id_for("quickbooks", "QB-001")
        assert can_id == _canonical_id_for("ruddr", "RUDDR-001")
        _seed_graph_from_truth(conn, canonical_ids={can_id})

        pre = count_amount_cooccurrence_periods(
            conn, "quickbooks", "QB-001", can_id, tenant_id=TENANT
        )
        assert pre == 0

        qb_summary = ingest_transactions(_qb_connector(), conn, TENANT, WIDE_RANGE)
        ruddr_summary = ingest_transactions(_ruddr_connector(), conn, TENANT, WIDE_RANGE)
        total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        assert total > 0
        assert qb_summary.inserted > 0
        assert ruddr_summary.inserted > 0

        post = count_amount_cooccurrence_periods(
            conn, "quickbooks", "QB-001", can_id, tenant_id=TENANT
        )
        assert post >= 1

        qb_entity = normalize_entity(
            {
                "id": "QB-001",
                "source": "quickbooks",
                "entity_category": "organization",
                "display_name": "Cenlar, LLC.",
            }
        )

        import unittest.mock as _mock

        with _mock.patch(
            "core.matching.scoring._base_weighted_score", return_value=0.80
        ):
            scored = score_pair(
                entity=qb_entity,
                candidate_id=can_id,
                candidate_name="Cenlar FSB",
                candidate_aliases=(),
                candidate_category="psa",
                conn=conn,
                tenant_id=TENANT,
            )

        assert scored.signal_breakdown.b_boosts
        assert any(
            isinstance(b, BoostEntry) and b.signal_id == "B3"
            for b in scored.signal_breakdown.b_boosts
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 18. Guards count, they do not swallow
# ---------------------------------------------------------------------------


class _StubConnector(ConnectorInterface):
    category = "accounting"

    def __init__(self, records: List[NormalizedTransaction]) -> None:
        self._records = records

    def authenticate(self):  # pragma: no cover - unused
        raise NotImplementedError

    def read_entities(self, entity_type, filters):  # pragma: no cover - unused
        raise NotImplementedError

    def read_transactions(self, date_range: DateRange) -> List[NormalizedTransaction]:
        return list(self._records)

    def read_operational_records(self, record_type, filters):  # pragma: no cover
        raise NotImplementedError

    def validate_write(self, proposal):  # pragma: no cover - unused
        raise NotImplementedError

    def execute_write(self, approved_proposal):  # pragma: no cover - unused
        raise NotImplementedError

    def rollback_write(self, write_result):  # pragma: no cover - unused
        raise NotImplementedError

    def export_csv_fallback(self, entity_type, date_range):  # pragma: no cover
        raise NotImplementedError


def test_guards_count_they_do_not_swallow(conn: sqlite3.Connection) -> None:
    valid = NormalizedTransaction(
        source_id="GUARD-VALID-1",
        source="quickbooks",
        category="accounting",
        txn_type="invoice",
        amount=42.0,
        currency="USD",
        txn_date="2026-05-01",
        counterparty_source_id="QB-001",
        counterparty_kind="customer",
        raw_record={},
    )
    empty_source_id = NormalizedTransaction(
        source_id="",
        source="quickbooks",
        category="accounting",
        txn_type="invoice",
        amount=10.0,
        currency="USD",
        txn_date="2026-05-01",
        counterparty_source_id="QB-002",
        counterparty_kind="customer",
        raw_record={},
    )
    unparseable_date = NormalizedTransaction(
        source_id="GUARD-BAD-DATE-1",
        source="quickbooks",
        category="accounting",
        txn_type="invoice",
        amount=10.0,
        currency="USD",
        txn_date="not-a-date",
        counterparty_source_id="QB-003",
        counterparty_kind="customer",
        raw_record={},
    )

    connector = _StubConnector([valid, empty_source_id, unparseable_date])
    summary = ingest_transactions(connector, conn, TENANT, WIDE_RANGE)

    assert summary.skipped > 0
    assert summary.inserted > 0
    assert summary.read_total == 3

    text = TRANSACTIONS_MODULE.read_text()
    assert "except Exception" not in text


# ---------------------------------------------------------------------------
# 20. One engine, one connection provider
# ---------------------------------------------------------------------------


def test_no_sqlite_connect_in_new_module() -> None:
    text = TRANSACTIONS_MODULE.read_text()
    assert "sqlite3.connect" not in text
    assert not re.search(r"psycopg|DATABASE_URL|postgres", text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# 21. Schema untouched
# ---------------------------------------------------------------------------


def test_schema_files_untouched() -> None:
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "db/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    changed = [line for line in result.stdout.splitlines() if line.strip()]
    assert changed == []
