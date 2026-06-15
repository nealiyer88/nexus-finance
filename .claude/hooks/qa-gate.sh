#!/usr/bin/env bash
# qa-gate.sh — Stop hook for the Rocket build loop.
#
# Runs the project test suite before Claude is allowed to end its turn.
# - If tests pass: exit 0 with no stdout (Claude stops normally).
# - If tests fail: emit a "block" JSON payload on stdout (exit 0) so the
#   harness feeds the reason back to Claude and forces continuation.
#
# Regression awareness:
#   When .claude/hooks/test-baseline.txt exists (written by record-
#   baseline.sh before the build), failing tests are partitioned:
#     - REGRESSION: failing test IS in the baseline (was passing before)
#     - NEW FAILURE: failing test is NOT in the baseline (added by build)
#   Regressions get a stricter "revert what caused it" message because
#   the fixer should never pile more code onto a regression.
#
# Edge cases — all allow stop, never block on missing infra:
#   - tests/ absent
#   - pytest unavailable
#   - python3 unavailable (cannot safely emit JSON)

set -uo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_DIR" 2>/dev/null || exit 0

[ -d tests ] || exit 0

# -q (no -x) so we see ALL failures, not just the first — required for
# accurate regression vs. new-failure partitioning.
if command -v pytest >/dev/null 2>&1; then
  PYTEST_CMD=(pytest tests/ --tb=short -q)
elif command -v python3 >/dev/null 2>&1 && python3 -m pytest --version >/dev/null 2>&1; then
  PYTEST_CMD=(python3 -m pytest tests/ --tb=short -q)
else
  exit 0
fi

output="$("${PYTEST_CMD[@]}" 2>&1)"
rc=$?

if [ "$rc" -eq 0 ]; then
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

BASELINE=".claude/hooks/test-baseline.txt"

PYTEST_OUTPUT="$output" BASELINE_PATH="$BASELINE" python3 - <<'PY'
import json, os, re, sys

output = os.environ.get("PYTEST_OUTPUT", "")
baseline_path = os.environ.get("BASELINE_PATH", "")

# pytest -q failure lines:
#   FAILED tests/test_foo.py::test_one - AssertionError: ...
#   FAILED tests/test_bar.py::test_param[arg with spaces, commas]
#   FAILED tests/test_bar.py::TestX::test_y
# Collection errors:
#   ERROR tests/test_x.py - ImportError: ...
# Node ids may contain spaces (parametrize), so we split on the first
# " - " separator rather than using \S+ which would truncate at any space.
prefix_re = re.compile(r'^(?:FAILED|ERROR)\s+(.+)$')
failing_set = set()
for line in output.splitlines():
    m = prefix_re.match(line)
    if not m:
        continue
    rest = m.group(1)
    sep = rest.find(' - ')
    node = rest[:sep] if sep != -1 else rest
    failing_set.add(node.strip())
failing = sorted(failing_set)

baseline = set()
if baseline_path and os.path.isfile(baseline_path):
    with open(baseline_path) as f:
        baseline = {line.strip() for line in f if line.strip()}

display = output
if len(display) > 6000:
    display = display[:3000] + "\n\n...[truncated]...\n\n" + display[-3000:]

if baseline:
    regressions = [t for t in failing if t in baseline]
    new_failures = [t for t in failing if t not in baseline]

    if regressions:
        reason = (
            "QA gate blocked the stop: REGRESSION DETECTED.\n\n"
            "These tests passed BEFORE your changes and now fail:\n"
            + "\n".join(f"  - {t}" for t in regressions)
            + "\n\nRevert the change that broke them. Do NOT add new code "
              "to fix a regression — undo what caused it."
        )
        if new_failures:
            reason += (
                "\n\nAdditionally, these are NEW test failures introduced "
                "by your build:\n"
                + "\n".join(f"  - {t}" for t in new_failures)
            )
    else:
        reason = (
            "QA gate blocked the stop: NEW TEST FAILURES.\n\n"
            "These tests are failing (no regressions detected against "
            "baseline):\n"
            + "\n".join(f"  - {t}" for t in new_failures)
            + "\n\nFix these without breaking existing tests."
        )
else:
    reason = (
        "QA gate blocked the stop: pytest is failing on this branch. "
        "Fix the failing tests before ending your turn."
    )

reason += (
    "\n\n----- pytest output -----\n"
    + display
    + "\n----- end pytest output -----"
)

sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
PY

exit 0
