"""Tests for `api/middleware/audit.py` (feature 16).

Covers append-only enforcement (grep), the fixed action map (enumerated
per-route via `TestClient` + `AuditQueue.drain()`), the completeness
assertion tying `api.main.app.routes` to the action map, and the
exemption rule (`/health`, unmatched routes).
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

import core.graph.pg as pg
from api.main import app
from api.middleware.audit import ACTION_MAP, EXEMPT_PATHS, audit_queue

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_no_update_or_delete_against_audit_log():
    import re

    src = (REPO_ROOT / "api" / "middleware" / "audit.py").read_text()
    assert not re.search(r"\b(UPDATE|DELETE)\b", src)


ROUTE_CASES = [
    ("GET", "/connectors/", "connector.list", "connectors", None, False),
    ("POST", "/connectors/{provider}/sync", "connector.sync", "connectors", "quickbooks", True),
    ("GET", "/connectors/{provider}/status", "connector.status", "connectors", "quickbooks", True),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    "method,path_template,action,resource,provider,category_non_null", ROUTE_CASES
)
def test_action_map_coverage_per_route(
    pg_conn, method, path_template, action, resource, provider, category_non_null
):
    if provider is not None:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connectors (tenant_id, provider, category, credentials) "
                "VALUES (%s, %s, 'accounting', '{}') ON CONFLICT (tenant_id, provider) DO NOTHING",
                (pg.BOOTSTRAP_TENANT_ID, provider),
            )
        pg_conn.commit()

    concrete_path = path_template.replace("{provider}", provider or "")
    try:
        with TestClient(app) as client:
            if method == "GET":
                resp = client.get(concrete_path)
            else:
                resp = client.post(concrete_path)
            assert resp.status_code < 500

        audit_queue.drain()

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT action, resource, resource_id, category, tenant_id, actor_id "
                "FROM audit_log WHERE action = %s AND tenant_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (action, pg.BOOTSTRAP_TENANT_ID),
            )
            row = cur.fetchone()
            assert row is not None
            row_action, row_resource, row_resource_id, row_category, row_tenant, row_actor = row
            assert row_action == action
            assert row_resource == resource
            assert row_resource_id == provider
            assert str(row_tenant) == pg.BOOTSTRAP_TENANT_ID
            assert row_actor is None
            if category_non_null:
                assert row_category is not None
            else:
                assert row_category is None
    finally:
        if provider is not None:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM connectors WHERE tenant_id = %s AND provider = %s",
                    (pg.BOOTSTRAP_TENANT_ID, provider),
                )
                cur.execute(
                    "DELETE FROM audit_log WHERE resource = 'connectors' AND resource_id = %s",
                    (provider,),
                )
            pg_conn.commit()
        else:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM audit_log WHERE action = %s AND tenant_id = %s "
                    "AND resource_id IS NULL",
                    (action, pg.BOOTSTRAP_TENANT_ID),
                )
            pg_conn.commit()


@pytest.mark.integration
def test_health_and_unmatched_route_produce_zero_rows(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        before = cur.fetchone()[0]

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/no-such-route").status_code == 404

    audit_queue.drain()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        after = cur.fetchone()[0]
    assert after == before


def test_route_action_map_completeness():
    """Every (method, path) pair contributed by `api/routers/*`, minus the
    exemption set, has an entry in `ACTION_MAP` — so a new route lacking
    an audit mapping fails this test rather than silently going unaudited.
    """
    contributed = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        if not path.startswith("/connectors"):
            continue
        for method in methods:
            if method == "HEAD":
                continue
            contributed.add((method, path))

    contributed -= EXEMPT_PATHS
    assert contributed == set(ACTION_MAP.keys())
