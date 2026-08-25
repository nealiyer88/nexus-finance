"""Shared fixtures for the integration test tier (feature 10c).

`pg_conn` is the default fixture for writer, tenant-provisioning, and
reconciliation tests: it skips with an explicit reason (naming the
`DATABASE_URL` variable, never its value) when no Postgres is configured,
and otherwise yields a connection wrapped in a transaction that is rolled
back at teardown so tests leave no residue.

Runner-execution tests are rollback-exempt by construction (see
`tests/test_pg_bootstrap.py`) and do not use this fixture — they open
their own connection with autocommit semantics and clean up explicitly.
"""

from __future__ import annotations

import pytest

from core.graph import pg


@pytest.fixture()
def pg_conn():
    if not pg.is_available():
        pytest.skip("no Postgres configured: DATABASE_URL is unset")

    conn = pg.connect()
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture(autouse=True)
def _suppress_live_postgres_writes(request, monkeypatch):
    """Non-integration tests never reach live Postgres as a side effect.

    Stage 6's call site in `core.graph.resolution` gates on
    `pg_is_available()`. In any environment where `DATABASE_URL` is
    configured (this repo's documented `.env` setup), that gate is
    unconditionally True, so a plain unit test exercising
    `resolve_match`/`engine.match()` against an in-memory SQLite fixture
    would otherwise fire real, non-idempotent inserts into a live
    `audit_log`/`approval_decisions`. Force the gate closed for every
    test not explicitly marked `integration` — those tests use the
    `pg_conn` fixture (rollback-wrapped) or the rollback-exempt
    runner-execution pattern in `test_pg_bootstrap.py`, and opt in to
    live Postgres on purpose via `pytestmark`.
    """
    if "integration" not in request.node.keywords:
        monkeypatch.setattr("core.graph.resolution.pg_is_available", lambda: False)
