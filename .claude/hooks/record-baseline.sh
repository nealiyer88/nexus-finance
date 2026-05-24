#!/usr/bin/env bash
# record-baseline.sh — snapshot the current set of test node IDs into
# .claude/hooks/test-baseline.txt. Called by rocket.sh before each feature
# build so the qa-gate Stop hook can distinguish regressions (tests that
# were in the baseline) from new failures (tests added during the build).
#
# Uses pytest --collect-only as a proxy for "currently passing tests" —
# rocket.sh only records a baseline at the start of a build, when the
# branch is green.

set -uo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$PROJECT_DIR"

BASELINE_FILE=".claude/hooks/test-baseline.txt"

if [ ! -d tests ]; then
  : > "$BASELINE_FILE"
  echo "Baseline recorded: 0 tests (no tests/ directory)"
  exit 0
fi

if command -v pytest >/dev/null 2>&1; then
  PYTEST_CMD=(pytest tests/ --collect-only -q)
elif command -v python3 >/dev/null 2>&1 && python3 -m pytest --version >/dev/null 2>&1; then
  PYTEST_CMD=(python3 -m pytest tests/ --collect-only -q)
else
  : > "$BASELINE_FILE"
  echo "Baseline recorded: 0 tests (pytest unavailable)"
  exit 0
fi

# pytest --collect-only -q emits one node id per line (containing `::`),
# then a blank line and a summary like "170 tests collected in 0.05s".
# Filter to lines containing `::` to keep only node ids.
"${PYTEST_CMD[@]}" 2>/dev/null \
  | grep -E '::' \
  | sort -u > "$BASELINE_FILE"

count="$(wc -l < "$BASELINE_FILE" | tr -d ' ')"
echo "Baseline recorded: $count tests"
