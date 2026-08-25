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
