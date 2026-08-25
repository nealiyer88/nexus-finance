"""Live-Postgres tests for feature 10c's writers
(`core.graph.audit`, `core.graph.approvals`) and
`scripts/reconcile_stores.py`.
"""

from __future__ import annotations

import pathlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.graph import pg
from core.graph.approvals import record_approval_decision
from core.graph.audit import log_resolution
from core.graph.dispositions import MAPPING
from scripts.reconcile_stores import ReconcileError, reconcile

pytestmark = pytest.mark.integration

# An explicit watermark far enough in the past that it is immune to clock
# skew between the local SQLite process clock and the Postgres server
# clock — the two coverage tests below pass this rather than relying on
# `reconcile`'s default (MIN(audit_log.created_at)), which is sensitive
# to exactly that skew across two independently-clocked engines.
_SAFE_WATERMARK = datetime.now(timezone.utc) - timedelta(days=1)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"


def _require_pg() -> None:
    if not pg.is_available():
        pytest.skip("no Postgres configured: DATABASE_URL is unset")


def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SQLITE_SCHEMA.read_text())
    return conn


def _insert_canonical(conn: sqlite3.Connection, canonical_id: str, tenant_id=None) -> None:
    conn.execute(
        "INSERT INTO canonical_entities "
        "(canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (canonical_id, tenant_id, canonical_id.lower(), "client", "organization", 0.9),
    )


def _insert_alias(conn: sqlite3.Connection, canonical_id: str, value: str) -> None:
    conn.execute(
        "INSERT INTO entity_aliases (canonical_id, value, source, category, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (canonical_id, value, "ruddr", "psa", 0.9),
    )


# ---------------------------------------------------------------------------
# Writers (criteria 19, 20b, 21)
# ---------------------------------------------------------------------------


def test_log_resolution_inserts_one_row_per_call(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM audit_log")
        before = cur.fetchone()[0]

    canonical_id = f"CLIENT_AUDIT_{uuid.uuid4().hex[:8]}"
    log_resolution(
        pg_conn,
        canonical_id=canonical_id,
        incoming_entity_raw="Acme Corp",
        match_type="CONFIRMED",
        confidence=0.91,
        signals={"token_set_ratio": 90.0},
        category_pair="psa:accounting",
        user_id="user_1",
        tenant_id=None,
    )

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM audit_log")
        after = cur.fetchone()[0]
        cur.execute(
            "SELECT category, tenant_id FROM audit_log WHERE resource_id = %s",
            (canonical_id,),
        )
        category, tenant_id = cur.fetchone()

    assert after == before + 1
    assert category == "psa:accounting"
    assert tenant_id is not None


def test_log_resolution_no_update_or_delete_in_code() -> None:
    import re

    src = (REPO_ROOT / "core" / "graph" / "audit.py").read_text()
    assert not re.search(r"\b(UPDATE|DELETE)\b", src)


def test_log_resolution_actor_id_null_and_actor_in_diff(pg_conn) -> None:
    actor = "system:not-a-uuid-actor"
    canonical_id = f"CLIENT_ACTOR_{uuid.uuid4().hex[:8]}"

    log_resolution(
        pg_conn,
        canonical_id=canonical_id,
        incoming_entity_raw="Acme Corp",
        match_type="CONFIRMED",
        confidence=0.9,
        signals={},
        category_pair="psa:accounting",
        user_id=actor,
        tenant_id=None,
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT actor_id, diff FROM audit_log WHERE resource_id = %s",
            (canonical_id,),
        )
        actor_id, diff = cur.fetchone()

    assert actor_id is None
    assert diff.get("actor") == actor


def test_record_approval_decision_covers_every_mapping_key(pg_conn) -> None:
    entity_pair_a = f"CLIENT_A_{uuid.uuid4().hex[:8]}"
    entity_pair_b = f"CLIENT_B_{uuid.uuid4().hex[:8]}"

    for status in MAPPING:
        record_approval_decision(
            pg_conn,
            entity_pair_a=entity_pair_a,
            entity_pair_b=entity_pair_b,
            pending_status=status,
            signal_breakdown={"token_set_ratio": 88.0},
            graph_evidence={},
            category_pair="psa:accounting",
            reasoning_trace="test",
            confidence_at_decision=0.8,
            decided_by="user_1",
            tenant_id=None,
        )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT disposition FROM approval_decisions "
            "WHERE entity_pair_a = %s AND entity_pair_b = %s",
            (entity_pair_a, entity_pair_b),
        )
        rows = {row[0] for row in cur.fetchall()}

    assert rows == set(MAPPING.values())


# ---------------------------------------------------------------------------
# End-to-end: resolve_match writes through the operational store
# ---------------------------------------------------------------------------


def test_resolve_match_writes_audit_and_approval_rows() -> None:
    _require_pg()
    from core.graph.resolution import resolve_match
    from core.ingestion.normalizer import normalize_entity
    from core.matching.types import Disposition, GraphEvidence, ScoredMatch, SignalBreakdown

    sconn = _sqlite_conn()
    canonical_a = f"CLIENT_E2E_A_{uuid.uuid4().hex[:8]}"
    canonical_b = f"CLIENT_E2E_B_{uuid.uuid4().hex[:8]}"
    _insert_canonical(sconn, canonical_a)
    _insert_canonical(sconn, canonical_b)
    sconn.commit()

    entity = normalize_entity(
        {
            "id": "RUDDR-E2E",
            "source": "ruddr",
            "entity_category": "organization",
            "display_name": "E2E Co",
        }
    )
    top = ScoredMatch(
        canonical_id=canonical_a,
        score=0.9,
        signal_breakdown=SignalBreakdown(
            token_sort_ratio=90.0,
            token_set_ratio=90.0,
            partial_ratio=85.0,
            jaro_winkler=0.9,
            ngram_jaccard=0.8,
            alias_boost_fired=False,
            abbreviation_bonus_fired=False,
        ),
        graph_evidence=GraphEvidence(
            shared_person_count=0,
            shared_person_bonus=0.0,
            neighborhood_overlap_count=0,
            neighborhood_overlap_bonus=0.0,
        ),
        category_pair=("psa", "accounting"),
        weight_profile_id="default_v1",
    )
    disp = Disposition(
        source_entity_id="src-e2e",
        action="AUTO_APPROVE",
        top_match=top,
        candidates_ranked=(top,),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )

    pg_conn = pg.connect()
    try:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM audit_log")
            audit_before = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM approval_decisions")
            approval_before = cur.fetchone()[0]
    finally:
        pg_conn.close()

    try:
        resolve_match(
            sconn,
            disp,
            entity,
            canonical_id=canonical_a,
            alias_confidence=0.9,
            source_node=canonical_b,
            target_node=canonical_a,
            relationship="SAME_AS",
            source_category="psa",
            target_category="accounting",
            weight=0.9,
            approved_by="user_e2e",
            tenant_id=None,
        )

        pg_conn = pg.connect()
        try:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM audit_log")
                audit_after = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM approval_decisions")
                approval_after = cur.fetchone()[0]
        finally:
            pg_conn.close()

        assert audit_after == audit_before + 1
        assert approval_after == approval_before + 1
    finally:
        # `resolve_match` writes through its own internal Postgres
        # connection (it is production code, not test-fixture-managed),
        # so the rows it commits here are real and must be explicitly
        # cleaned up — commit-then-clean, same pattern as
        # tests/test_pg_bootstrap.py.
        cleanup_conn = pg.connect()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM audit_log WHERE resource_id = %s", (canonical_a,)
                )
                cur.execute(
                    "DELETE FROM approval_decisions "
                    "WHERE entity_pair_a = %s AND entity_pair_b = %s",
                    (canonical_b, canonical_a),
                )
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


# ---------------------------------------------------------------------------
# reconcile_stores.py (criterion 22)
# ---------------------------------------------------------------------------


def test_reconcile_non_vacuity_gate_raises_on_empty_sqlite_set() -> None:
    _require_pg()
    sconn = _sqlite_conn()
    pconn = pg.connect()
    try:
        with pytest.raises(ReconcileError):
            reconcile(sconn, pconn, pg.BOOTSTRAP_TENANT_ID)
    finally:
        pconn.close()


def test_reconcile_reports_uncovered_identifier() -> None:
    _require_pg()
    sconn = _sqlite_conn()
    canonical_id = f"CLIENT_UNCOVERED_{uuid.uuid4().hex[:8]}"
    _insert_canonical(sconn, canonical_id)
    _insert_alias(sconn, canonical_id, "uncovered co")
    sconn.commit()

    pconn = pg.connect()
    try:
        result = reconcile(sconn, pconn, pg.BOOTSTRAP_TENANT_ID, watermark=_SAFE_WATERMARK)
    finally:
        pconn.close()

    assert canonical_id in result.uncovered


def test_reconcile_reports_informational_only_for_audit_row_with_no_graph_mutation() -> None:
    _require_pg()
    sconn = _sqlite_conn()
    canonical_id = f"CLIENT_COVERED_{uuid.uuid4().hex[:8]}"
    _insert_canonical(sconn, canonical_id)
    _insert_alias(sconn, canonical_id, "covered co")
    sconn.commit()

    ghost_id = f"CLIENT_GHOST_{uuid.uuid4().hex[:8]}"
    pconn = pg.connect()
    try:
        log_resolution(
            pconn,
            canonical_id=canonical_id,
            incoming_entity_raw="Covered Co",
            match_type="CONFIRMED",
            confidence=0.9,
            signals={},
            category_pair="psa:accounting",
            user_id="user_recon",
            tenant_id=None,
        )
        log_resolution(
            pconn,
            canonical_id=ghost_id,
            incoming_entity_raw="Ghost Co",
            match_type="CONFIRMED",
            confidence=0.9,
            signals={},
            category_pair="psa:accounting",
            user_id="user_recon",
            tenant_id=None,
        )
        pconn.commit()

        result = reconcile(sconn, pconn, pg.BOOTSTRAP_TENANT_ID, watermark=_SAFE_WATERMARK)

        assert canonical_id not in result.uncovered
        assert ghost_id in result.informational
    finally:
        # `log_resolution` commits real rows above (commit-then-clean,
        # same pattern as tests/test_pg_bootstrap.py) — remove them
        # regardless of assertion outcome.
        with pconn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_log WHERE resource_id IN (%s, %s)",
                (canonical_id, ghost_id),
            )
        pconn.commit()
        pconn.close()


def test_main_exits_zero_when_sqlite_row_count_exceeds_audit_row_count(tmp_path) -> None:
    # Proves the COVERAGE invariant, not count equality: three SQLite
    # `entity_aliases` rows collapse to a single distinct canonical_id,
    # and a lone `audit_log` row covers it — SQLite row count (3) exceeds
    # `audit_log` row count (1) yet the tenant is healthy (exit 0). A
    # regression to naive `len(sqlite_rows) == len(audit_rows)` equality
    # would fail this case even though coverage genuinely holds.
    _require_pg()
    from scripts.reconcile_stores import main as reconcile_main

    db_path = tmp_path / "graph.sqlite"
    sconn = sqlite3.connect(str(db_path))
    sconn.executescript(SQLITE_SCHEMA.read_text())
    canonical_id = f"CLIENT_HEALTHY_{uuid.uuid4().hex[:8]}"
    _insert_canonical(sconn, canonical_id)
    _insert_alias(sconn, canonical_id, "healthy co")
    _insert_alias(sconn, canonical_id, "healthy co llc")
    _insert_alias(sconn, canonical_id, "healthy-co")
    sconn.commit()
    sconn.close()

    pconn = pg.connect()
    try:
        log_resolution(
            pconn,
            canonical_id=canonical_id,
            incoming_entity_raw="Healthy Co",
            match_type="CONFIRMED",
            confidence=0.9,
            signals={},
            category_pair="psa:accounting",
            user_id="user_healthy",
            tenant_id=None,
        )
        pconn.commit()

        exit_code = reconcile_main(
            [
                "--sqlite-path", str(db_path),
                "--tenant-id", pg.BOOTSTRAP_TENANT_ID,
                "--watermark", _SAFE_WATERMARK.isoformat(),
            ]
        )
        assert exit_code == 0
    finally:
        with pconn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE resource_id = %s", (canonical_id,))
        pconn.commit()
        pconn.close()


def test_main_exits_nonzero_on_uncovered_identifier(tmp_path) -> None:
    _require_pg()
    from scripts.reconcile_stores import main as reconcile_main

    db_path = tmp_path / "graph.sqlite"
    sconn = sqlite3.connect(str(db_path))
    sconn.executescript(SQLITE_SCHEMA.read_text())
    canonical_id = f"CLIENT_MAIN_UNCOVERED_{uuid.uuid4().hex[:8]}"
    _insert_canonical(sconn, canonical_id)
    _insert_alias(sconn, canonical_id, "main uncovered co")
    sconn.commit()
    sconn.close()

    exit_code = reconcile_main(
        [
            "--sqlite-path", str(db_path),
            "--tenant-id", pg.BOOTSTRAP_TENANT_ID,
            "--watermark", _SAFE_WATERMARK.isoformat(),
        ]
    )
    assert exit_code == 1
