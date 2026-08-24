"""Requirements/env/guard-retirement checks for feature 10a.

No database required. `git diff` here is only ever used to inspect the
committed change to `requirements.txt`, never to open a connection.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_DRIVER_PIN_RE = re.compile(r"^psycopg\[binary\]==")


def _requirements_diff_lines() -> tuple[list[str], list[str]]:
    """Return (added_lines, removed_lines) for requirements.txt vs HEAD."""
    result = subprocess.run(
        ["git", "diff", "HEAD", "--", "requirements.txt"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    added, removed = [], []
    for line in result.stdout.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def test_requirements_diff_adds_only_the_driver_pin() -> None:
    added, removed = _requirements_diff_lines()
    assert added, "expected requirements.txt to gain at least one line"
    assert removed == [], f"expected no removed lines, got: {removed}"
    for line in added:
        assert _DRIVER_PIN_RE.match(line), f"unexpected added line: {line}"


def test_requirements_txt_has_exactly_one_postgres_driver_pin() -> None:
    text = (REPO_ROOT / "requirements.txt").read_text()
    matches = [line for line in text.splitlines() if _DRIVER_PIN_RE.match(line.strip())]
    assert len(matches) == 1
    # No second Postgres driver.
    assert "pg8000" not in text
    assert "asyncpg" not in text


def test_psycopg_importable() -> None:
    import psycopg  # noqa: F401


def test_no_requirements_diff_guard_remains() -> None:
    """Grep tests/ for any test asserting the requirements.txt working-tree
    diff is empty; the result set must be empty."""
    offenders = []
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        if path.name == "test_pg_requirements_and_env.py":
            continue
        text = path.read_text()
        if "requirements.txt" in text and "git" in text and "diff" in text:
            # Only flag tests that assert the diff is *empty*.
            if re.search(r"stdout\.strip\(\)\s*==\s*[\"']{2}", text) or "--quiet" in text:
                offenders.append(path.name)
    assert offenders == []


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


def test_env_example_has_placeholder_database_url() -> None:
    text = (REPO_ROOT / ".env.example").read_text()
    match = re.search(r"^DATABASE_URL=(.*)$", text, re.MULTILINE)
    assert match is not None
    value = match.group(1).strip()
    assert value, "DATABASE_URL= must carry an obviously-fake placeholder value"

    # Does not parse as a usable credential: hostname resolves to a
    # reserved, non-routable / non-resolvable name.
    from urllib.parse import urlparse

    parsed = urlparse(value)
    assert parsed.hostname is not None
    assert parsed.hostname.endswith(".invalid") or "fake" in parsed.hostname.lower()
    assert parsed.username is None or "fake" in parsed.username.lower()
    assert parsed.password is None or "fake" in parsed.password.lower()


def test_env_file_remains_gitignored_and_untracked() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", ".env"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert ignored.returncode == 0

    tracked = subprocess.run(
        ["git", "ls-files", ".env"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert tracked.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Schema parity module still importable / unchanged
# ---------------------------------------------------------------------------


def test_schema_parity_module_imports_and_omits_pending_decisions() -> None:
    import tests.test_schema_parity as parity

    assert "pending_decisions" not in parity.SHARED_TABLES
