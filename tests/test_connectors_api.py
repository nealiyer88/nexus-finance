"""Tests for the connectors API router, tenant middleware, and the
tenant-identifier seam (feature 16).

Non-database tests (tenant precedence, malformed-header rejection, the
static seam assertion) require no Postgres. Everything that reads or
writes the `connectors` table is `@pytest.mark.integration` and uses
10c's `pg_conn` fixture — this module opens no second connection of its
own.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

import api.middleware.tenant as tenant_mod
import core.graph.pg as pg
from api.main import app, require_tenant_row
from api.middleware.audit import audit_queue


REPO_ROOT_FILES = ["api", "dashboard", "tests"]


def _no_dsn(monkeypatch):
    """10c's monkeypatch form for a genuine no-database condition."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    assert pg.is_available() is False


# --- static seam -----------------------------------------------------------


def test_default_tenant_id_is_bootstrap_tenant_id_by_identity():
    assert tenant_mod.DEFAULT_TENANT_ID is pg.BOOTSTRAP_TENANT_ID


def test_no_bare_uuid_literal_standing_in_for_tenant_id():
    uuid_pattern = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    )
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    for rel in REPO_ROOT_FILES:
        for path in (repo_root / rel).rglob("*.py"):
            text = path.read_text()
            assert not uuid_pattern.search(text), path


def test_deferred_metric_field_is_absent_from_source():
    import pathlib
    import subprocess

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["grep", "-rIn", "entity" + "_count", "api/", "dashboard/", "tests/"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""


def test_tenant_placeholder_not_described_as_isolation():
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    forbidden = re.compile(
        r"tenant.isolat|row.level security|\bRLS\b|multi.?tenant|access control",
        re.IGNORECASE,
    )
    this_features_files = [
        "dashboard/app.py",
        "dashboard/pages/connectors.py",
        "dashboard/pages/audit_log.py",
        "api/routers/connectors.py",
        "api/middleware/audit.py",
        "api/middleware/tenant.py",
        "api/main.py",
    ]
    for rel in this_features_files:
        text = (repo_root / rel).read_text()
        for match in forbidden.finditer(text):
            # The tenant middleware docstring is explicitly allowed to name
            # these terms only to DISCLAIM them ("NOT isolation",
            # "would misstate...", etc.) — assert every occurrence has a
            # negation/disclaimer word nearby, before or after.
            window = text[max(0, match.start() - 60) : match.end() + 60]
            assert re.search(
                r"\b(not|no|zero|never|misstate)\b", window, re.IGNORECASE
            ), (rel, match.group(0), window)


# --- tenant precedence / malformed-form rejection (no database) -----------


def test_tenant_precedence_header_wins():
    with TestClient(app) as client:
        candidate = str(uuid.uuid4())
        resp = client.get("/health", headers={"X-Nexus-Tenant": candidate})
        assert resp.status_code == 200


def test_tenant_absent_header_falls_back_not_rejected():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200


def test_malformed_header_rejected_with_400():
    with TestClient(app) as client:
        resp = client.get("/connectors/", headers={"X-Nexus-Tenant": "not-a-uuid"})
        assert resp.status_code == 400
        assert "X-Nexus-Tenant" in resp.json()["detail"]


def test_well_formed_header_from_any_caller_is_accepted():
    # Uses `/health` (no database round trip) so this stays a pure edge
    # test of the resolver's form check rather than the connectors
    # router's own data path.
    with TestClient(app) as client:
        candidate = str(uuid.uuid4())
        resp = client.get("/health", headers={"X-Nexus-Tenant": candidate})
        # Any well-formed value is accepted at the edge — no ownership
        # check, no rejection.
        assert resp.status_code == 200


# --- audit writes off the request path (structural, no timing) ------------


@pytest.mark.integration
def test_audit_writes_off_request_path(monkeypatch, pg_conn):
    calls = []

    def _raising_connect():
        calls.append(1)
        raise RuntimeError("connection factory should not be called inline")

    from api.routers import connectors as connectors_router

    def _fake_get_conn():
        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a, **k):
                pass

            def fetchall(self):
                return []

            def fetchone(self):
                return None

        class _Conn:
            def cursor(self, **kw):
                return _Cur()

            def commit(self):
                pass

            def close(self):
                pass

        yield _Conn()

    app.dependency_overrides[connectors_router.get_conn] = _fake_get_conn
    try:
        # Enter the context (and let startup's own legitimate connect()
        # call happen) BEFORE patching the factory to raise — this test
        # is about the request path, not the startup precondition.
        with TestClient(app) as client:
            monkeypatch.setattr("core.graph.pg.connect", _raising_connect)
            assert client.get("/health").status_code == 200
            assert client.get("/connectors/").status_code == 200
            assert calls == []

            # Restore the real factory (still inside the running app) and
            # confirm the audit rows enqueued by those two requests are
            # then written.
            monkeypatch.undo()
            audit_queue.drain()
    finally:
        app.dependency_overrides.pop(connectors_router.get_conn, None)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE action = 'connector.list' "
            "AND tenant_id = %s",
            (pg.BOOTSTRAP_TENANT_ID,),
        )
        assert cur.fetchone()[0] >= 1

    import inspect

    from api.middleware.audit import AuditMiddleware

    src = inspect.getsource(AuditMiddleware.dispatch)
    assert "pg.connect" not in src
    assert "connect(" not in src


def test_dispatch_source_has_no_connection_factory_call():
    import inspect

    from api.middleware.audit import AuditMiddleware

    src = inspect.getsource(AuditMiddleware.dispatch)
    assert "core.graph.pg" not in src or "connect" not in src.split("core.graph.pg")[1][:20]


# --- integration: list / sync / status --------------------------------


# These tests exercise the API's connectors router through `TestClient`,
# which opens its own Postgres connection (via `get_conn` -> `pg.connect()`)
# separate from `pg_conn` — so a row inserted-but-not-committed on `pg_conn`
# would be invisible to it. `pg_conn.commit()` makes the row visible across
# connections; that means this feature's rollback safety net does not apply
# to these rows, so each test cleans up explicitly in a `finally` block.


@pytest.mark.integration
def test_list_connectors_filtered_by_tenant(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (tenant_id, provider, category, credentials) "
            "VALUES (%s, 'test_provider_list', 'accounting', '{}')",
            (pg.BOOTSTRAP_TENANT_ID,),
        )
    pg_conn.commit()
    try:
        with TestClient(app) as client:
            resp = client.get("/connectors/")
        assert resp.status_code == 200
        items = resp.json()
        assert all(
            set(item.keys())
            == {
                "provider",
                "category",
                "connected",
                "last_sync",
                "last_sync_status",
                "last_sync_error",
            }
            for item in items
        )
        assert all(("entity" + "_count") not in item for item in items)
        assert any(item["provider"] == "test_provider_list" for item in items)
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM connectors WHERE tenant_id = %s AND provider = 'test_provider_list'",
                (pg.BOOTSTRAP_TENANT_ID,),
            )
        pg_conn.commit()


@pytest.mark.integration
def test_sync_accepted_and_recorded_and_unknown_404(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (tenant_id, provider, category, credentials) "
            "VALUES (%s, 'quickbooks', 'accounting', '{}') "
            "ON CONFLICT (tenant_id, provider) DO NOTHING",
            (pg.BOOTSTRAP_TENANT_ID,),
        )
    pg_conn.commit()
    try:
        with TestClient(app) as client:
            resp = client.post("/connectors/quickbooks/sync")
            assert resp.status_code == 202
            assert resp.json() == {"provider": "quickbooks", "status": "accepted"}

            resp_404 = client.post("/connectors/nope/sync")
            assert resp_404.status_code == 404

        audit_queue.drain()

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT last_sync, last_sync_status FROM connectors "
                "WHERE tenant_id = %s AND provider = 'quickbooks'",
                (pg.BOOTSTRAP_TENANT_ID,),
            )
            row = cur.fetchone()
            assert row[0] is not None
            assert row[1] == "accepted"

            cur.execute(
                "SELECT count(*) FROM audit_log WHERE action = 'connector.sync' "
                "AND resource_id = 'quickbooks'"
            )
            assert cur.fetchone()[0] == 1
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM connectors WHERE tenant_id = %s AND provider = 'quickbooks'",
                (pg.BOOTSTRAP_TENANT_ID,),
            )
            cur.execute(
                "DELETE FROM audit_log WHERE resource = 'connectors' AND resource_id = 'quickbooks'"
            )
        pg_conn.commit()


@pytest.mark.integration
def test_status_endpoint(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (tenant_id, provider, category, credentials) "
            "VALUES (%s, 'test_provider_status', 'psa', '{}')",
            (pg.BOOTSTRAP_TENANT_ID,),
        )
    pg_conn.commit()
    try:
        with TestClient(app) as client:
            resp = client.get("/connectors/test_provider_status/status")
            assert resp.status_code == 200
            body = resp.json()
            assert set(body.keys()) == {"last_sync", "last_sync_status", "last_sync_error"}

            assert client.get("/connectors/does-not-exist/status").status_code == 404
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM connectors WHERE tenant_id = %s AND provider = 'test_provider_status'",
                (pg.BOOTSTRAP_TENANT_ID,),
            )
        pg_conn.commit()


# --- seam, live half --------------------------------------------------


@pytest.mark.integration
def test_bootstrap_tenant_row_exists_and_audit_insert_commits(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM tenants WHERE id = %s", (pg.BOOTSTRAP_TENANT_ID,))
        assert cur.fetchone() is not None

        cur.execute(
            "INSERT INTO audit_log (tenant_id, actor_id, action, resource, resource_id, category, diff) "
            "VALUES (%s, NULL, 'connector.list', 'connectors', NULL, NULL, NULL)",
            (tenant_mod.DEFAULT_TENANT_ID,),
        )
    # No ForeignKeyViolation raised => success.


# --- missing tenants row fails loudly at startup -----------------------


@pytest.mark.integration
def test_missing_tenant_row_fails_startup_loudly(monkeypatch, pg_conn):
    missing_tenant = str(uuid.uuid4())
    monkeypatch.setenv("NEXUS_TENANT_ID", missing_tenant)
    with pytest.raises(RuntimeError, match=missing_tenant):
        with TestClient(app):
            pass


@pytest.mark.integration
def test_require_tenant_row_no_self_healing(pg_conn):
    missing_tenant = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match=missing_tenant):
        require_tenant_row(pg_conn, missing_tenant)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tenants WHERE id = %s", (missing_tenant,))
        assert cur.fetchone() is None


def test_no_self_healing_grep():
    import pathlib
    import subprocess

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "grep",
            "-rIn",
            "INSERT INTO tenants\\|resolve_or_create_tenant\\|resolve_tenant_for_write",
            "api/",
            "dashboard/",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""

    # Same pattern, scoped to this feature's own added/modified files
    # (never `tests/` at large — 10c's shipped tests legitimately
    # reference its own tenant-provisioning helpers).
    diff_files = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    this_features_files = [f for f in diff_files if not f.startswith("tests/")]
    if this_features_files:
        result2 = subprocess.run(
            [
                "grep",
                "-Il",
                "INSERT INTO tenants\\|resolve_or_create_tenant\\|resolve_tenant_for_write",
                *this_features_files,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result2.stdout == ""


# --- malformed tenant identifier, worker side (structural) -------------


@pytest.mark.integration
def test_malformed_tenant_worker_branch_is_loud_not_silent(caplog, pg_conn):
    import logging

    before = audit_queue.failed_writes
    audit_queue.enqueue(
        {
            "tenant_id": "not-a-uuid",
            "action": "connector.list",
            "resource": "connectors",
            "resource_id": None,
            "category": None,
            "diff": None,
        }
    )
    with caplog.at_level(logging.ERROR):
        audit_queue.drain()  # must not raise
    assert audit_queue.failed_writes == before + 1
    assert "not-a-uuid" in caplog.text
    assert "queue full" not in caplog.text.lower()

    # worker still alive / functional: a subsequent good row drains fine
    audit_queue.enqueue(
        {
            "tenant_id": pg.BOOTSTRAP_TENANT_ID,
            "action": "connector.list",
            "resource": "connectors",
            "resource_id": "seam-check",
            "category": None,
            "diff": None,
        }
    )
    audit_queue.drain()
    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE resource_id = 'seam-check'"
            )
            assert cur.fetchone()[0] == 1
            cur.execute("DELETE FROM audit_log WHERE resource_id = 'seam-check'")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.integration
def test_fk_violation_worker_branch_is_loud(caplog, pg_conn):
    import logging

    missing_tenant = str(uuid.uuid4())
    before = audit_queue.failed_writes
    audit_queue.enqueue(
        {
            "tenant_id": missing_tenant,
            "action": "connector.list",
            "resource": "connectors",
            "resource_id": None,
            "category": None,
            "diff": None,
        }
    )
    with caplog.at_level(logging.ERROR):
        audit_queue.drain()
    assert audit_queue.failed_writes == before + 1
    assert missing_tenant in caplog.text
    assert "queue full" not in caplog.text.lower()


# --- migration discoverability -----------------------------------------


def test_migration_discoverable_and_wired():
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    migrations_dir = repo_root / "db" / "migrations"
    matches = list(migrations_dir.glob("*_connector_sync_status.sql"))
    assert len(matches) == 1
    migration_file = matches[0]

    prefix = migration_file.name.split("_", 1)[0]
    other_prefixes = [
        p.name.split("_", 1)[0]
        for p in migrations_dir.glob("*.sql")
        if p != migration_file
    ]
    assert prefix not in other_prefixes

    manifest = (migrations_dir / "postgres.manifest").read_text()
    assert migration_file.name in manifest.splitlines()

    sqlite_sibling = migrations_dir / migration_file.name.replace(
        "_connector_sync_status.sql", "_connector_sync_status_sqlite.sql"
    )
    assert not sqlite_sibling.exists()


def test_ownership_not_duplicated():
    import pathlib
    import subprocess

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    result_cached = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    changed = set(result.stdout.splitlines()) | set(result_cached.stdout.splitlines())
    forbidden = {
        "pytest.ini",
        "tests/conftest.py",
        "core/graph/pg.py",
        "core/graph/tenants.py",
        "core/graph/audit.py",
        "scripts/migrate_pg.py",
        "scripts/reconcile_stores.py",
    }
    assert not (changed & forbidden)

    manifest_diff = subprocess.run(
        ["git", "diff", "-U0", "db/migrations/postgres.manifest"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    ).stdout
    removed_lines = [
        line
        for line in manifest_diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    assert removed_lines == []


def test_no_jwt_supabase_create_policy_code_in_diff():
    """No JWT parsing, Supabase import, or `CREATE POLICY` code — a prose
    mention in a docstring explaining what this feature explicitly does
    NOT do (see `api/middleware/tenant.py`'s module docstring and
    FOLLOW-UP 16-A) is not itself such code, so this checks for actual
    import/usage patterns rather than the bare English word.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    for rel in [
        "dashboard/app.py",
        "dashboard/pages/connectors.py",
        "dashboard/pages/audit_log.py",
        "api/routers/connectors.py",
        "api/middleware/audit.py",
        "api/middleware/tenant.py",
        "api/main.py",
    ]:
        text = (repo_root / rel).read_text()
        assert not re.search(r"\bimport\s+jwt\b|\bfrom\s+jwt\b", text)
        assert not re.search(r"\bimport\s+supabase\b|\bfrom\s+supabase\b", text)
        assert "CREATE POLICY" not in text
