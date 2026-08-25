"""Tests for `core.graph.pg` (feature 10a).

No database required anywhere in this file. `DATABASE_URL` is always
forced absent or set to a sentinel via monkeypatch, and `.env` reloading
is neutralized so a developer's local `.env` cannot leak into a test run.
"""

from __future__ import annotations

import ast
import inspect
import re
import uuid

import psycopg
import pytest

import scripts.migrate_pg as migrate_pg
from core.graph import pg


def _force_no_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Neutralize the lazy `.env` loader so a developer's real `.env` (which
    # may itself set DATABASE_URL for unrelated local work) cannot leak in.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)


def test_get_dsn_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_no_dsn(monkeypatch)
    assert pg.get_dsn() is None


def test_is_available_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_no_dsn(monkeypatch)
    assert pg.is_available() is False


def test_connect_raises_runtime_error_naming_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_no_dsn(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        pg.connect()
    assert "DATABASE_URL" in str(excinfo.value)


def test_bootstrap_tenant_id_is_a_literal_valid_uuid() -> None:
    # Importable / usable with no database present.
    parsed = uuid.UUID(pg.BOOTSTRAP_TENANT_ID)
    assert str(parsed) == pg.BOOTSTRAP_TENANT_ID.lower()

    source = inspect.getsource(pg)
    assert "uuid4()" not in source


def test_connect_timeout_is_a_named_module_level_constant() -> None:
    source = inspect.getsource(pg)
    assert re.search(r"^[A-Z_]+\s*:\s*int\s*=", source, re.MULTILINE), (
        "expected an UPPER_CASE module-level constant"
    )

    connect_call = re.search(r"psycopg\.connect\([^)]*\)", source, re.DOTALL)
    assert connect_call is not None
    call_text = connect_call.group(0)
    assert "connect_timeout=" in call_text
    # No inline numeric literal at the call site: the timeout kwarg's value
    # must be a name, not a digit.
    timeout_arg = re.search(r"connect_timeout\s*=\s*([^\s,)]+)", call_text)
    assert timeout_arg is not None
    assert not timeout_arg.group(1).strip().isdigit()
    assert timeout_arg.group(1).strip() == "PG_CONNECT_TIMEOUT_SECONDS"


SENTINEL_MARKER = "SENTINEL-VALUE-DO-NOT-LEAK-42"


@pytest.mark.parametrize(
    "sentinel_dsn",
    [
        SENTINEL_MARKER,
        f"postgresql://{SENTINEL_MARKER}::::",
    ],
)
def test_dsn_sentinel_never_appears_in_failure_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, sentinel_dsn: str
) -> None:
    """A driver failure that echoes the DSN must be re-raised scrubbed.

    No socket is opened: the driver boundary (`psycopg.connect`) is replaced
    with a stub that raises the worst-case error — one whose message embeds
    the whole connection string. The DSN itself is an opaque sentinel, so no
    host, port, database name or username appears anywhere here.
    """
    monkeypatch.setenv("DATABASE_URL", sentinel_dsn)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)

    calls: list[str] = []

    def _leaky_driver_failure(dsn: str, **kwargs: object):
        calls.append(dsn)
        raise psycopg.OperationalError(f"connection failed: {dsn}")

    monkeypatch.setattr(psycopg, "connect", _leaky_driver_failure)

    messages: list[str] = []
    with pytest.raises(RuntimeError) as excinfo:
        pg.connect()
    messages.append(str(excinfo.value))

    # The DSN reached the driver unmodified — the module is not mangling it.
    assert calls == [sentinel_dsn]

    captured = capsys.readouterr()
    messages.append(captured.out)
    messages.append(captured.err)

    for message in messages:
        assert SENTINEL_MARKER not in message


_LOGGING_METHOD_NAMES = frozenset(
    {"print", "debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


def _emitting_nodes(tree: ast.AST) -> list[ast.AST]:
    """Every node that emits a message: a print/log call, or a raise."""
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
    """True when the DSN value flows into this node, however it is spelled.

    Works on the parsed tree rather than on single lines, so a multi-line
    f-string or a call split across lines is caught the same as a one-liner.
    A *literal* mention of the string `DATABASE_URL` is fine — the brief
    permits naming the variable, never its value.
    """
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


@pytest.mark.parametrize("module", [pg, migrate_pg], ids=lambda m: m.__name__)
def test_no_secret_logging_paths(module) -> None:
    """Static check: neither module prints, logs, or raises the DSN value.

    Covers `core/graph/pg.py` and `scripts/migrate_pg.py`, and inspects the
    parsed syntax tree so multi-line constructs cannot slip through. Pure
    stdlib `ast` — no runtime dependency and no database.
    """
    tree = ast.parse(inspect.getsource(module))
    nodes = _emitting_nodes(tree)
    assert nodes, f"no print/log/raise sites found in {module.__name__} — check would be vacuous"

    for node in nodes:
        assert not _leaks_dsn(node), (
            f"{module.__name__} emits the DSN value in: {ast.unparse(node)}"
        )
