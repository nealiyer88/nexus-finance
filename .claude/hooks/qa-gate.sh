#!/usr/bin/env bash
# ── QA Gate — Stop Hook ──────────────────────────────────────────────────────
# Blocks a build/fix agent from ending its turn while tests fail.
# Baseline-aware: distinguishes regressions (revert) from new failures (fix).
# Wired via .claude/settings.json as a Stop hook.
#
# Fires ONLY during a rocket build/fix session (ROCKET_SESSION=1, set by rocket.sh)
# so it never blocks a human's normal Claude Code sessions in this project. To make
# it gate EVERY session instead, delete the ROCKET_SESSION guard below.
# ─────────────────────────────────────────────────────────────────────────────
if [ -z "${ROCKET_SESSION:-}" ]; then exit 0; fi

PROJ="${CLAUDE_PROJECT_DIR:-.}"
BASELINE_FILE="${PROJ}/.claude/hooks/test-baseline.txt"
TEST_DIR="${PROJ}/tests"

# Test command: prefer rocket.config.sh's TEST_CMD, else a sensible default.
TEST_CMD="python -m pytest tests/ -x --tb=short -q"
if [ -f "${PROJ}/rocket.config.sh" ]; then
    # shellcheck disable=SC1090
    source "${PROJ}/rocket.config.sh" 2>/dev/null || true
fi

# If no tests directory exists, allow stop (nothing to gate on).
if [ ! -d "$TEST_DIR" ]; then exit 0; fi

TEST_OUTPUT=$(cd "$PROJ" && eval "$TEST_CMD" 2>&1) || true
TEST_EXIT=$?

if [ $TEST_EXIT -eq 0 ]; then exit 0; fi

if [ -f "$BASELINE_FILE" ]; then
    # CUSTOMIZE the parse if your runner's "FAILED" line format differs from pytest's.
    FAILING=$(echo "$TEST_OUTPUT" | grep -E "^FAILED " | sed 's/FAILED //' | sed 's/ -.*$//')
    REGRESSION_MSG=""
    NEW_FAIL_MSG=""
    while IFS= read -r test_id; do
        [ -z "$test_id" ] && continue
        if grep -qF "$test_id" "$BASELINE_FILE"; then
            REGRESSION_MSG="${REGRESSION_MSG}\n  REGRESSION: ${test_id}"
        else
            NEW_FAIL_MSG="${NEW_FAIL_MSG}\n  NEW FAILURE: ${test_id}"
        fi
    done <<< "$FAILING"

    REASON=""
    if [ -n "$REGRESSION_MSG" ]; then
        REASON="${REASON}REGRESSIONS — Revert the breaking change. Do NOT add new code to fix a regression.${REGRESSION_MSG}\n\n"
    fi
    if [ -n "$NEW_FAIL_MSG" ]; then
        REASON="${REASON}NEW FAILURES — Fix without breaking existing tests.${NEW_FAIL_MSG}\n\n"
    fi

    echo "{\"decision\": \"block\", \"reason\": \"$(echo -e "$REASON" | sed 's/"/\\"/g' | tr '\n' ' ')\"}"
else
    echo "{\"decision\": \"block\", \"reason\": \"Tests failing. Fix all failures before completing. $(echo "$TEST_OUTPUT" | tail -20 | sed 's/"/\\"/g' | tr '\n' ' ')\"}"
fi
