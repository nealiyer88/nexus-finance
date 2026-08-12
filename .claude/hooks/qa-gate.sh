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
#
# Deliberately NOT `set -e`: this hook must capture the runner's exit code and
# keep going. An earlier version did `TEST_OUTPUT=$(...) || true; TEST_EXIT=$?`,
# which captures the exit of `|| true` — always 0 — so the gate reported "tests
# passed" unconditionally and could never block anything. A gate that always
# allows is worse than no gate: it looks like a safety net and isn't one.
set -uo pipefail

if [ -z "${ROCKET_SESSION:-}" ]; then exit 0; fi

PROJ="${CLAUDE_PROJECT_DIR:-.}"
BASELINE_FILE="${PROJ}/.claude/hooks/test-baseline.txt"
TEST_DIR="${PROJ}/tests"

# Already continuing from a stop-hook block? Allow the stop, or the hook and the
# agent can hold each other in a loop.
HOOK_INPUT="$(cat 2>/dev/null || true)"
case "$HOOK_INPUT" in
    *'"stop_hook_active":true'* | *'"stop_hook_active": true'*) exit 0 ;;
esac

# Test command: prefer rocket.config.sh's TEST_CMD, else a sensible default.
TEST_CMD="python -m pytest tests/ -x --tb=short -q"
if [ -f "${PROJ}/rocket.config.sh" ]; then
    # shellcheck disable=SC1090
    source "${PROJ}/rocket.config.sh" 2>/dev/null || true
fi

# If no tests directory exists, allow stop (nothing to gate on).
if [ ! -d "$TEST_DIR" ]; then exit 0; fi

TEST_OUTPUT=$(cd "$PROJ" && eval "$TEST_CMD" 2>&1)
TEST_EXIT=$?

if [ $TEST_EXIT -eq 0 ]; then exit 0; fi

# 126/127 mean the runner itself could not be executed or was not found — the
# suite never ran, so we know nothing about the code. Block with that stated
# plainly rather than letting it read as an ordinary test failure, and rather
# than allowing the stop on a suite that was never executed.
if [ $TEST_EXIT -eq 126 ] || [ $TEST_EXIT -eq 127 ]; then
    export TEST_CMD TEST_OUTPUT
    "${PYTHON_BIN:-python3}" - <<'PY'
import json, os
print(json.dumps({"decision": "block", "reason":
    "ENVIRONMENT ERROR: the test command could not be run (%s). The suite did NOT "
    "execute, so nothing is verified. Do not claim tests passed or were checked by "
    "inspection — state ENVIRONMENT ERROR in your final output.\n\n%s"
    % (os.environ.get("TEST_CMD", "?"), os.environ.get("TEST_OUTPUT", "")[-800:])}))
PY
    exit 0
fi

# Classify regression vs new failure against the baseline, and emit the decision
# as properly-escaped JSON. Escaping is done by a JSON encoder, not by sed: a
# backslash, control char, or quote in test output would otherwise produce
# malformed JSON, which the harness cannot read as a block — so a genuine test
# failure would silently let the agent stop.
export TEST_OUTPUT BASELINE_FILE
"${PYTHON_BIN:-python3}" - <<'PY'
import json, os, re

out = os.environ.get("TEST_OUTPUT", "")
baseline_path = os.environ.get("BASELINE_FILE", "")
baseline = ""
if baseline_path and os.path.exists(baseline_path):
    with open(baseline_path, errors="replace") as fh:
        baseline = fh.read()

# CUSTOMIZE this parse if your runner's failure lines differ from `FAILED <id>`.
failing = re.findall(r"^FAILED\s+(\S+)", out, re.M)
regressions = [t for t in failing if t in baseline]
new_failures = [t for t in failing if t not in baseline]

parts = ["Tests failing."]
if regressions:
    parts.append(
        "REGRESSIONS — Revert the change that broke these. Do NOT add new code to "
        "fix a regression; undo what caused it.\n"
        + "\n".join(f"  REGRESSION: {t}" for t in regressions)
    )
if new_failures:
    parts.append(
        "NEW FAILURES — Fix these without breaking existing tests.\n"
        + "\n".join(f"  NEW FAILURE: {t}" for t in new_failures)
    )
if not failing:
    # No parseable FAILED lines: collection error, import error, crash. Show the
    # tail rather than claiming zero failures.
    parts.append(
        "The run failed without parseable FAILED lines (collection/import error?). "
        "Output tail:\n" + out[-2000:]
    )

print(json.dumps({"decision": "block", "reason": "\n\n".join(parts)}))
PY
