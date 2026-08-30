"""Tests for AR reconciliation (feature 15): `core.reconciliation.ar`,
the `/reconciliation/ar` API routes, and the AR reconciliation dashboard
page.

Pure SQLite, no database server, no network. `NEXUS_STORE_PATH` points
every test at a per-test temporary file whose schema is built from
`db/schema_sqlite.sql` plus every `*_sqlite.sql` migration (located by
glob, never by number) — the same pattern `tests/test_entity_api.py`
uses. Tests seed `transactions` rows directly, in the shape feature 12a
ingests; they do not exercise 12a's writer.

Dashboard pages are pulled from `sys.modules["pages.<name>"]` after
`import dashboard.app` — Dash's page loader imports every module under
`dashboard/pages/` as `pages.<name>`; a direct
`import dashboard.pages.ar_reconciliation` would re-trigger
`dash.register_page` under a second module name and collide on path.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import sys
import uuid
from typing import Optional

import dash
import pytest
from dash import dash_table
from fastapi.testclient import TestClient

import dashboard.app  # noqa: F401  (registers every page's callbacks once)
from api.main import app
from api.middleware.tenant import DEFAULT_TENANT_ID
from core.graph.entity_store import AMOUNT_TOLERANCE_CAP, AMOUNT_TOLERANCE_PCT
from core.reconciliation.ar import get_ar_detail, get_ar_report

ar_recon_page = sys.modules["pages.ar_reconciliation"]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
SQLITE_MIGRATIONS = sorted((REPO_ROOT / "db" / "migrations").glob("*_sqlite.sql"))
PAGES_DIR = REPO_ROOT / "dashboard" / "pages"
AR_PAGE_SOURCE = PAGES_DIR / "ar_reconciliation.py"


# ---------------------------------------------------------------------------
# Fixtures + shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def store_path(tmp_path, monkeypatch):
    path = tmp_path / "store.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SQLITE_SCHEMA.read_text())
        for migration in SQLITE_MIGRATIONS:
            conn.executescript(migration.read_text())
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("NEXUS_STORE_PATH", str(path))
    return path


@pytest.fixture()
def client(store_path):
    return TestClient(app)


def _connect(path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _insert_canonical(
    path,
    canonical_id: str,
    tenant_id: Optional[str],
    canonical_name: str,
    entity_type: str = "client",
    entity_category: str = "organization",
) -> None:
    conn = _connect(path)
    try:
        conn.execute(
            """
            INSERT INTO canonical_entities (
                canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (canonical_id, tenant_id, canonical_name, entity_type, entity_category, 0.95),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_system_reference(
    path, canonical_id: str, source: str, category: str, external_id: str
) -> None:
    conn = _connect(path)
    try:
        conn.execute(
            """
            INSERT INTO system_references (canonical_id, source, category, external_id)
            VALUES (?, ?, ?, ?)
            """,
            (canonical_id, source, category, external_id),
        )
        conn.commit()
    finally:
        conn.close()


_txn_seq = 0


def _insert_transaction(
    path,
    tenant_id: Optional[str],
    source: str,
    category: str,
    txn_type: str,
    amount: float,
    canonical_id: Optional[str] = None,
    counterparty_source_id: Optional[str] = None,
    period: str = "2026-03",
    txn_date: str = "2026-03-15",
    external_source_id: Optional[str] = None,
) -> str:
    """Seed one `transactions` row, in the shape feature 12a ingests.
    Returns the `external_source_id` used, minting one from `uuid.uuid4()`
    when not given — never a bare literal."""
    global _txn_seq
    _txn_seq += 1
    if external_source_id is None:
        external_source_id = f"{uuid.uuid4()}-{_txn_seq}"
    conn = _connect(path)
    try:
        conn.execute(
            """
            INSERT INTO transactions (
                tenant_id, source, category, external_source_id, txn_type,
                amount, currency, txn_date, period, counterparty_source_id,
                canonical_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?)
            """,
            (
                tenant_id,
                source,
                category,
                external_source_id,
                txn_type,
                amount,
                txn_date,
                period,
                counterparty_source_id,
                canonical_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return external_source_id


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------


def test_imports() -> None:
    from core.reconciliation.ar import (  # noqa: F401
        classify_status,
        get_ar_detail,
        get_ar_report,
    )


# ---------------------------------------------------------------------------
# 2. Tenant predicate checked by execution, not inspection.
# ---------------------------------------------------------------------------


def test_tenant_predicate_is_present_on_every_captured_statement(store_path) -> None:
    tenant_id = str(uuid.uuid4())
    _insert_canonical(store_path, "CLIENT_TRACE", tenant_id, "Trace Co")
    _insert_transaction(
        store_path,
        tenant_id,
        "ruddr",
        "psa",
        "time_entry",
        1000.0,
        canonical_id="CLIENT_TRACE",
    )

    conn = _connect(store_path)
    captured: list[str] = []
    try:
        conn.set_trace_callback(lambda sql: captured.append(sql))
        get_ar_report(conn, tenant_id)
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert captured, "the entry point must execute at least one statement"
    for sql in captured:
        normalized = " ".join(sql.split()).lower()
        if "transactions" in normalized or "canonical_entities" in normalized:
            assert "tenant_id" in normalized, sql


# ---------------------------------------------------------------------------
# 3. Router registration
# ---------------------------------------------------------------------------


def test_reconciliation_routes_are_registered() -> None:
    paths = {r.path for r in app.routes}
    assert "/reconciliation/ar" in paths
    assert "/reconciliation/ar/{canonical_id}" in paths


def test_previously_registered_routers_still_present() -> None:
    paths = {r.path for r in app.routes}
    assert "/health" in paths
    assert "/entities/" in paths
    assert "/approvals/" in paths or any(p.startswith("/approvals") for p in paths)
    assert any(p.startswith("/connectors") for p in paths)


# ---------------------------------------------------------------------------
# 4. Dashboard page structure contract
# ---------------------------------------------------------------------------


def test_ar_page_registers_at_its_own_declared_path() -> None:
    pattern = re.compile(
        r"dash\.register_page\(\s*__name__\s*,\s*path\s*=\s*[\"']([^\"']+)[\"']"
    )
    m = pattern.search(AR_PAGE_SOURCE.read_text())
    assert m, "no dash.register_page(path=...) found in ar_reconciliation.py"
    declared_path = m.group(1)

    registered = {
        entry["path"]
        for entry in dash.page_registry.values()
        if entry["module"].rsplit(".", 1)[-1] == "ar_reconciliation"
    }
    assert registered == {declared_path}


def test_ar_page_layout_is_zero_arg_callable_with_datatable() -> None:
    tree = ar_recon_page.layout()
    assert tree is not None

    def _flatten(component):
        out = [component]
        children = getattr(component, "children", None)
        if isinstance(children, list):
            for child in children:
                out.extend(_flatten(child))
        elif children is not None and hasattr(children, "children"):
            out.extend(_flatten(children))
        return out

    tables = [c for c in _flatten(tree) if isinstance(c, dash_table.DataTable)]
    assert len(tables) == 1
    assert tables[0].id == "ar-recon-table"


def test_ar_page_returns_200_via_shell() -> None:
    client = dashboard.app.app.server.test_client()
    resp = client.get("/ar-reconciliation")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. Variance = PSA total - accounting total, recomputed by the test.
# ---------------------------------------------------------------------------


def test_variance_equals_recomputed_totals(store_path) -> None:
    tenant_id = str(uuid.uuid4())
    _insert_canonical(store_path, "CLIENT_VAR", tenant_id, "Variance Co")

    psa_amounts = [1000.0, 2500.0]
    accounting_amounts = [900.0]
    for amt in psa_amounts:
        _insert_transaction(
            store_path, tenant_id, "ruddr", "psa", "time_entry", amt,
            canonical_id="CLIENT_VAR",
        )
    for amt in accounting_amounts:
        _insert_transaction(
            store_path, tenant_id, "quickbooks", "accounting", "invoice", amt,
            canonical_id="CLIENT_VAR",
        )
    # A non-invoice accounting row must not count toward invoiced_total.
    _insert_transaction(
        store_path, tenant_id, "quickbooks", "accounting", "payment", 5000.0,
        canonical_id="CLIENT_VAR",
    )

    conn = _connect(store_path)
    try:
        report = get_ar_report(conn, tenant_id)
    finally:
        conn.close()

    row = next(r for r in report if r["canonical_id"] == "CLIENT_VAR")
    expected_labor = sum(psa_amounts)
    expected_invoiced = sum(accounting_amounts)
    assert row["labor_total"] == pytest.approx(expected_labor)
    assert row["invoiced_total"] == pytest.approx(expected_invoiced)
    assert row["variance"] == pytest.approx(expected_labor - expected_invoiced)


# ---------------------------------------------------------------------------
# 6. Tolerance boundary
# ---------------------------------------------------------------------------


def test_tolerance_boundary_matched_vs_not(store_path) -> None:
    tenant_id = str(uuid.uuid4())

    labor = 10000.0
    tolerance = min(labor * AMOUNT_TOLERANCE_PCT, AMOUNT_TOLERANCE_CAP)

    # Within tolerance -> MATCHED.
    _insert_canonical(store_path, "CLIENT_TOL_MATCH", tenant_id, "Tolerance Match Co")
    _insert_transaction(
        store_path, tenant_id, "ruddr", "psa", "time_entry", labor,
        canonical_id="CLIENT_TOL_MATCH",
    )
    _insert_transaction(
        store_path, tenant_id, "quickbooks", "accounting", "invoice",
        labor - (tolerance * 0.5),
        canonical_id="CLIENT_TOL_MATCH",
    )

    # Strictly beyond tolerance -> not MATCHED.
    _insert_canonical(store_path, "CLIENT_TOL_MISS", tenant_id, "Tolerance Miss Co")
    _insert_transaction(
        store_path, tenant_id, "ruddr", "psa", "time_entry", labor,
        canonical_id="CLIENT_TOL_MISS",
    )
    _insert_transaction(
        store_path, tenant_id, "quickbooks", "accounting", "invoice",
        labor - (tolerance * 2.0),
        canonical_id="CLIENT_TOL_MISS",
    )

    conn = _connect(store_path)
    try:
        report = get_ar_report(conn, tenant_id)
    finally:
        conn.close()

    by_id = {r["canonical_id"]: r for r in report}
    match_row = by_id["CLIENT_TOL_MATCH"]
    miss_row = by_id["CLIENT_TOL_MISS"]

    match_tolerance = min(
        max(abs(match_row["labor_total"]), abs(match_row["invoiced_total"])) * AMOUNT_TOLERANCE_PCT,
        AMOUNT_TOLERANCE_CAP,
    )
    miss_tolerance = min(
        max(abs(miss_row["labor_total"]), abs(miss_row["invoiced_total"])) * AMOUNT_TOLERANCE_PCT,
        AMOUNT_TOLERANCE_CAP,
    )

    assert abs(match_row["variance"]) < match_tolerance
    assert match_row["status"] == "MATCHED"

    assert abs(miss_row["variance"]) > miss_tolerance
    assert miss_row["status"] != "MATCHED"


# ---------------------------------------------------------------------------
# 7. Unbilled flagged
# ---------------------------------------------------------------------------


def test_unbilled_flagged_when_no_accounting_side(store_path) -> None:
    tenant_id = str(uuid.uuid4())
    _insert_canonical(store_path, "CLIENT_UNBILLED", tenant_id, "Unbilled Co")
    _insert_transaction(
        store_path, tenant_id, "ruddr", "psa", "time_entry", 5000.0,
        canonical_id="CLIENT_UNBILLED",
    )

    conn = _connect(store_path)
    try:
        report = get_ar_report(conn, tenant_id)
    finally:
        conn.close()

    row = next(r for r in report if r["canonical_id"] == "CLIENT_UNBILLED")
    assert row["invoiced_total"] == 0
    assert row["labor_total"] > 0
    assert row["status"] == "UNBILLED"


# ---------------------------------------------------------------------------
# 8. Detail endpoint returns exactly the seeded row set.
# ---------------------------------------------------------------------------


def test_detail_returns_exactly_seeded_rows(store_path) -> None:
    tenant_id = str(uuid.uuid4())
    _insert_canonical(store_path, "CLIENT_DETAIL", tenant_id, "Detail Co")

    seeded = set()
    ext_id_1 = _insert_transaction(
        store_path, tenant_id, "ruddr", "psa", "time_entry", 1200.0,
        canonical_id="CLIENT_DETAIL",
    )
    seeded.add(("ruddr", ext_id_1))
    ext_id_2 = _insert_transaction(
        store_path, tenant_id, "quickbooks", "accounting", "invoice", 900.0,
        canonical_id="CLIENT_DETAIL",
    )
    seeded.add(("quickbooks", ext_id_2))

    # A row belonging to a different canonical must not leak in.
    _insert_canonical(store_path, "CLIENT_OTHER", tenant_id, "Other Co")
    _insert_transaction(
        store_path, tenant_id, "ruddr", "psa", "time_entry", 99.0,
        canonical_id="CLIENT_OTHER",
    )

    assert seeded, "the seeded set must be non-empty before comparison"

    conn = _connect(store_path)
    try:
        detail = get_ar_detail(conn, "CLIENT_DETAIL", tenant_id)
    finally:
        conn.close()

    assert detail is not None
    returned = {
        (t["source"], t["external_source_id"])
        for t in detail["psa_transactions"] + detail["accounting_transactions"]
    }
    assert returned == seeded


def test_detail_endpoint_via_http(client, store_path) -> None:
    tenant_id = DEFAULT_TENANT_ID
    _insert_canonical(store_path, "CLIENT_HTTP", tenant_id, "HTTP Co")
    ext_id = _insert_transaction(
        store_path, tenant_id, "ruddr", "psa", "time_entry", 500.0,
        canonical_id="CLIENT_HTTP",
    )

    resp = client.get("/reconciliation/ar/CLIENT_HTTP")
    assert resp.status_code == 200
    body = resp.json()
    returned = {(t["source"], t["external_source_id"]) for t in body["psa_transactions"]}
    assert returned == {("ruddr", ext_id)}


def test_summary_endpoint_via_http(client, store_path) -> None:
    tenant_id = DEFAULT_TENANT_ID
    _insert_canonical(store_path, "CLIENT_SUMMARY_HTTP", tenant_id, "Summary HTTP Co")
    _insert_transaction(
        store_path, tenant_id, "ruddr", "psa", "time_entry", 750.0,
        canonical_id="CLIENT_SUMMARY_HTTP",
    )

    resp = client.get("/reconciliation/ar")
    assert resp.status_code == 200
    ids = {item["canonical_id"] for item in resp.json()["items"]}
    assert "CLIENT_SUMMARY_HTTP" in ids


# ---------------------------------------------------------------------------
# 9. Tenant isolation
# ---------------------------------------------------------------------------


def test_tenant_isolation_report_and_detail(store_path) -> None:
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())

    _insert_canonical(store_path, "CLIENT_A", tenant_a, "Tenant A Co")
    _insert_canonical(store_path, "CLIENT_B", tenant_b, "Tenant B Co")
    _insert_transaction(
        store_path, tenant_a, "ruddr", "psa", "time_entry", 1000.0,
        canonical_id="CLIENT_A",
    )
    _insert_transaction(
        store_path, tenant_b, "ruddr", "psa", "time_entry", 2000.0,
        canonical_id="CLIENT_B",
    )

    conn = _connect(store_path)
    try:
        report_a = get_ar_report(conn, tenant_a)
        report_b = get_ar_report(conn, tenant_b)
    finally:
        conn.close()

    ids_a = {r["canonical_id"] for r in report_a}
    ids_b = {r["canonical_id"] for r in report_b}
    assert ids_a, "tenant A result set must be non-empty before the disjointness check"
    assert ids_b, "tenant B result set must be non-empty before the disjointness check"
    assert ids_a.isdisjoint(ids_b)

    # Neither report references a row whose canonical belongs to the
    # other tenant.
    assert "CLIENT_B" not in ids_a
    assert "CLIENT_A" not in ids_b

    # Detail for tenant A's canonical is invisible under tenant B, and
    # vice versa.
    conn = _connect(store_path)
    try:
        assert get_ar_detail(conn, "CLIENT_A", tenant_b) is None
        assert get_ar_detail(conn, "CLIENT_B", tenant_a) is None
        detail_a = get_ar_detail(conn, "CLIENT_A", tenant_a)
        detail_b = get_ar_detail(conn, "CLIENT_B", tenant_b)
    finally:
        conn.close()

    assert detail_a is not None
    assert detail_b is not None
    for txn in detail_a["psa_transactions"] + detail_a["accounting_transactions"]:
        assert txn["tenant_id"] == tenant_a
    for txn in detail_b["psa_transactions"] + detail_b["accounting_transactions"]:
        assert txn["tenant_id"] == tenant_b


def test_tenant_isolation_via_http(client, store_path) -> None:
    tenant_a = DEFAULT_TENANT_ID
    tenant_b = str(uuid.uuid4())
    _insert_canonical(store_path, "CLIENT_HTTP_A", tenant_a, "HTTP Tenant A Co")
    _insert_canonical(store_path, "CLIENT_HTTP_B", tenant_b, "HTTP Tenant B Co")
    _insert_transaction(
        store_path, tenant_a, "ruddr", "psa", "time_entry", 300.0,
        canonical_id="CLIENT_HTTP_A",
    )
    _insert_transaction(
        store_path, tenant_b, "ruddr", "psa", "time_entry", 400.0,
        canonical_id="CLIENT_HTTP_B",
    )

    resp_a = client.get("/reconciliation/ar", headers={"X-Nexus-Tenant": tenant_a})
    resp_b = client.get("/reconciliation/ar", headers={"X-Nexus-Tenant": tenant_b})
    ids_a = {item["canonical_id"] for item in resp_a.json()["items"]}
    ids_b = {item["canonical_id"] for item in resp_b.json()["items"]}

    assert ids_a, "tenant A result set must be non-empty before the disjointness check"
    assert ids_b, "tenant B result set must be non-empty before the disjointness check"
    assert ids_a.isdisjoint(ids_b)


# ---------------------------------------------------------------------------
# 10. Fallback join via system_references (unresolved canonical_id)
# ---------------------------------------------------------------------------


def test_fallback_join_via_system_references(store_path) -> None:
    tenant_id = str(uuid.uuid4())
    _insert_canonical(store_path, "CLIENT_FALLBACK", tenant_id, "Fallback Co")
    _insert_system_reference(
        store_path, "CLIENT_FALLBACK", "ruddr", "psa", "ruddr-ext-fallback"
    )

    # canonical_id is left NULL, resolved only via system_references.
    _insert_transaction(
        store_path, tenant_id, "ruddr", "psa", "time_entry", 3000.0,
        canonical_id=None, counterparty_source_id="ruddr-ext-fallback",
    )

    conn = _connect(store_path)
    try:
        report = get_ar_report(conn, tenant_id)
    finally:
        conn.close()

    row = next(r for r in report if r["canonical_id"] == "CLIENT_FALLBACK")
    assert row["labor_total"] == pytest.approx(3000.0)


# ---------------------------------------------------------------------------
# 11. Pure dashboard classify_rows helper
# ---------------------------------------------------------------------------


def test_classify_rows_is_pure_and_matches_tolerance() -> None:
    rows = [
        {"canonical_id": "C1", "labor_total": 1000.0, "invoiced_total": 1000.0},
        {"canonical_id": "C2", "labor_total": 5000.0, "invoiced_total": 0.0},
        {"canonical_id": "C3", "labor_total": 0.0, "invoiced_total": 5000.0},
    ]
    result1 = ar_recon_page.classify_rows(rows)
    result2 = ar_recon_page.classify_rows(rows)
    assert result1 == result2  # pure, no I/O, deterministic

    by_id = {r["canonical_id"]: r for r in result1}
    assert by_id["C1"]["status"] == "MATCHED"
    assert by_id["C2"]["status"] == "UNBILLED"
    assert by_id["C3"]["status"] == "OVERBILLED"


def test_no_bare_uuid_literal_in_this_test_module() -> None:
    """This module's own no-bare-UUID discipline (mirrors
    `tests/test_connectors_api.py`'s repo-wide guard): every identifier
    here is minted with `uuid.uuid4()`, never typed in."""
    uuid_pattern = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    )
    text = pathlib.Path(__file__).read_text()
    assert not uuid_pattern.search(text)
