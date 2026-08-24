"""Tests for the new `pytest.ini` (feature 10a).

No database required. Exercises marker registration and the
`--strict-markers` collection-error path via subprocess, since both are
properties of the pytest invocation itself, not of importable code.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_COLLECTED_RE = re.compile(r"(\d+) tests? collected")


def test_pytest_ini_exists_and_registers_integration_marker() -> None:
    assert (REPO_ROOT / "pytest.ini").is_file()

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--markers"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert any(
        line.startswith("@pytest.mark.integration") for line in result.stdout.splitlines()
    )


def test_strict_markers_rejects_bogus_marker(tmp_path: pathlib.Path) -> None:
    bogus_test = tmp_path / "test_bogus_marker.py"
    bogus_test.write_text(
        "import pytest\n\n"
        "@pytest.mark.definitely_not_a_registered_marker\n"
        "def test_noop() -> None:\n"
        "    assert True\n"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", "pytest.ini", str(bogus_test), "--collect-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_full_suite_collects_more_than_zero_tests_with_integration_excluded() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-m", "not integration", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    match = _COLLECTED_RE.search(result.stdout)
    assert match is not None, result.stdout
    assert int(match.group(1)) > 0
