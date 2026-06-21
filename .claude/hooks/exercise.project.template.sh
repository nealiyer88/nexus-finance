#!/usr/bin/env bash
# ── Phase 4c Exercise Gate — PROJECT RUNNER TEMPLATE ─────────────────────────
# Copy this file to `.claude/hooks/exercise.project.sh` and fill in the CUSTOMIZE
# sections for your app. post-build-exercise.sh delegates to it after reviews PASS.
#
# What it does: boot your app, wait until it's ready, run an app-level test
# (e.g. Playwright / Cypress / an HTTP smoke / a CLI flow), map the result to a
# verdict file, and ALWAYS tear the app down (trap EXIT + port-based kill).
#
# Contract (fixed by post-build-exercise.sh — do NOT change):
#   called as:  exercise.project.sh <slug> <brief-path> <log-dir>
#   must write: <log-dir>/<slug>-exercise-verdict.md whose LAST line is exactly
#               VERDICT: PASS | VERDICT: FAIL | VERDICT: SKIPPED
#   exit code:  ignored (the wrapper reads the verdict file)
#
# Keep output aggregate-only — no secrets / PII / dollar values in the verdict
# file or committed logs. Full test output goes to a gitignored sidecar log.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SLUG="${1:?usage: exercise.project.sh <slug> <brief-path> <log-dir>}"
BRIEF="${2:-}"
LOG_DIR="${3:?usage: exercise.project.sh <slug> <brief-path> <log-dir>}"

# ── CUSTOMIZE ─────────────────────────────────────────────────────────────────
# Ports your app listens on (used for readiness + teardown). List all of them.
APP_PORTS=(8080)
# A URL that returns HTTP 200 once the app is ready (health/root). Empty = skip wait.
HEALTH_URL="http://127.0.0.1:8080/health"
# Command to BOOT the app in the background (runs from repo root). Keep it a single
# process where possible so teardown is clean (avoid auto-reload that orphans workers).
boot_app() {
  # e.g.  python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 > "$APP_LOG" 2>&1 &
  echo "CUSTOMIZE: replace boot_app() with your server launch" >&2
  return 1
}
# Command that DRIVES the app and exits non-zero on failure (the actual gate).
# e.g.  ( cd e2e && npx playwright test )   |   bash scripts/smoke.sh
run_exercise() {
  # npx playwright test
  echo "CUSTOMIZE: replace run_exercise() with your app-level test" >&2
  return 1
}
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
mkdir -p "$LOG_DIR"
VERDICT_FILE="$LOG_DIR/${SLUG}-exercise-verdict.md"
APP_LOG="$LOG_DIR/${SLUG}-exercise-app.log"
TEST_LOG="$LOG_DIR/${SLUG}-exercise-test.log"
APP_PID=""

log() { printf '[exercise] %s\n' "$*"; }

# ── port helpers — cross-platform best effort ─────────────────────────────────
pids_on_port() {
  # Linux/Mac: lsof; Windows/Git-Bash: netstat (PID = last column).
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti ":$1" 2>/dev/null | sort -u
  else
    netstat -ano 2>/dev/null | grep -E "[:.]$1[[:space:]]+.*LISTENING" | awk '{print $NF}' | sort -u
  fi
}
kill_pid() {
  local pid="$1"; [ -n "$pid" ] || return 0
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //PID "$pid" //F //T >/dev/null 2>&1 || true   # Windows tree-kill
  else
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi
}
free_port() { local p; for p in $(pids_on_port "$1"); do kill_pid "$p"; done; }
ports_free() { local p; for p in "${APP_PORTS[@]}"; do [ -n "$(pids_on_port "$p")" ] && return 1; done; return 0; }

teardown() {
  log "teardown: freeing ports ${APP_PORTS[*]}"
  [ -n "$APP_PID" ] && kill_pid "$APP_PID"
  local i p
  for i in 1 2 3 4 5 6; do
    for p in "${APP_PORTS[@]}"; do free_port "$p"; done
    ports_free && break
    sleep 1
  done
  ports_free && log "teardown: ports freed" || log "teardown: WARNING — ports still occupied"
}
trap teardown EXIT

wait_for_200() {
  local url="$1" timeout="${2:-60}" i=0 code="000"
  [ -z "$url" ] && return 0
  while [ "$i" -lt "$timeout" ]; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    [ "$code" = "200" ] && { log "ready ($url)"; return 0; }
    i=$((i + 1)); sleep 1
  done
  log "NOT ready after ${timeout}s (last HTTP $code): $url"
  return 1
}

finish() { { echo ""; echo "$1"; echo ""; echo "VERDICT: $2"; } >> "$VERDICT_FILE"; exit 0; }

# ── fresh verdict file ───────────────────────────────────────────────────────
{ echo "# Exercise gate — ${SLUG}"; echo ""; echo "Brief:  ${BRIEF:-(none)}"; echo "Ports:  ${APP_PORTS[*]}"; } > "$VERDICT_FILE"

# ── clean slate, boot, wait, run, map ────────────────────────────────────────
ports_free || { log "clearing pre-existing listeners"; for p in "${APP_PORTS[@]}"; do free_port "$p"; done; sleep 2; }
cd "$REPO_ROOT" || finish "## Boot failure"$'\n'"Could not cd to repo root." FAIL

log "booting app"
if ! boot_app; then finish "## Boot failure"$'\n'"boot_app() failed (see ${SLUG}-exercise-app.log). Did you customize it?" FAIL; fi
APP_PID=$!

if ! wait_for_200 "$HEALTH_URL" 90; then
  finish "## Boot failure"$'\n'"App did not become healthy within 90s (see ${SLUG}-exercise-app.log)." FAIL
fi

log "running exercise"
run_exercise > "$TEST_LOG" 2>&1
EX=$?
log "exercise exit=${EX}"

if [ "$EX" -eq 0 ]; then
  finish "## Result"$'\n'"App-level exercise passed. Full log: ${SLUG}-exercise-test.log" PASS
else
  finish "## Result"$'\n'"App-level exercise FAILED (exit ${EX}). Full log: ${SLUG}-exercise-test.log" FAIL
fi
