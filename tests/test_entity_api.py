"""Tests for the entities API router, the new `core.graph.entity_store`
read helpers, and the Overview / Entity Graph dashboard pages
(feature 14).

Pure SQLite, no database server, no network. `NEXUS_STORE_PATH` points
every test at a per-test temporary file whose schema is built from
`db/schema_sqlite.sql` plus every `*_sqlite.sql` migration (located by
glob, never by number) — the same pattern `tests/test_approvals_api.py`
uses. Never `CREATE TABLE` inside a test.

`TestClient(app)` is used WITHOUT the `with` context manager throughout
this module, so the app's `startup` lifespan hook never runs.

Dashboard pages are pulled from `sys.modules["pages.<name>"]` after
`import dashboard.app` — Dash's page loader imports every module under
`dashboard/pages/` as `pages.<name>`; importing `dashboard.pages.overview`
directly as well would re-trigger `dash.register_page` under a second
module name and collide on path (`tests/test_dashboard_shell.py`'s
convention).
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import uuid
from typing import Any, Dict, List, Optional

import dash
import pytest
from dash import dash_table
from fastapi.testclient import TestClient

import dashboard.app  # noqa: F401  (registers every page's callbacks once)
from api.main import app
from api.middleware.tenant import DEFAULT_TENANT_ID
from core.matching.disposition import AUTO_APPROVE_THRESHOLD

overview_page = sys.modules["pages.overview"]
entity_graph_page = sys.modules["pages.entity_graph"]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
SQLITE_MIGRATIONS = sorted((REPO_ROOT / "db" / "migrations").glob("*_sqlite.sql"))


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


def _insert_canonical(
    path,
    canonical_id: str,
    tenant_id: Optional[str],
    canonical_name: str,
    entity_type: str = "client",
    entity_category: str = "organization",
    confidence: float = 0.95,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO canonical_entities (
                canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_alias(
    path, canonical_id: str, value: str, source: str, category: str, confidence: float = 0.9
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO entity_aliases (canonical_id, value, source, category, confidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (canonical_id, value, source, category, confidence),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_system_reference(
    path,
    canonical_id: str,
    source: str,
    category: str,
    external_id: str,
    external_fields: Optional[Dict[str, Any]] = None,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO system_references (canonical_id, source, category, external_id, external_fields)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                canonical_id,
                source,
                category,
                external_id,
                json.dumps(external_fields) if external_fields else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_edge(
    path,
    source_node: str,
    target_node: str,
    relationship: str,
    source_category: str,
    target_category: str,
    weight: float = 0.9,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO entity_edges (
                source_node, target_node, relationship, source_category, target_category, weight,
                approved_by, approval_count
            ) VALUES (?, ?, ?, ?, ?, ?, 'test', 1)
            """,
            (source_node, target_node, relationship, source_category, target_category, weight),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_pending(path, pending_id: str, tenant_id: Optional[str], status: str = "pending") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO pending_decisions (
                pending_id, tenant_id, decision_key, status, source_entity_id, action,
                top_canonical_id, top_score, category_pair, cluster_conflict,
                abbreviation_rescue, entity_json, disposition_json, proposal_json
            ) VALUES (?, ?, ?, ?, ?, 'QUEUE_FOR_REVIEW', NULL, NULL, 'psa:accounting', 0, 0, '{}', '{}', '{}')
            """,
            (pending_id, tenant_id, f"key-{pending_id}", status, f"src-{pending_id}"),
        )
        conn.commit()
    finally:
        conn.close()


def _collect_ids(component) -> List[Any]:
    ids = []
    comp_id = getattr(component, "id", None)
    if comp_id is not None:
        ids.append(comp_id)
    children = getattr(component, "children", None)
    if isinstance(children, list):
        for child in children:
            ids.extend(_collect_ids(child))
    elif children is not None and hasattr(children, "id"):
        ids.extend(_collect_ids(children))
    return ids


def _flatten(component) -> List[Any]:
    out = [component]
    children = getattr(component, "children", None)
    if isinstance(children, list):
        for child in children:
            out.extend(_flatten(child))
    elif children is not None and hasattr(children, "children"):
        out.extend(_flatten(children))
    return out


# ---------------------------------------------------------------------------
# Overview page — KPI cards
# ---------------------------------------------------------------------------


def test_overview_kpi_cards_match_mapping():
    assert overview_page.KPI_MAPPING  # non-empty, per AC-1
    tree = overview_page.layout()
    all_ids = _collect_ids(tree)
    card_ids_found = {i for i in all_ids if isinstance(i, str) and i in overview_page.KPI_MAPPING}
    assert card_ids_found == set(overview_page.KPI_MAPPING)


# ---------------------------------------------------------------------------
# Entity Graph page — table + search contract
# ---------------------------------------------------------------------------


def test_entity_graph_layout_table_and_search_contract():
    tree = entity_graph_page.layout()
    all_ids = _collect_ids(tree)

    tables = [c for c in _flatten(tree) if isinstance(c, dash_table.DataTable)]
    assert len(tables) == 1
    displayed = {col["id"] for col in tables[0].columns}
    assert displayed == {
        "canonical_id",
        "canonical_name",
        "entity_type",
        "entity_category",
        "confidence",
        "alias_count",
        "source_categories",
    }

    assert "entity-search-input" in all_ids


# ---------------------------------------------------------------------------
# Shipped app — both paths reachable with no database present
# ---------------------------------------------------------------------------


def test_overview_and_entity_graph_paths_return_200_with_no_database(monkeypatch):
    monkeypatch.delenv("NEXUS_STORE_PATH", raising=False)
    flask_client = dashboard.app.app.server.test_client()
    for path in ("/", "/entity-graph"):
        resp = flask_client.get(path)
        assert resp.status_code == 200, path


# ---------------------------------------------------------------------------
# API routes — reachability
# ---------------------------------------------------------------------------


def test_all_entities_routes_reachable(client, store_path):
    _insert_canonical(store_path, "CLIENT_0001", DEFAULT_TENANT_ID, "Acme Corp")

    assert client.get("/entities/stats").status_code == 200
    assert client.get("/entities/").status_code == 200
    assert client.get("/entities/CLIENT_0001").status_code == 200


def test_missing_entity_returns_404(client, store_path):
    resp = client.get("/entities/DOES_NOT_EXIST")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Stats — every field recomputed independently in the test
# ---------------------------------------------------------------------------


def test_stats_endpoint_matches_recomputed_values(client, store_path):
    _insert_canonical(store_path, "CLIENT_0001", DEFAULT_TENANT_ID, "Acme Corp", confidence=0.95)
    _insert_canonical(store_path, "CLIENT_0002", DEFAULT_TENANT_ID, "Beta LLC", confidence=0.60)
    _insert_alias(store_path, "CLIENT_0001", "Acme, LLC", "quickbooks", "accounting")
    _insert_alias(store_path, "CLIENT_0001", "acme-corp", "ruddr", "psa")
    _insert_pending(store_path, "PEND_1", DEFAULT_TENANT_ID, status="pending")
    _insert_pending(store_path, "PEND_2", DEFAULT_TENANT_ID, status="approved")

    resp = client.get("/entities/stats")
    assert resp.status_code == 200
    body = resp.json()

    conn = sqlite3.connect(store_path)
    try:
        resolved = conn.execute(
            "SELECT COUNT(*) FROM canonical_entities WHERE tenant_id = ?", (DEFAULT_TENANT_ID,)
        ).fetchone()[0]
        auto = conn.execute(
            "SELECT COUNT(*) FROM canonical_entities WHERE tenant_id = ? AND confidence >= ?",
            (DEFAULT_TENANT_ID, AUTO_APPROVE_THRESHOLD),
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM pending_decisions WHERE tenant_id = ? AND status = 'pending'",
            (DEFAULT_TENANT_ID,),
        ).fetchone()[0]
        cross_numerator = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT a.canonical_id
                  FROM entity_aliases AS a
                  JOIN canonical_entities AS c ON c.canonical_id = a.canonical_id
                 WHERE c.tenant_id = ?
                 GROUP BY a.canonical_id
                HAVING COUNT(DISTINCT a.category) >= 2
            )
            """,
            (DEFAULT_TENANT_ID,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert body["resolved_entities"] == resolved
    assert body["pending_approvals"] == pending
    assert body["cross_category_coverage"] == pytest.approx(cross_numerator / resolved)
    assert body["auto_match_rate"] == pytest.approx(auto / resolved)


# ---------------------------------------------------------------------------
# Entity detail — alias grouping by entity_aliases.category
# ---------------------------------------------------------------------------


def test_entity_detail_groups_aliases_by_category(client, store_path):
    _insert_canonical(store_path, "CLIENT_0003", DEFAULT_TENANT_ID, "Cenlar FSB")
    _insert_alias(store_path, "CLIENT_0003", "Cenlar, LLC", "quickbooks", "accounting")
    _insert_alias(store_path, "CLIENT_0003", "cenlar-fsb", "ruddr", "psa")
    _insert_alias(store_path, "CLIENT_0003", "CEN", "internal", "internal")
    _insert_system_reference(store_path, "CLIENT_0003", "quickbooks", "accounting", "QB-123")

    resp = client.get("/entities/CLIENT_0003")
    assert resp.status_code == 200
    body = resp.json()

    conn = sqlite3.connect(store_path)
    try:
        category_rows = conn.execute(
            "SELECT DISTINCT category FROM entity_aliases WHERE canonical_id = ?",
            ("CLIENT_0003",),
        ).fetchall()
        expected_categories = {row[0] for row in category_rows}
        expected_by_category = {
            category: sorted(
                v[0]
                for v in conn.execute(
                    "SELECT value FROM entity_aliases WHERE canonical_id = ? AND category = ?",
                    ("CLIENT_0003", category),
                ).fetchall()
            )
            for category in expected_categories
        }
    finally:
        conn.close()

    assert set(body["aliases"].keys()) == expected_categories
    for category, values in expected_by_category.items():
        assert sorted(body["aliases"][category]) == values


# ---------------------------------------------------------------------------
# Fuzzy search
# ---------------------------------------------------------------------------


def test_entity_search_misspelled_hit_and_unrelated_miss(client, store_path):
    _insert_canonical(
        store_path, "CLIENT_0004", DEFAULT_TENANT_ID, "Pacific Rim Technologies International"
    )
    _insert_canonical(store_path, "CLIENT_0005", DEFAULT_TENANT_ID, "Northwind Traders")

    resp_hit = client.get("/entities/", params={"q": "Pacifc Rim Technolgies"})
    assert resp_hit.status_code == 200
    hit_ids = {item["canonical_id"] for item in resp_hit.json()["items"]}
    assert "CLIENT_0004" in hit_ids

    resp_miss = client.get("/entities/", params={"q": "zzqx vwkj plmr"})
    assert resp_miss.status_code == 200
    assert resp_miss.json()["items"] == []


# ---------------------------------------------------------------------------
# Tenant scoping — disjointness invariant
# ---------------------------------------------------------------------------


def test_tenant_scoped_reads_are_disjoint(client, store_path):
    tenant_a = DEFAULT_TENANT_ID
    tenant_b = str(uuid.uuid4())
    _insert_canonical(store_path, "CLIENT_A1", tenant_a, "Tenant A Co")
    _insert_canonical(store_path, "CLIENT_B1", tenant_b, "Tenant B Co")

    resp_a = client.get("/entities/", headers={"X-Nexus-Tenant": tenant_a})
    resp_b = client.get("/entities/", headers={"X-Nexus-Tenant": tenant_b})
    ids_a = {item["canonical_id"] for item in resp_a.json()["items"]}
    ids_b = {item["canonical_id"] for item in resp_b.json()["items"]}

    assert ids_a, "tenant A result set must be non-empty before the disjointness check"
    assert ids_b, "tenant B result set must be non-empty before the disjointness check"
    assert ids_a.isdisjoint(ids_b)


# ---------------------------------------------------------------------------
# Entity table row shaping — pure helper
# ---------------------------------------------------------------------------


def test_row_for_table_joins_source_categories():
    row = entity_graph_page.row_for_table(
        {
            "canonical_id": "CLIENT_0001",
            "canonical_name": "Acme Corp",
            "entity_type": "client",
            "entity_category": "organization",
            "confidence": 0.95,
            "alias_count": 2,
            "source_categories": ["accounting", "psa"],
        }
    )
    assert row["source_categories"] == "accounting, psa"
