#!/usr/bin/env bash
# ── Record Baseline — Pre-Build Test Snapshot ───────────────────────────────
# Snapshots passing test IDs before each build. qa-gate.sh uses this to
# distinguish regressions from new failures. Called by rocket.sh before Phase 3.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

PROJ="${CLAUDE_PROJECT_DIR:-.}"
BASELINE_FILE="${PROJ}/.claude/hooks/test-baseline.txt"
TEST_DIR="${PROJ}/tests"

# Collect command: prefer rocket.config.sh's BASELINE_COLLECT_CMD, else default.
BASELINE_COLLECT_CMD="python -m pytest tests/ --collect-only -q"
if [ -f "${PROJ}/rocket.config.sh" ]; then
    # shellcheck disable=SC1090
    source "${PROJ}/rocket.config.sh" 2>/dev/null || true
fi

if [ ! -d "$TEST_DIR" ]; then
    echo "# No tests directory — empty baseline" > "$BASELINE_FILE"
    exit 0
fi

# Test node IDs contain "::" in pytest; CUSTOMIZE the filter for another runner.
( cd "$PROJ" && eval "$BASELINE_COLLECT_CMD" 2>/dev/null ) | grep "::" > "$BASELINE_FILE" || true
COUNT=$(wc -l < "$BASELINE_FILE" | tr -d ' ')
echo "Baseline recorded: $COUNT test(s)"
