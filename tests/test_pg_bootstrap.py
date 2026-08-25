"""Live-Postgres tests for the migration runner and tenant provisioning
(feature 10c). Every test here requires a reachable Postgres.

Runner-execution tests are rollback-exempt by construction: the runner
opens its own connection and commits internally, so a rolled-back
fixture connection cannot see or undo what it does. Those tests open
their own connection with autocommit semantics and restore any planted
precondition in a `finally` block that runs even on failure
("commit-then-clean"). Tenant-provisioning tests that don't touch the
runner use the rollback `pg_conn` fixture from `tests/conftest.py`.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

import psycopg
import scripts.migrate_pg as migrate_pg
from core.graph import pg
from core.graph.tenants import resolve_or_create_tenant, resolve_tenant_for_write

pytestmark = pytest.mark.integration

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MANIFEST_PATH = MIGRATIONS_DIR / "postgres.manifest"


def _require_pg() -> None:
    if not pg.is_available():
        pytest.skip("no Postgres configured: DATABASE_URL is unset")


def _manifest_entries() -> list[str]:
    entries = []
    for line in MANIFEST_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return entries


# ---------------------------------------------------------------------------
# Migration execution (criteria 6-13)
# ---------------------------------------------------------------------------


def test_runner_exits_0_and_operational_tables_exist() -> None:
    _require_pg()
    assert migrate_pg.run(dry_run=False) == 0

    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.approval_decisions')")
            assert cur.fetchone()[0] is not None
            cur.execute("SELECT to_regclass('public.audit_log')")
            assert cur.fetchone()[0] is not None
    finally:
        conn.close()


def test_schema_migrations_equals_manifest_in_order() -> None:
    _require_pg()
    migrate_pg.run(dry_run=False)
    expected = _manifest_entries()

    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations ORDER BY applied_at")
            actual = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    assert actual == expected


def test_sqlite_dialect_migrations_not_applied(capsys) -> None:
    _require_pg()
    migrate_pg.run(dry_run=False)
    captured = capsys.readouterr()

    sqlite_names = {p.name for p in MIGRATIONS_DIR.glob("*_sqlite.sql")}
    assert sqlite_names, "no *_sqlite.sql files on disk — this check would be vacuous"

    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(r"SELECT COUNT(*) FROM schema_migrations WHERE filename LIKE '%\_sqlite%'")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()

    for name in sqlite_names:
        assert name not in captured.out


def test_dry_run_live_exits_0_and_row_count_unchanged() -> None:
    _require_pg()
    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            before = cur.fetchone()[0]
    finally:
        conn.close()

    assert migrate_pg.run(dry_run=True) == 0

    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            after = cur.fetchone()[0]
    finally:
        conn.close()

    assert after == before


def test_runner_idempotent_second_run_changes_nothing(capsys) -> None:
    _require_pg()
    migrate_pg.run(dry_run=False)  # ensure fully applied first

    conn = pg.connect()
    canonical_id = f"CLIENT_RECONCILE_IDEMPOTENCY_{uuid.uuid4().hex[:8]}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO canonical_entities "
                "(canonical_id, tenant_id, canonical_name, entity_type, entity_category) "
                "VALUES (%s, %s, %s, %s, %s)",
                (canonical_id, pg.BOOTSTRAP_TENANT_ID, "seed", "client", "organization"),
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            migrations_before = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM canonical_entities")
            canonical_before = cur.fetchone()[0]

        exit_code = migrate_pg.run(dry_run=False)
        assert exit_code == 0

        captured = capsys.readouterr()
        for entry in _manifest_entries():
            assert f"skipped {entry}" in captured.out
            assert f"applied {entry}" not in captured.out

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            migrations_after = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM canonical_entities")
            canonical_after = cur.fetchone()[0]

        assert migrations_after == migrations_before
        assert canonical_after == canonical_before
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM canonical_entities WHERE canonical_id = %s", (canonical_id,))
        conn.commit()
        conn.close()


def _canonical_schema_migration_filename() -> str:
    for entry in _manifest_entries():
        path = MIGRATIONS_DIR / entry
        if "canonical_entities" in migrate_pg.dropped_tables(path.read_text()):
            return entry
    raise AssertionError("no manifested migration drops canonical_entities")


def test_destructive_guard_fires_live_and_leaves_no_trace(capsys) -> None:
    _require_pg()
    migrate_pg.run(dry_run=False)  # ensure canonical_entities exists

    filename = _canonical_schema_migration_filename()
    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM canonical_entities")
            row_count_before = cur.fetchone()[0]
            # Capture the exact row so cleanup can restore it byte-for-byte
            # (including `applied_at`) rather than just re-inserting with a
            # fresh timestamp, which would corrupt the manifest-order
            # invariant other tests in this module rely on.
            cur.execute(
                "SELECT applied_at FROM schema_migrations WHERE filename = %s", (filename,)
            )
            original_applied_at = cur.fetchone()[0]
            # Plant the precondition: drop the schema_migrations record for
            # the canonical-schema migration while its tables still exist.
            cur.execute("DELETE FROM schema_migrations WHERE filename = %s", (filename,))
        conn.commit()

        exit_code = migrate_pg.run(dry_run=False)
        assert exit_code != 0

        captured = capsys.readouterr()
        assert "refusing to apply" in captured.out
        assert filename in captured.out
        assert not any(f"applied {filename}" in line for line in captured.out.splitlines())

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM canonical_entities")
            row_count_after = cur.fetchone()[0]
        assert row_count_after == row_count_before
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (%s, %s) "
                "ON CONFLICT (filename) DO NOTHING",
                (filename, original_applied_at),
            )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Tenant provisioning (criteria 14-18)
# ---------------------------------------------------------------------------


def test_bootstrap_tenant_seeded_after_runner() -> None:
    _require_pg()
    migrate_pg.run(dry_run=False)

    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id::text FROM tenants WHERE id = %s", (pg.BOOTSTRAP_TENANT_ID,))
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None
    assert isinstance(pg.BOOTSTRAP_TENANT_ID, str)
    assert row[0] == pg.BOOTSTRAP_TENANT_ID


def test_bootstrap_tenant_fk_dependent_insert_succeeds_random_uuid_fails(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (tenant_id, actor_id, action, resource) "
            "VALUES (%s, NULL, %s, %s)",
            (pg.BOOTSTRAP_TENANT_ID, "test-action", "test-resource"),
        )
    # No exception -> the bootstrap tenant satisfies the FK.

    with pg_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO audit_log (tenant_id, actor_id, action, resource) "
                "VALUES (%s, NULL, %s, %s)",
                (str(uuid.uuid4()), "test-action", "test-resource"),
            )
    pg_conn.rollback()  # clear the aborted-transaction state before teardown


def test_resolve_or_create_tenant_creates_and_is_idempotent(pg_conn) -> None:
    fresh_id = str(uuid.uuid4())

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tenants WHERE id = %s", (fresh_id,))
        before = cur.fetchone()[0]

    result1 = resolve_or_create_tenant(pg_conn, fresh_id, name="Test Tenant", slug=f"test-{fresh_id}")
    assert result1 == fresh_id
    assert isinstance(result1, str)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tenants WHERE id = %s", (fresh_id,))
        after_first = cur.fetchone()[0]
    assert after_first == before + 1

    result2 = resolve_or_create_tenant(pg_conn, fresh_id, name="Test Tenant", slug=f"test-{fresh_id}")
    assert result2 == fresh_id

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tenants WHERE id = %s", (fresh_id,))
        after_second = cur.fetchone()[0]
    assert after_second == after_first


def test_slug_collision_raises_loudly_naming_the_slug(pg_conn) -> None:
    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())
    shared_slug = f"collide-{uuid.uuid4()}"

    resolve_or_create_tenant(pg_conn, id_a, name="Tenant A", slug=shared_slug)

    with pytest.raises(psycopg.errors.UniqueViolation) as excinfo:
        resolve_or_create_tenant(pg_conn, id_b, name="Tenant B", slug=shared_slug)

    message = str(excinfo.value)
    assert shared_slug in message
    assert "://" not in message  # no DSN leaked into the error
    pg_conn.rollback()  # clear the aborted-transaction state before teardown


def test_null_tenant_fallback_returns_bootstrap_id(pg_conn) -> None:
    assert resolve_tenant_for_write(pg_conn, None) == pg.BOOTSTRAP_TENANT_ID
