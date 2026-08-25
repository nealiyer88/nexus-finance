"""No-database unit tests for feature 10c: the suppression proof, the
`resolve_or_create_tenant` signature/schema check, the disposition subset
check, secret-hygiene AST checks, and the `actor_id` NULL-binding grep.

Nothing here opens a Postgres connection; the Stage 6 suppression test
uses an in-memory SQLite database loaded from the schema files.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import sqlite3

import pytest

from core.graph import pg
from core.graph.dispositions import MAPPING
from core.graph.resolution import resolve_match
from core.graph.tenants import resolve_or_create_tenant, resolve_tenant_for_write
from core.ingestion.normalizer import normalize_entity
from core.matching.types import Disposition, GraphEvidence, ScoredMatch, SignalBreakdown

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
TRAINING_MIGRATION = REPO_ROOT / "db" / "migrations" / "002_llm_training_data_sqlite.sql"

_WRITER_MODULES = (
    REPO_ROOT / "core" / "graph" / "audit.py",
    REPO_ROOT / "core" / "graph" / "approvals.py",
    REPO_ROOT / "core" / "graph" / "tenants.py",
    REPO_ROOT / "scripts" / "reconcile_stores.py",
)


# ---------------------------------------------------------------------------
# Suppression proof (criterion 3)
# ---------------------------------------------------------------------------


def test_dotenv_suppressed_is_available_returns_false(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)

    assert pg.is_available() is False


@pytest.fixture()
def sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SQLITE_SCHEMA.read_text())
    conn.executescript(TRAINING_MIGRATION.read_text())
    try:
        yield conn
    finally:
        conn.close()


def _scored_match(canonical_id: str) -> ScoredMatch:
    return ScoredMatch(
        canonical_id=canonical_id,
        score=0.60,
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


def test_dotenv_suppressed_stage6_runs_and_performs_no_operational_write(
    monkeypatch, sqlite_conn: sqlite3.Connection
) -> None:
    """Stage 6 is actually driven here — the no-write assertion has teeth.

    Under suppression `pg_is_available()` is False, so `resolve_match`
    must never reach `pg_connect()`. The connect helper *resolution.py*
    actually calls is patched to record and fail; the SQLite side effects
    are asserted afterwards to prove the Stage 6 body really executed
    rather than the assertion passing because nothing ran.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    assert pg.is_available() is False

    calls: list[str] = []

    def _recording_connect(*args, **kwargs):
        calls.append("connect")
        raise AssertionError("Stage 6 must not open an operational-store connection")

    monkeypatch.setattr("core.graph.resolution.pg_connect", _recording_connect)

    for canonical_id, name in (("CLIENT_A", "pacrim technologies"), ("CLIENT_B", "pacrim other")):
        sqlite_conn.execute(
            "INSERT INTO canonical_entities (canonical_id, tenant_id, canonical_name, "
            "entity_type, entity_category, confidence) VALUES (?, ?, ?, ?, ?, ?)",
            (canonical_id, None, name, "client", "organization", 0.95),
        )
    sqlite_conn.commit()

    entity = normalize_entity(
        {"id": "RUDDR-X", "source": "ruddr", "entity_category": "organization", "display_name": "PacRim Tech"}
    )
    top = _scored_match("CLIENT_A")
    disposition = Disposition(
        source_entity_id="src-1",
        action="LLM_FALLBACK",
        top_match=top,
        candidates_ranked=(top,),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )

    out_cid = resolve_match(
        sqlite_conn,
        disposition,
        entity,
        canonical_id="CLIENT_A",
        alias_confidence=0.85,
        source_node="CLIENT_B",
        target_node="CLIENT_A",
        relationship="SAME_AS",
        source_category="psa",
        target_category="accounting",
        weight=0.85,
        approved_by="user_1",
        tenant_id=None,
    )

    assert out_cid == "CLIENT_A"
    # Stage 6 genuinely ran: the local graph write landed.
    alias_rows = sqlite_conn.execute(
        "SELECT 1 FROM entity_aliases WHERE canonical_id = ? AND value = ?",
        ("CLIENT_A", entity.normalized_name),
    ).fetchall()
    assert len(alias_rows) == 1
    # ...and it did so without touching the operational store.
    assert calls == []


# ---------------------------------------------------------------------------
# resolve_or_create_tenant signature + schema check (criterion 17)
# ---------------------------------------------------------------------------


def test_resolve_or_create_tenant_has_no_defaults_for_name_or_slug() -> None:
    sig = inspect.signature(resolve_or_create_tenant)
    assert sig.parameters["name"].default is inspect.Parameter.empty
    assert sig.parameters["slug"].default is inspect.Parameter.empty


class _ExplodingConn:
    """Any use of the connection is a failure — these two paths must
    decide before they touch the operational store."""

    def cursor(self, *args, **kwargs):
        raise AssertionError("resolve_tenant_for_write must not query for these inputs")


def test_resolve_tenant_for_write_raises_on_present_non_uuid_tenant() -> None:
    """A malformed-but-PRESENT tenant id must never degrade to bootstrap.

    Degrading would file this caller's rows under the bootstrap tenant —
    a silent cross-tenant attribution error in the audit trail.
    """
    with pytest.raises(ValueError) as excinfo:
        resolve_tenant_for_write(_ExplodingConn(), "not-a-uuid-tenant")

    message = str(excinfo.value)
    assert "not-a-uuid-tenant" in message
    assert "UUID" in message
    # The error names the bad value and nothing about the connection.
    for forbidden in ("postgres", "password", "DATABASE_URL", "@", "://"):
        assert forbidden not in message
    # And it must not have quietly produced the bootstrap tenant instead.
    assert pg.BOOTSTRAP_TENANT_ID not in message


def test_resolve_tenant_for_write_none_still_falls_back_to_bootstrap() -> None:
    """The sanctioned fallback: feature 12 legitimately passes None."""
    assert resolve_tenant_for_write(_ExplodingConn(), None) == pg.BOOTSTRAP_TENANT_ID


def test_tenants_name_and_slug_columns_not_null_and_slug_unique() -> None:
    schema = SCHEMA_SQL.read_text()
    match = re.search(r"CREATE TABLE IF NOT EXISTS tenants\s*\((.*?)\n\);", schema, re.DOTALL)
    assert match is not None, "tenants table not found in db/schema.sql"
    body = match.group(1)
    # DDL uses multi-space alignment runs between column name and type.
    assert re.search(r"\bname\s+TEXT\s+NOT\s+NULL\b", body)
    assert re.search(r"\bslug\s+TEXT\s+NOT\s+NULL\s+UNIQUE\b", body)


# ---------------------------------------------------------------------------
# Disposition subset check (criterion 21, no-database half)
# ---------------------------------------------------------------------------


def _derive_check_set() -> set[str]:
    text = SCHEMA_SQL.read_text()
    match = re.search(
        r"disposition\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*disposition\s+IN\s*\((?P<values>[^)]*)\)\s*\)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None
    return {v.strip().strip("'") for v in match.group("values").split(",") if v.strip()}


def _derive_terminal_set() -> set[str]:
    for path in sorted((REPO_ROOT / "db" / "migrations").glob("*_sqlite.sql")):
        text = path.read_text()
        if "pending_decisions" not in text:
            continue
        match = re.search(
            r"status\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'(?P<default>[^']+)'"
            r"\s*CHECK\s*\(\s*status\s+IN\s*\((?P<values>[^)]*)\)\s*\)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            values = {v.strip().strip("'") for v in match.group("values").split(",") if v.strip()}
            return values - {match.group("default")}
    raise AssertionError("no db/migrations/*_sqlite.sql defines pending_decisions.status")


def test_mapping_values_and_keys_are_subsets() -> None:
    check_set = _derive_check_set()
    terminal_set = _derive_terminal_set()
    assert set(MAPPING.values()) <= check_set
    assert set(MAPPING.keys()) <= terminal_set


# ---------------------------------------------------------------------------
# actor_id NULL-binding grep (criterion 20a)
# ---------------------------------------------------------------------------


def test_actor_id_never_bound_to_a_non_null_value() -> None:
    for path in (REPO_ROOT / "core" / "graph" / "audit.py", REPO_ROOT / "core" / "graph" / "approvals.py"):
        tree = ast.parse(path.read_text())
        sql_literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "actor_id" in node.value
        ]
        if path.name == "approvals.py":
            # approvals.py has no actor_id column at all — this loop body
            # simply never runs for it, which is itself the assertion.
            assert sql_literals == []
            continue
        assert sql_literals, f"expected at least one actor_id-mentioning SQL literal in {path}"
        for sql in sql_literals:
            # The column appears in the INSERT column list, and the VALUES
            # clause binds it as a bare SQL NULL — never a %s placeholder.
            assert re.search(r"\bNULL\b", sql), f"{path}: actor_id SQL has no literal NULL: {sql!r}"
            assert not re.search(r"actor_id\s*,\s*%s", sql, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Secret hygiene (criteria 23, 24, 25)
# ---------------------------------------------------------------------------

_LOGGING_METHOD_NAMES = frozenset(
    {"print", "debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


def _emitting_nodes(tree: ast.AST) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            nodes.append(node)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _LOGGING_METHOD_NAMES:
                nodes.append(node)
    return nodes


def _leaks_dsn(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "dsn":
            return True
        if isinstance(child, ast.Call):
            func = child.func
            if getattr(func, "id", "") == "get_dsn" or getattr(func, "attr", "") == "get_dsn":
                return True
        if isinstance(child, ast.Subscript) and "environ" in ast.dump(child.value):
            if "DATABASE_URL" in ast.dump(child.slice):
                return True
        if isinstance(child, ast.Call) and getattr(child.func, "attr", "") == "get":
            if "environ" in ast.dump(child.func) and "DATABASE_URL" in ast.dump(child):
                return True
    return False


# Textual counterpart of `_emitting_nodes`, derived independently of the
# AST walk: a `raise`, a bare `print(`, or a `<something>.<log method>(`
# call. Used only to prove the AST collection is not silently empty.
_TEXTUAL_EMITTER_RE = re.compile(
    r"(^|\s)raise\s|(^|[\s.(])print\s*\(|\.(?:" + "|".join(sorted(_LOGGING_METHOD_NAMES)) + r")\s*\(",
    re.MULTILINE,
)


@pytest.mark.parametrize("path", _WRITER_MODULES, ids=lambda p: p.name)
def test_no_secret_logging_paths_in_10c_modules(path: pathlib.Path) -> None:
    """No 10c module emits the DSN on any raise/log path.

    Two of the four modules (`audit.py`, `approvals.py`) legitimately have
    no emitting nodes at all, so looping over their (empty) collection
    would assert nothing. Rather than encoding which modules those are,
    the AST collection is cross-checked against an independent textual
    scan: a module whose source contains an emitter must yield nodes (or
    the walker is broken), and a module with none must yield an
    explicitly-empty collection. Either way the parametrization can fail.
    """
    source = path.read_text()
    tree = ast.parse(source)
    nodes = _emitting_nodes(tree)
    has_textual_emitter = _TEXTUAL_EMITTER_RE.search(source) is not None

    if not has_textual_emitter:
        # The empty collection is itself the assertion for this module.
        assert nodes == [], f"{path}: AST found emitters the textual scan did not"
        return

    assert nodes, f"{path}: source contains raise/log statements but the AST walk found none"
    for node in nodes:
        assert not _leaks_dsn(node), f"{path} emits the DSN value in: {ast.unparse(node)}"


def test_dsn_sentinel_never_appears_across_10c_failure_paths(monkeypatch, capsys) -> None:
    sentinel = "SENTINEL-VALUE-DO-NOT-LEAK-10C"
    monkeypatch.setenv("DATABASE_URL", sentinel)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)

    messages = []

    # Every failure path reachable with no database: connect() raising.
    # Each call MUST raise — a silently-succeeding call would leave the
    # assertions below scanning near-empty strings and passing vacuously.
    connect_raised = False
    try:
        pg.connect()
    except Exception as exc:  # noqa: BLE001
        connect_raised = True
        messages.append(str(exc))
    assert connect_raised, "pg.connect() did not raise with no reachable database"

    from scripts import reconcile_stores

    reconcile_raised = False
    try:
        reconcile_stores.main(["--sqlite-path", ":memory:", "--tenant-id", "not-a-real-tenant"])
    except Exception as exc:  # noqa: BLE001
        reconcile_raised = True
        messages.append(str(exc))
    assert reconcile_raised, "reconcile_stores.main() did not raise with no reachable database"

    captured = capsys.readouterr()
    messages.append(captured.out)
    messages.append(captured.err)

    for message in messages:
        assert sentinel not in message
