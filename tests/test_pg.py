"""Tests for `core.graph.pg` (feature 10a).

No database required anywhere in this file. `DATABASE_URL` is always
forced absent or set to a sentinel via monkeypatch, and `.env` reloading
is neutralized so a developer's local `.env` cannot leak into a test run.
"""

from __future__ import annotations

import inspect
import re
import uuid

import pytest

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


@pytest.mark.parametrize(
    "sentinel_dsn",
    [
        "SENTINEL-VALUE-DO-NOT-LEAK-42",
        "postgresql://not a valid dsn::::",
        "postgresql://sentinelu:sentinelp@127.0.0.1:1/sentineldb-SENTINEL-VALUE-DO-NOT-LEAK-42",
    ],
)
def test_dsn_sentinel_never_appears_in_failure_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, sentinel_dsn: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", sentinel_dsn)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)

    messages: list[str] = []
    try:
        pg.connect()
    except RuntimeError as exc:
        messages.append(str(exc))

    captured = capsys.readouterr()
    messages.append(captured.out)
    messages.append(captured.err)

    sentinel_marker = "SENTINEL-VALUE-DO-NOT-LEAK-42"
    for message in messages:
        assert sentinel_marker not in message


def test_no_secret_logging_paths_in_pg_module() -> None:
    source = inspect.getsource(pg)
    # No line logs, prints, or formats get_dsn()'s return value or the raw
    # environment value into a message.
    for line in source.splitlines():
        if "print(" in line or "log" in line.lower():
            assert "get_dsn()" not in line
            assert 'environ["DATABASE_URL"]' not in line
            assert 'environ.get("DATABASE_URL")' not in line
