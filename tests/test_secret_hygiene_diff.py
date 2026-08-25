"""Secret-hygiene proof for feature 10c: the live DSN is not in the diff.

The two criteria this file covers ("the secret is not in the diff" and the
added-line `DATABASE_URL` assignment rule) had no automated test anywhere in
the feature. Everything here is derived at run time — no hostname, port,
database name, username, commit SHA, line number, or expected count is
written down.

What is covered, and what is deliberately not:

* **Covered components: the hostname, the database name, and the password
  when one is present.** A component the configured value does not carry is
  skipped, and its absence is never an error — the local peer-auth DSN has
  an empty password. No production code path branches on that; the skip
  happens here, in the test's own value derivation.
* **The URL scheme and the port are excluded.** Neither carries a secret and
  both appear in every valid connection string, including the sentinel DSNs
  this feature's own tests construct — a check covering them would forbid
  its own solution.
* **The DSN's user component is excluded.** In this environment it is
  byte-identical to the operating-system account name, hence a path
  component of the repository root; it already occurs in committed material
  and in pytest's own rootdir header, so covering it would go red on a
  flawless implementation.

The resolved value is obtained from feature 10a's getter, held only in
memory, and written nowhere.

Scope of "under `features/` and `.rocket/`": the files git would carry —
tracked, plus untracked-and-not-ignored. Agent/run scratch directories under
those paths are gitignored tool output, not material this feature writes or
commits.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import uuid
from urllib.parse import urlparse

import pytest

from core.graph import pg

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Directories the criteria name. Both are searched via git so that ignored
# tool scratch output is out of scope and nothing else is.
SECRET_FREE_DIRS = ("features", ".rocket")

# This feature's build commit, by the repository's own commit convention.
# The base is that commit's parent, resolved at run time — no SHA here.
_BUILD_COMMIT_RE = re.compile(r"^feat(\([^)]*\))?:", re.IGNORECASE)

# Shell-style assignment only: `DATABASE_URL=` with no space around `=`.
_DATABASE_URL_ASSIGNMENT_RE = re.compile(r"DATABASE_URL=(?P<value>\S*)")

# Markdown/quoting punctuation that can wrap a value in prose, and the
# placeholder shapes briefs use in place of a real one.
_WRAPPING_CHARS = "`'\"*_,;)]}"
_PLACEHOLDER_RE = re.compile(r"^(<.*|\$\{?.*|\{\{.*|%.*%|\.\.\.)$")


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout


def _read_text(path: pathlib.Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _feature_base_ref() -> str:
    """The commit this feature's changes are measured against."""
    for entry in _git("log", "--format=%H%x1f%s").splitlines():
        sha, _, subject = entry.partition("\x1f")
        if _BUILD_COMMIT_RE.match(subject.strip()):
            return f"{sha}^"
    raise AssertionError("no build commit found to anchor this feature's diff")


def _untracked_paths(*pathspec: str) -> list[pathlib.Path]:
    """Untracked, not-ignored files — new files are part of this diff too."""
    out = _git("ls-files", "--others", "--exclude-standard", "--", *pathspec)
    return [REPO_ROOT / line for line in out.splitlines() if line]


def _feature_diff_text(*pathspec: str) -> str:
    """Committed feature diff + working-tree changes + new untracked files."""
    base = _feature_base_ref()
    parts = [
        _git("diff", f"{base}..HEAD", "--", *pathspec) if pathspec else _git("diff", f"{base}..HEAD"),
        _git("diff", "HEAD", "--", *pathspec) if pathspec else _git("diff", "HEAD"),
    ]
    for path in _untracked_paths(*(pathspec or (".",))):
        parts.append(_read_text(path))
    return "\n".join(parts)


def _added_lines(*pathspec: str) -> list[str]:
    """Lines this feature ADDS under the given pathspec — added lines only."""
    base = _feature_base_ref()
    lines: list[str] = []
    for diff in (_git("diff", f"{base}..HEAD", "--", *pathspec), _git("diff", "HEAD", "--", *pathspec)):
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])
    for path in _untracked_paths(*pathspec):
        lines.extend(_read_text(path).splitlines())
    return lines


def _committable_files(*pathspec: str) -> list[pathlib.Path]:
    out = _git("ls-files", "--cached", "--others", "--exclude-standard", "--", *pathspec)
    paths = [REPO_ROOT / line for line in out.splitlines() if line]
    return [p for p in paths if p.is_file()]


def _resolved_dsn() -> str:
    """The configured DSN, or a sentinel when none is configured.

    The sentinel keeps the absence assertions running rather than turning
    "no DSN configured" into a silent always-pass: its components are
    freshly generated, so they are absent from the tree by construction and
    a broken search would still be caught by the detector test below.
    """
    dsn = pg.get_dsn()
    if dsn:
        return dsn
    return f"postgresql://{uuid.uuid4().hex}@{uuid.uuid4().hex}:0/{uuid.uuid4().hex}"


def _covered_values(dsn: str) -> dict[str, str]:
    """The whole value plus exactly the covered components it carries."""
    parsed = urlparse(dsn)
    candidates = {
        "connection string": dsn,
        "hostname": parsed.hostname,
        "database name": (parsed.path or "").lstrip("/"),
        "password": parsed.password,
    }
    # An absent or empty component is skipped, never an error.
    return {name: value for name, value in candidates.items() if value}


def _occurrences(values: dict[str, str], corpus: str) -> list[str]:
    return sorted(name for name, value in values.items() if value in corpus)


def _assigned_database_url_value(line: str) -> str | None:
    """The value an added line assigns to DATABASE_URL, else None.

    An empty assignment (the dotenv-suppressing invocation form) and a
    placeholder (`<from the configured dotenv>`, `${...}`) assign no value.
    """
    match = _DATABASE_URL_ASSIGNMENT_RE.search(line)
    if match is None:
        return None
    value = match.group("value").strip().strip(_WRAPPING_CHARS)
    if not value or _PLACEHOLDER_RE.match(value):
        return None
    return value


# ---------------------------------------------------------------------------
# The covered set is what the criterion says it is
# ---------------------------------------------------------------------------


def test_covered_set_is_the_whole_value_plus_only_the_named_components() -> None:
    dsn = _resolved_dsn()
    values = _covered_values(dsn)

    assert values.get("connection string") == dsn, "the whole value must always be covered"
    assert set(values) <= {"connection string", "hostname", "database name", "password"}

    parsed = urlparse(dsn)
    excluded = {parsed.scheme, str(parsed.port or ""), parsed.username or ""}
    excluded.discard("")
    assert excluded, "the DSN carries none of the deliberately-excluded parts"
    # Scheme, port and user are excluded on purpose: they carry no secret and
    # appear in every valid DSN, including this feature's own sentinels.
    assert excluded.isdisjoint(set(values.values()) - {dsn})

    # Positive control on the real resolved values, in memory only: every
    # covered value is a searchable non-empty string, so an empty component
    # can never make the absence checks below pass by accident.
    assert _occurrences(values, "\n".join(values.values())) == sorted(values)


# ---------------------------------------------------------------------------
# Criterion: the secret is not in this feature's diff
# ---------------------------------------------------------------------------


def test_dsn_and_covered_components_absent_from_this_features_diff() -> None:
    diff = _feature_diff_text()
    assert diff.strip(), "no feature diff was resolved — the check would be vacuous"

    values = _covered_values(_resolved_dsn())
    leaked = _occurrences(values, diff)
    # Names only — the values themselves are never put in a message.
    assert leaked == [], f"DSN components present in this feature's diff: {leaked}"


# ---------------------------------------------------------------------------
# Criterion: the secret is in no file under features/ or .rocket/
# ---------------------------------------------------------------------------


def test_dsn_and_covered_components_absent_from_features_and_rocket() -> None:
    files = _committable_files(*SECRET_FREE_DIRS)
    assert files, "no files resolved under the secret-free directories"

    values = _covered_values(_resolved_dsn())
    offenders: list[str] = []
    for path in files:
        found = _occurrences(values, _read_text(path))
        if found:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {found}")
    assert offenders == []


# ---------------------------------------------------------------------------
# Criterion: no ADDED line under features/ or .rocket/ assigns DATABASE_URL
# ---------------------------------------------------------------------------


def test_no_added_line_under_features_or_rocket_assigns_database_url() -> None:
    lines = _added_lines(*SECRET_FREE_DIRS)
    assert lines, "no added lines resolved — the check would be vacuous"

    offenders = [line.strip() for line in lines if _assigned_database_url_value(line) is not None]
    assert offenders == []


# ---------------------------------------------------------------------------
# The detector itself can fail (non-vacuity), proven outside the repository
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("component", ["connection string", "hostname", "database name", "password"])
def test_detector_flags_a_leaked_component(component: str, tmp_path: pathlib.Path) -> None:
    """A fabricated DSN planted outside the repo is caught, per component.

    Nothing real is written anywhere: the DSN below is generated for this
    test alone, and `tmp_path` is outside the repository.
    """
    fake = f"postgresql://{uuid.uuid4().hex}:{uuid.uuid4().hex}@{uuid.uuid4().hex}:0/{uuid.uuid4().hex}"
    values = _covered_values(fake)
    assert component in values, "the fabricated DSN must carry every covered component"

    planted = tmp_path / "leaked.md"
    planted.write_text(f"see {values[component]} for details\n")
    assert _occurrences(values, _read_text(planted)) != []


def test_detector_flags_an_added_database_url_assignment() -> None:
    """The assignment rule fires on a real value and stays quiet otherwise."""
    fake = f"postgresql://{uuid.uuid4().hex}@{uuid.uuid4().hex}:0/{uuid.uuid4().hex}"
    assert _assigned_database_url_value(f"DATABASE_URL={fake}") == fake
    assert _assigned_database_url_value(f"export DATABASE_URL='{fake}'") == fake

    # Forms that assign nothing: the dotenv-suppressing invocation, a
    # markdown code span, and a placeholder.
    assert _assigned_database_url_value("DATABASE_URL= .venv/bin/python -m pytest") is None
    assert _assigned_database_url_value("set `DATABASE_URL=` to empty") is None
    assert _assigned_database_url_value("DATABASE_URL=<from the configured dotenv> pytest") is None
    assert _assigned_database_url_value("nothing to see here") is None
