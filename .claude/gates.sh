#!/usr/bin/env bash
# ── gates.sh — quality gate runner ──────────────────────────────────────────
# Installed to a project as .claude/gates.sh. Runs the project's configured
# quality commands (format/lint/typecheck/test/coverage/secrets/dep-audit/
# exercise) for a scope and writes a one-line-per-gate report ending in an
# exact VERDICT line. This file is AGNOSTIC CORE: it names no language, tool,
# or vendor — every command it runs comes from rocket.config.sh, set by the
# project. Concrete examples belong only in templates/rocket.config.example.sh.
#
# Usage:  bash .claude/gates.sh <pre-merge|post-integration|all> <slug> [outfile]
#   pre-merge         : format, lint, typecheck
#   post-integration  : lint, typecheck, test, coverage, secrets, dep-audit, exercise
#   all               : every gate above
#
# NOT-RUN != PASS (house rule): a gate whose *_CMD is the empty string is
# SKIPPED. A gate whose *_CMD is set but cannot even be executed (exit 126/127
# — command not found, not executable) is FAIL, never SKIPPED, never PASS —
# "nothing was verified" is reported as a failure, not silently waved through.
#
# Exit code: 0 when the final VERDICT is PASS, 1 when FAIL, 2 on a usage error
# (bad args/scope) before any gate ran — distinct from a real gate verdict so
# callers can tell "gates ran and failed" from "gates.sh was invoked wrong".
#
# Deliberately NOT `set -e`, and deliberately NOT `$(cmd) || true; EC=$?` —
# see hooks/qa-gate.sh's comment on that exact bug: `|| true` captures ITS OWN
# exit status (always 0), so the real command's exit code is lost and a gate
# can never block. Every command here is run as `(...) >logfile 2>&1; ec=$?`
# with nothing between the command and the `$?` read.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SCOPE="${1:-}"
SLUG="${2:-}"
OUTFILE="${3:-}"

if [ -z "$SCOPE" ] || [ -z "$SLUG" ]; then
    echo "usage: gates.sh <pre-merge|post-integration|all> <slug> [outfile]" >&2
    exit 2
fi
case "$SCOPE" in
    pre-merge|post-integration|all) ;;
    *)
        echo "gates.sh: unknown scope '$SCOPE' (want pre-merge|post-integration|all)" >&2
        exit 2 ;;
esac

PROJ="${CLAUDE_PROJECT_DIR:-.}"

# ── Config: every *_CMD may be empty = SKIPPED. Same sourcing convention as
# hooks/qa-gate.sh — set defaults first so an unset var never trips `set -u`,
# then let the project's rocket.config.sh override.
: "${FORMAT_CMD:=}"
: "${LINT_CMD:=}"
: "${TYPECHECK_CMD:=}"
: "${TEST_CMD:=}"
: "${COVERAGE_CMD:=}"
: "${COVERAGE_MIN:=}"
: "${SECRETS_CMD:=}"
: "${DEPAUDIT_CMD:=}"
: "${EXERCISE_HOOK:=}"
: "${PYTHON_BIN:=}"
if [ -f "${PROJ}/rocket.config.sh" ]; then
    # shellcheck disable=SC1090
    source "${PROJ}/rocket.config.sh" 2>/dev/null || true
fi

# python3 (or the project's PYTHON_BIN) for anything needing real numeric
# parsing/arithmetic — coverage-percent comparison is not a job for grep/sed.
run_python() {
    if [ -n "$PYTHON_BIN" ] && command -v "$PYTHON_BIN" >/dev/null 2>&1; then "$PYTHON_BIN" "$@"; return; fi
    if command -v python3 >/dev/null 2>&1; then python3 "$@"; return; fi
    if command -v python  >/dev/null 2>&1; then python  "$@"; return; fi
    if command -v py      >/dev/null 2>&1; then py      "$@"; return; fi
    return 127
}

# ── Where output goes ─────────────────────────────────────────────────────────
# Per-gate raw logs live "next to <outfile>": in a sibling directory named
# after it. With no outfile (report to stdout, no path to sit "next to"), logs
# fall back to the project's existing features/_logs/ convention, scoped by
# slug+scope so concurrent gate runs don't clobber each other.
if [ -n "$OUTFILE" ]; then
    LOGDIR="$(dirname -- "$OUTFILE")/$(basename -- "$OUTFILE").gate-logs"
    mkdir -p -- "$(dirname -- "$OUTFILE")" 2>/dev/null || true
else
    LOGDIR="${PROJ}/features/_logs/gates/${SLUG}-${SCOPE}"
fi
mkdir -p -- "$LOGDIR" 2>/dev/null || LOGDIR="${TMPDIR:-/tmp}"

REPORT_FILE="${LOGDIR}/${SLUG}-${SCOPE}-report.tmp"
: > "$REPORT_FILE"

FAILED=0

# gate_result <name> <PASS|FAIL|SKIPPED> <reason> <logfile-or-empty> <show_tail:0|1>
# Raw tool output never goes in the report itself — only a one-line reason,
# plus (only when show_tail=1, i.e. on a real FAIL) up to 20 lines of tail.
# Full output always lives in <logfile> under LOGDIR regardless.
gate_result() {
    local name="$1" status="$2" reason="$3" logfile="${4:-}" show_tail="${5:-0}"
    {
        printf 'GATE %s: %s  (%s)\n' "$name" "$status" "$reason"
        if [ "$show_tail" = "1" ] && [ -n "$logfile" ] && [ -f "$logfile" ]; then
            tail -n 20 "$logfile" | sed 's/^/    | /'
        fi
    } >> "$REPORT_FILE"
}

# run_blocking_gate <name> <cmd> — the common shape shared by lint, typecheck,
# test, secrets, dep-audit: empty cmd = SKIPPED; 126/127 = FAIL (environment
# error, fail closed); exit 0 = PASS; any other exit = FAIL.
run_blocking_gate() {
    local name="$1" cmd="$2"
    if [ -z "$cmd" ]; then
        gate_result "$name" SKIPPED "command not configured" "" 0
        return
    fi
    local logfile="${LOGDIR}/${SLUG}-${name}.log"
    ( cd "$PROJ" && eval "$cmd" ) >"$logfile" 2>&1
    local ec=$?
    if [ $ec -eq 126 ] || [ $ec -eq 127 ]; then
        gate_result "$name" FAIL "ENVIRONMENT ERROR: $cmd could not be run — nothing was verified" "$logfile" 1
        FAILED=1
    elif [ $ec -eq 0 ]; then
        gate_result "$name" PASS "$cmd (exit 0)" "" 0
    else
        gate_result "$name" FAIL "$cmd failed (exit $ec)" "$logfile" 1
        FAILED=1
    fi
}

# FORMAT_CMD is advisory (auto-fix): it reports PASS or SKIPPED only, and
# NEVER fails the run — even if the command itself errors or can't execute.
# This is the one deliberate exception to "fail closed": the contract states
# explicitly that FORMAT_CMD never fails the run, unlike every other gate.
run_format_gate() {
    if [ -z "$FORMAT_CMD" ]; then
        gate_result format SKIPPED "command not configured" "" 0
        return
    fi
    local logfile="${LOGDIR}/${SLUG}-format.log"
    ( cd "$PROJ" && eval "$FORMAT_CMD" ) >"$logfile" 2>&1
    local ec=$?
    gate_result format PASS "ran (exit $ec); advisory — never blocks VERDICT; full output: $logfile" "" 0
}

# COVERAGE_CMD + COVERAGE_MIN: blocking. Empty COVERAGE_CMD = SKIPPED. A
# COVERAGE_CMD with no COVERAGE_MIN is a misconfiguration, not a free pass —
# fail closed rather than guess a threshold. Output that prints no parseable
# number is FAIL (ENVIRONMENT ERROR), never PASS, per the contract.
run_coverage_gate() {
    if [ -z "$COVERAGE_CMD" ]; then
        gate_result coverage SKIPPED "command not configured" "" 0
        return
    fi
    if [ -z "$COVERAGE_MIN" ]; then
        gate_result coverage FAIL "ENVIRONMENT ERROR: COVERAGE_CMD is set but COVERAGE_MIN is not — nothing was verified" "" 0
        FAILED=1
        return
    fi
    local logfile="${LOGDIR}/${SLUG}-coverage.log"
    ( cd "$PROJ" && eval "$COVERAGE_CMD" ) >"$logfile" 2>&1
    local ec=$?
    if [ $ec -eq 126 ] || [ $ec -eq 127 ]; then
        gate_result coverage FAIL "ENVIRONMENT ERROR: $COVERAGE_CMD could not be run — nothing was verified" "$logfile" 1
        FAILED=1
        return
    fi
    local pct
    pct=$(COV_LOG="$logfile" run_python - <<'PY'
import os, re
text = open(os.environ["COV_LOG"], errors="replace").read()
# A PERCENTAGE is required — "NN%" — and the last one wins, because coverage
# tools print a per-file breakdown followed by a TOTAL line.
#
# There is deliberately no fallback to a bare number. A fallback sounds
# forgiving and is the opposite: any digit in the output becomes the coverage
# figure, so a command printing a timestamp, a file count, or an error code
# yields a number that is compared against COVERAGE_MIN and passes or fails
# arbitrarily. A gate that reports a confident verdict from a number it made
# up is worse than one that admits it could not measure — so no percent sign
# means no measurement, which the caller turns into FAIL.
nums = re.findall(r"(\d+(?:\.\d+)?)\s*%", text)
print(nums[-1] if nums else "")
PY
)
    if [ -z "$pct" ]; then
        gate_result coverage FAIL "ENVIRONMENT ERROR: $COVERAGE_CMD printed no parseable coverage number — nothing was verified" "$logfile" 1
        FAILED=1
        return
    fi
    local verdict
    verdict=$(PCT="$pct" MIN="$COVERAGE_MIN" run_python - <<'PY'
import os
pct = float(os.environ["PCT"])
mn = float(os.environ["MIN"])
print("PASS" if pct >= mn else "FAIL")
PY
)
    if [ "$verdict" = "PASS" ]; then
        gate_result coverage PASS "coverage ${pct}% >= min ${COVERAGE_MIN}%" "" 0
    else
        gate_result coverage FAIL "coverage ${pct}% < min ${COVERAGE_MIN}%" "$logfile" 1
        FAILED=1
    fi
}

# EXERCISE_HOOK: blocking; a script that boots/drives the app, invoked the
# same way rocket.sh's exercise phase invokes it — <hook> <slug> <brief> <log-dir>
# — and expected to write "<log-dir>/<slug>-exercise-verdict.md" whose last
# line matches `VERDICT: PASS|FAIL|SKIPPED`. No brief path is available to a
# standalone gates.sh run, so an empty string is passed (the hook contract
# already treats that argument as optional). A hook that runs but writes no
# parseable verdict is FAIL, same "ran and produced nothing = broken gate,
# not a pass" rule the bundled hooks/post-build-exercise.sh applies one layer
# down to a project's own exercise script.
run_exercise_gate() {
    if [ -z "$EXERCISE_HOOK" ]; then
        gate_result exercise SKIPPED "command not configured" "" 0
        return
    fi
    if [ ! -f "$EXERCISE_HOOK" ]; then
        gate_result exercise FAIL "ENVIRONMENT ERROR: $EXERCISE_HOOK could not be run — nothing was verified" "" 0
        FAILED=1
        return
    fi
    local hooklog="${LOGDIR}/${SLUG}-exercise-hook.log"
    local verdict_file="${LOGDIR}/${SLUG}-exercise-verdict.md"
    rm -f "$verdict_file"
    ( cd "$PROJ" && bash "$EXERCISE_HOOK" "$SLUG" "" "$LOGDIR" ) >"$hooklog" 2>&1
    local ec=$?
    if [ $ec -eq 126 ] || [ $ec -eq 127 ]; then
        gate_result exercise FAIL "ENVIRONMENT ERROR: $EXERCISE_HOOK could not be run — nothing was verified" "$hooklog" 1
        FAILED=1
        return
    fi
    local v
    v=$(grep -aoE 'VERDICT: (PASS|FAIL|SKIPPED)' "$verdict_file" 2>/dev/null | tail -1 | awk '{print $2}')
    case "$v" in
        PASS)    gate_result exercise PASS "hook verdict PASS ($verdict_file)" "" 0 ;;
        SKIPPED) gate_result exercise SKIPPED "hook verdict SKIPPED — app not exercised ($verdict_file)" "" 0 ;;
        FAIL)    gate_result exercise FAIL "hook verdict FAIL — see $verdict_file" "$verdict_file" 1; FAILED=1 ;;
        *)
            gate_result exercise FAIL "ENVIRONMENT ERROR: exercise hook produced no verdict — nothing was verified" "$hooklog" 1
            FAILED=1 ;;
    esac
}

# ── Which gates run in which scope ────────────────────────────────────────────
{
    printf '# gates.sh report — scope=%s slug=%s\n' "$SCOPE" "$SLUG"
    printf '# per-gate logs: %s\n\n' "$LOGDIR"
} >> "$REPORT_FILE"

case "$SCOPE" in
    pre-merge)
        run_format_gate
        run_blocking_gate lint "$LINT_CMD"
        run_blocking_gate typecheck "$TYPECHECK_CMD"
        ;;
    post-integration)
        run_blocking_gate lint "$LINT_CMD"
        run_blocking_gate typecheck "$TYPECHECK_CMD"
        run_blocking_gate test "$TEST_CMD"
        run_coverage_gate
        run_blocking_gate secrets "$SECRETS_CMD"
        run_blocking_gate dep-audit "$DEPAUDIT_CMD"
        run_exercise_gate
        ;;
    all)
        run_format_gate
        run_blocking_gate lint "$LINT_CMD"
        run_blocking_gate typecheck "$TYPECHECK_CMD"
        run_blocking_gate test "$TEST_CMD"
        run_coverage_gate
        run_blocking_gate secrets "$SECRETS_CMD"
        run_blocking_gate dep-audit "$DEPAUDIT_CMD"
        run_exercise_gate
        ;;
esac

if [ $FAILED -eq 0 ]; then
    printf 'VERDICT: PASS\n' >> "$REPORT_FILE"
else
    printf 'VERDICT: FAIL\n' >> "$REPORT_FILE"
fi

if [ -n "$OUTFILE" ]; then
    cat "$REPORT_FILE" > "$OUTFILE"
else
    cat "$REPORT_FILE"
fi

[ $FAILED -eq 0 ] && exit 0
exit 1
