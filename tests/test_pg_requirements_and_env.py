"""Requirements/env/guard-retirement checks for feature 10a.

No database required, and no `git diff` between commits: every
requirements assertion below reads the current contents of
`requirements.txt`, so no commit landing after this feature can
invalidate it.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_DRIVER_PIN_RE = re.compile(r"^psycopg\[binary\]==")

# `name`, optional `[extras]`, `==`, `version` — an exact pin, nothing looser.
_EXACT_PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)(?P<extras>\[[A-Za-z0-9,._-]+\])?==(?P<version>[A-Za-z0-9._+!-]+)$"
)


def _requirement_lines() -> list[str]:
    """Every active (non-blank, non-comment) line of requirements.txt."""
    text = (REPO_ROOT / "requirements.txt").read_text()
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def test_requirements_adds_only_the_driver_pin() -> None:
    """requirements.txt carries the Postgres driver, exactly pinned, once —
    and every other entry beside it is itself an exact pin.

    Asserted against the file as it stands rather than against a diff
    between two commits: a diff-based check silently re-breaks the moment
    any later commit lands on top of the feature. No version literal and
    no expected requirement count appear here — both are derived from the
    file at test time.
    """
    lines = _requirement_lines()
    assert lines, "requirements.txt has no active requirement lines"

    parsed = []
    for line in lines:
        match = _EXACT_PIN_RE.match(line)
        assert match is not None, f"requirement is not an exact `name==version` pin: {line}"
        parsed.append(match)

    drivers = [m for m in parsed if m.group("name").lower() == "psycopg"]
    assert len(drivers) == 1, f"expected exactly one psycopg requirement, got: {[m.group(0) for m in drivers]}"

    driver = drivers[0]
    assert driver.group("extras") == "[binary]", (
        f"psycopg must request the binary extra, got: {driver.group(0)}"
    )
    assert _DRIVER_PIN_RE.match(driver.group(0)), f"driver line not pinned: {driver.group(0)}"
    assert driver.group("version"), "psycopg[binary] must carry a version"

    # The driver the feature added is the only Postgres driver present.
    names = {m.group("name").lower() for m in parsed}
    assert names.isdisjoint({"pg8000", "asyncpg", "psycopg2", "psycopg2-binary"}), (
        f"a second Postgres driver crept in: {sorted(names)}"
    )


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
