#!/usr/bin/env bash
# ── Phase 4c Exercise Gate — generic hook ───────────────────────────────────
# rocket.sh calls this after reviews PASS (and after the optional Phase-4b QB
# gate) and before ship. Its job: actually DRIVE the built app — boot it,
# exercise core flows, and assert control→output coupling (an input/click must
# change what the app produces). Reviewers read code and run unit tests; this
# gate is the only mechanical proof the app works as an app.
#
# Contract (rocket.sh side):
#   called as:  post-build-exercise.sh <slug> <brief-path> <log-dir>
#   must write: <log-dir>/<slug>-exercise-verdict.md whose LAST verdict line is
#               exactly  VERDICT: PASS | VERDICT: FAIL | VERDICT: SKIPPED
#   exit code:  0 unless the hook itself crashed (rocket reads the verdict file,
#               not the exit code — same pattern as the review verdicts)
#
# NOT RUN ≠ PASS: when no project exercise script exists, this hook reports
# SKIPPED — never PASS. rocket.sh ships on SKIPPED but logs it as skipped in
# RUN_LOG.md and SHIPPED.md, so a ship that was never exercised says so.
#
# To activate for this project: copy hooks/exercise.project.template.sh to
# .claude/hooks/exercise.project.sh and fill in your app's boot + test commands.
# It receives the same 3 args and owns the verdict file; if it writes no verdict,
# this wrapper records a FAIL (a gate that ran and produced nothing is broken).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SLUG="${1:?usage: post-build-exercise.sh <slug> <brief-path> <log-dir>}"
BRIEF="${2:-}"
LOG_DIR="${3:?usage: post-build-exercise.sh <slug> <brief-path> <log-dir>}"
VERDICT_FILE="$LOG_DIR/${SLUG}-exercise-verdict.md"
PROJECT_SCRIPT=".claude/hooks/exercise.project.sh"

if [ -f "$PROJECT_SCRIPT" ]; then
    bash "$PROJECT_SCRIPT" "$SLUG" "$BRIEF" "$LOG_DIR" || true
    # Project script owns the verdict file. No verdict written = broken gate = FAIL.
    if ! grep -qaE 'VERDICT: (PASS|FAIL|SKIPPED)' "$VERDICT_FILE" 2>/dev/null; then
        {
            echo ""
            echo "## [exercise hook] project script wrote no verdict"
            echo "$PROJECT_SCRIPT ran but $VERDICT_FILE has no 'VERDICT:' line."
            echo "A gate that ran and produced nothing is a broken gate, not a pass."
            echo "VERDICT: FAIL"
        } >> "$VERDICT_FILE"
    fi
else
    {
        echo "## Exercise gate — ${SLUG}"
        echo ""
        echo "No project exercise script at \`$PROJECT_SCRIPT\` — the app was NOT exercised."
        echo "This is a SKIP, not a pass: no app-level flow was driven, no"
        echo "control→output coupling was asserted."
        echo ""
        echo "To close this gap, copy hooks/exercise.project.template.sh to"
        echo "$PROJECT_SCRIPT and fill in your boot + test commands (e.g. boot the"
        echo "server, drive the feature's core flow, assert that inputs change output,"
        echo "write VERDICT: PASS|FAIL here)."
        echo ""
        echo "VERDICT: SKIPPED"
    } > "$VERDICT_FILE"
fi

exit 0
