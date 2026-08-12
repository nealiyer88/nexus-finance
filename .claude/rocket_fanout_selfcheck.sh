#!/usr/bin/env bash
# ── rocket_fanout_selfcheck.sh — verify the fan-out machinery, no live agent ───
# Drives fanout_build() from .claude/rocket_fanout.sh against a throwaway git repo
# using a FAKE builder (ROCKET_FANOUT_BUILDER_FN). Proves worktree alloc → build →
# ownership enforcement → per-slice test gate → clean-union merge → integration →
# cleanup, the two failure policies (block / ship-rest), and the failure modes a
# LIVE agent produces that a well-behaved fake never would:
#
#   · writes outside its owns set (existing file)   → reverted
#   · creates a file another unit owns              → reverted AND unit fails
#   · creates an unpredicted, unclaimed file        → KEPT and merged
#   · two units create the SAME unpredicted path    → both fail loudly
#   · hangs                                          → per-unit timeout, unit fails
#   · exits 0 having written nothing                 → unit fails (not-run ≠ pass)
#   · passes the builder but fails its own test      → unit fails
#   · declares no test command                       → unit fails
#   · spends money in N concurrent units             → cost accumulates, none lost
#
# `set -euo pipefail` on purpose: rocket.sh runs under it, so the trapdoors it
# creates (`cmd; ec=$?`, a trailing `[ … ] && …`) must be exercised here, not
# discovered on a live run. Exercised by tests/test_fanout_worktree.py.
#
# Requires: git, bash, and a python interpreter path in $SELFCHECK_PY (the caller
# passes it; the harness's own _python would resolve python3 in-container).
set -euo pipefail

_D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FANOUT_LIB="$_D/rocket_fanout.sh"
# scheduler is alongside (core: ./scripts) or up one (installed: .claude/ → ../scripts)
if [ -f "$_D/scripts/rocket_schedule.py" ]; then SCHEDULE_PY_SRC="$_D/scripts/rocket_schedule.py"
else SCHEDULE_PY_SRC="$_D/../scripts/rocket_schedule.py"; fi
: "${SELFCHECK_PY:=python3}"

fail() { echo "SELFCHECK FAIL: $*" >&2; exit 1; }
have() { [ -f "$1" ] || fail "expected file present: $1"; }
absent() { [ ! -f "$1" ] || fail "expected file ABSENT (enforcement/policy): $1"; }

# ── Minimal runtime deps that rocket_fanout.sh expects from rocket.sh ──────────
_python() { "$SELFCHECK_PY" "$@"; }
ts() { echo "t"; }               # deterministic; real ts() adds a timestamp
export -f _python ts 2>/dev/null || true
feature_cost=0
run_cost=0

# ── Build a throwaway repo ────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP" || fail "cd tmp"
git init -q -b main
git config user.email sc@example.com
git config user.name selfcheck
mkdir -p scripts features/_plan features/_logs
cp "$SCHEDULE_PY_SRC" scripts/rocket_schedule.py
echo "seed" > seed.txt
mkdir -p shared && echo "pre-existing" > shared/preexisting.txt
printf '.rocket/\nfeatures/_logs/\n' > .gitignore

SCRIPT_DIR="$TMP"
LOG_DIR="features/_logs"
MAX_PARALLEL=2
UNIT_TIMEOUT_SECONDS=600     # per-scenario overrides below

cat > features/_plan/sc-1.units.yml <<'YML'
feature: SC-1
slug: sc-1
units:
  - id: SC-1/contracts
    role: contract
    model: lead
    owns: [shared/contract.txt]
    test: "true"
  - id: SC-1/a
    role: fanout
    model: worker
    owns: [a/one.txt]
    reads: [shared/contract.txt]
    depends_on: [SC-1/contracts]
    on_failure: ship-rest
    test: 'sh -c "exit ${SC_TEST_RC_A:-0}"'
  - id: SC-1/b
    role: fanout
    model: lead
    owns: [b/two.txt]
    reads: [shared/contract.txt]
    depends_on: [SC-1/contracts]
    on_failure: block
    test: "true"
  - id: SC-1/integration
    role: integration
    model: lead
    owns: [integrated.txt]
    depends_on: [SC-1/a, SC-1/b]
    test: "true"
YML

# A second feature whose lone unit declares NO test command — the map defect that
# must fail the unit rather than sail through ungated.
cat > features/_plan/sc-2.units.yml <<'YML'
feature: SC-2
slug: sc-2
units:
  - id: SC-2/untested
    role: contract
    model: lead
    owns: [untested.txt]
YML

git add -A && git commit -qm "seed (scripts + maps)"
ROOT="$(git rev-parse HEAD)"
reset_tree() { git reset -q --hard "$ROOT" && git clean -qfd >/dev/null 2>&1 || true; }

# ── Fake builder ──────────────────────────────────────────────────────────────
# Writes its owns files in its worktree. Env knobs simulate real misbehaviour:
#   SC_FAIL_UNIT   unit that exits non-zero
#   SC_STRAY       unit that also modifies a PRE-EXISTING file it does not own
#   SC_INVENT      unit that also creates an UNPREDICTED, unclaimed file
#   SC_LANDGRAB    unit that creates a file inside ANOTHER unit's owns set
#   SC_HANG        unit that never returns
#   SC_NOOP        unit that exits 0 having written nothing
#   SC_COST        dollars each unit reports (exercises concurrent accumulation)
fake_builder() {
    local wt="$1" role="$2" uid="$3" owns="$4"
    local reads="$5" test="$6" brief="$7" log="${8:-}"
    if [ "${SC_HANG:-}" = "$uid" ]; then sleep 120; fi
    if [ "${SC_NOOP:-}" != "$uid" ]; then
      ( cd "$wt" || exit 1
        local IFS=','; local f
        for f in $owns; do
            if [ -z "$f" ]; then continue; fi
            case "$f" in */) mkdir -p "$f"; echo x > "${f}keep" ;;   # dir-owns
                          *)  mkdir -p "$(dirname "$f")"; echo "$uid" > "$f" ;;
            esac
        done
        if [ "${SC_STRAY:-}" = "$uid" ]; then
            echo "escaped" >> shared/preexisting.txt     # existing file → must revert
        fi
        if [ "${SC_INVENT:-}" = "$uid" ]; then
            mkdir -p invented; echo "$uid" > "invented/${uid##*/}.txt"
        fi
        if [ "${SC_COLLIDE:-}" = "fanout" ] && [ "$role" = "fanout" ]; then
            mkdir -p invented; echo "$uid" > invented/same.txt
        fi
        if [ "${SC_LANDGRAB:-}" = "$uid" ]; then
            mkdir -p b; echo "$uid" > b/two.txt          # owned by SC-1/b
        fi ) || return 9
    fi
    if [ -n "${SC_COST:-}" ]; then _fanout_record_cost "$uid" "$SC_COST" 0 "$log"; fi
    if [ "${SC_FAIL_UNIT:-}" = "$uid" ]; then return 3; fi
    return 0
}
export ROCKET_FANOUT_BUILDER_FN=fake_builder
export ROCKET_FANOUT=1

# shellcheck disable=SC1090
source "$FANOUT_LIB"

# run_fanout <feat> <slug> — call fanout_build the way rocket.sh does (capturing
# the exit code WITHOUT `; rc=$?`, which `set -e` would never let us reach).
FANOUT_RC=0
run_fanout() { FANOUT_RC=0; fanout_build "$1" "$2" "features/_plan/$2.units.yml" || FANOUT_RC=$?; }

echo "── scenario 1: happy path + enforcement + unpredicted-file policy ──"
( export SC_STRAY="SC-1/a" SC_INVENT="SC-1/b"
  fanout_build SC-1 sc-1 features/_plan/sc-1.units.yml ) || fail "s1 fanout_build non-zero"
have shared/contract.txt; have a/one.txt; have b/two.txt; have integrated.txt
[ "$(cat shared/preexisting.txt)" = "pre-existing" ] \
    || fail "s1 out-of-scope write to an EXISTING file was not reverted"
have invented/b.txt    # unpredicted + unclaimed → KEPT and merged (policy)
[ -z "$(git status --porcelain)" ] || fail "s1 tree not clean/committed"
[ ! -d .rocket/worktrees ] || [ -z "$(ls -A .rocket/worktrees 2>/dev/null)" ] || fail "s1 worktrees not cleaned"
# NOT `git log | grep -q`: grep -q exits on the first match, git takes SIGPIPE,
# and `set -o pipefail` turns that into a failed pipeline. Buffer, then match.
_log="$(git log --oneline)"
case "$_log" in *"fanout: merge step"*) ;; *) fail "s1 no merge commit" ;; esac
echo "  scenario 1 OK"

reset_tree

echo "── scenario 2: block policy (unit b fails) blocks the feature ──"
( export SC_FAIL_UNIT="SC-1/b"; run_fanout SC-1 sc-1; [ "$FANOUT_RC" -ne 0 ] ) \
    || fail "s2 should block (non-zero)"
absent b/two.txt                                          # failed block-unit NOT merged
echo "  scenario 2 OK"

reset_tree

echo "── scenario 3: ship-rest policy (unit a fails) ships the rest ──"
: > FIX_QUEUE.md
( export SC_FAIL_UNIT="SC-1/a" FIX_QUEUE_FILE="FIX_QUEUE.md"
  run_fanout SC-1 sc-1; [ "$FANOUT_RC" -eq 0 ] ) || fail "s3 should NOT block (exit 0)"
absent a/one.txt                                          # failed ship-rest unit dropped
have b/two.txt                                            # the rest shipped
grep -q "SC-1/a" FIX_QUEUE.md || fail "s3 failed slice not filed to FIX_QUEUE"
echo "  scenario 3 OK"

reset_tree

echo "── scenario 4: a HANGING unit hits the per-unit timeout and FAILS ──"
_t0=$(date +%s)
( export SC_HANG="SC-1/b" UNIT_TIMEOUT_SECONDS=3
  run_fanout SC-1 sc-1; [ "$FANOUT_RC" -ne 0 ] ) || fail "s4 timeout must FAIL the unit, not pass it"
_t1=$(date +%s)
[ $((_t1 - _t0)) -lt 90 ] || fail "s4 took ${_t0}->${_t1}s — the timeout did not fire"
absent b/two.txt                                          # timed-out unit not merged
echo "  scenario 4 OK ($((_t1 - _t0))s)"

reset_tree

echo "── scenario 5: a unit that exits 0 having written NOTHING fails ──"
( export SC_NOOP="SC-1/b"; run_fanout SC-1 sc-1; [ "$FANOUT_RC" -ne 0 ] ) \
    || fail "s5 empty/no-op unit must FAIL (not-run is never pass)"
absent b/two.txt
echo "  scenario 5 OK"

reset_tree

echo "── scenario 6: builder succeeds but the unit's own TEST fails ──"
( export SC_TEST_RC_A=1 FIX_QUEUE_FILE="FIX_QUEUE.md"
  run_fanout SC-1 sc-1; [ "$FANOUT_RC" -eq 0 ] ) || fail "s6 ship-rest unit should not block"
absent a/one.txt                                          # test gate kept it out of the merge
have b/two.txt
echo "  scenario 6 OK"

reset_tree

echo "── scenario 7: two units invent the SAME unpredicted path → both fail ──"
( export SC_COLLIDE="fanout"; run_fanout SC-1 sc-1; [ "$FANOUT_RC" -ne 0 ] ) \
    || fail "s7 unpredicted-path collision must FAIL the units"
absent invented/same.txt
absent b/two.txt                                          # block-policy unit b failed too
echo "  scenario 7 OK"

reset_tree

echo "── scenario 8: a unit creating a file ANOTHER unit owns is reverted + fails ──"
( export SC_LANDGRAB="SC-1/a" FIX_QUEUE_FILE="FIX_QUEUE.md"
  run_fanout SC-1 sc-1; [ "$FANOUT_RC" -eq 0 ] ) || fail "s8 ship-rest land-grabber should not block"
absent a/one.txt                                          # land-grabber failed → not merged
[ "$(cat b/two.txt)" = "SC-1/b" ] || fail "s8 b/two.txt was written by the wrong unit"
echo "  scenario 8 OK"

reset_tree

echo "── scenario 9: a unit with NO test command FAILS (not-run is never pass) ──"
run_fanout SC-2 sc-2
[ "$FANOUT_RC" -ne 0 ] || fail "s9 untested unit must FAIL"
absent untested.txt
echo "  scenario 9 OK"

reset_tree

echo "── scenario 10: cost from CONCURRENT units accumulates, none lost ──"
feature_cost=0; run_cost=0
export SC_COST=0.25
run_fanout SC-1 sc-1
unset SC_COST
[ "$FANOUT_RC" -eq 0 ] || fail "s10 fanout should succeed"
# 4 units x $0.25 = $1.00. Losing the two CONCURRENT units' spend (the subshell
# trap) would show $0.50 — the ceiling would be blind to exactly the phase
# fan-out multiplies.
_expect=1.0
_python -c "import sys; sys.exit(0 if abs(float('$feature_cost') - $_expect) < 1e-6 else 1)" \
    || fail "s10 feature_cost=$feature_cost, expected $_expect (concurrent unit cost lost)"
_python -c "import sys; sys.exit(0 if abs(float('$run_cost') - $_expect) < 1e-6 else 1)" \
    || fail "s10 run_cost=$run_cost, expected $_expect"
echo "  scenario 10 OK (feature_cost=$feature_cost run_cost=$run_cost)"

echo "SELFCHECK PASS"
