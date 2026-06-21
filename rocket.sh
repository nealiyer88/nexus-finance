#!/usr/bin/env bash
set -euo pipefail

# ── Rocket Loop — Hybrid Autonomous Build Loop ───────────────────────────────
# Deterministic bash router. Processes features from a queue one at a time:
#   Phase 1   debate (3 adversaries) + reconcile  → hardened design   [MODEL_PLAN]
#   Phase 2   prompt-gen                          → CC build prompt    [MODEL_BUILD]
#   Phase 3   build (nested session)              → code + tests + commit
#   Phase 4-5 review (QA blind + code) + fix loop → PASS/FAIL          [MODEL_BUILD]
#   Phase 4b  QB validation gate        (optional, off unless QB_GATE=1)
#   Phase 4c  exercise gate             (optional, drives the built app)
#   Phase 6   ship (pure bash — flip queue, log, commit, post-ship hook)
#
# Model tiering (configurable in rocket.config.sh):
#   MODEL_PLAN    — adversary debate + reconcile (highest-leverage upstream reasoning)
#   MODEL_BUILD   — prompt-gen, build, reviews, fix, QB gate
#   MODEL_NARRATE — cheap plain-English live narration only (never cost-tracked)
#
# Usage: ./rocket.sh                 # up to MAX_FEATURES
#        ./rocket.sh --max 5         # up to 5
#        ./rocket.sh --feature B4    # force a specific feature id
#        ./rocket.sh --queue Q.md    # use a different queue file
# ──────────────────────────────────────────────────────────────────────────────

# ── Load project config (overrides the defaults below) ────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _cfg in "./rocket.config.sh" "$SCRIPT_DIR/rocket.config.sh"; do
    if [ -f "$_cfg" ]; then # shellcheck disable=SC1090
        source "$_cfg"; break
    fi
done

# ── Defaults (config wins; these fill any gaps) ───────────────────────────────
: "${MODEL_PLAN:=opus}"
: "${MODEL_BUILD:=sonnet}"
: "${MODEL_NARRATE:=claude-haiku-4-5-20251001}"
: "${FEATURE_BUDGET_USD:=50}"
: "${QUEUE_FILE:=FEATURE_QUEUE.md}"
: "${FEATURE_ID_REGEX:=[A-Za-z0-9]+}"
: "${MAX_FEATURES:=3}"
: "${TEST_CMD:=python -m pytest tests/ -x --tb=short}"
: "${ROCKET_BRANCH_DEFAULT:=main}"
: "${GUARD_CLEAN_PATHS:=}"
: "${QB_GATE:=0}"
: "${POST_SHIP_HOOK:=}"
: "${PLAN_LOG_MARKER:=}"
: "${PROMPTS_PATH:=}"
: "${PYTHON_BIN:=}"
: "${AGENT:=claude}"                 # which adapter drives the loop (see .claude/adapters/)
: "${ADAPTERS_DIR:=.claude/adapters}"

# ── Portable python launcher ──────────────────────────────────────────────────
# Cross-platform: python3 (Linux/Mac/Docker) → python → py (Windows launcher).
_python() {
    if [ -n "$PYTHON_BIN" ]; then "$PYTHON_BIN" "$@"; return; fi
    if command -v python3 >/dev/null 2>&1; then python3 "$@"; return; fi
    if command -v python  >/dev/null 2>&1; then python  "$@"; return; fi
    if command -v py      >/dev/null 2>&1; then py      "$@"; return; fi
    echo "rocket: no python interpreter found (tried python3, python, py)" >&2
    return 1
}

# ── Agent adapter (agnostic invocation layer) ─────────────────────────────────
# The loop never calls a specific AI CLI directly — it calls the adapter selected
# by $AGENT (default "claude"). The adapter maps the loop's generic request
# (tier + access mode) to one concrete CLI invocation and defines three functions:
#   agent_build_cmd <tier> <mode> · agent_extract <raw> <dest> · agent_narrate <prompt>
# See .claude/adapters/README.md. Read-only enforcement for reviewers/gates does
# NOT depend on the agent — it is the git-based guard_revert (Layer 2); the adapter's
# own read-only mode (where it has one) is just belt-and-suspenders.
_adapter_file="${ADAPTERS_DIR}/${AGENT}.sh"
if [ -f "$_adapter_file" ]; then
    # shellcheck disable=SC1090
    source "$_adapter_file"
else
    echo "rocket: no adapter for AGENT='$AGENT' at $_adapter_file" >&2
    echo "        available: $(ls "$ADAPTERS_DIR" 2>/dev/null | sed 's/\.sh$//' | tr '\n' ' ')" >&2
    exit 1
fi
for _fn in agent_build_cmd agent_extract agent_narrate; do
    if ! declare -F "$_fn" >/dev/null 2>&1; then
        echo "rocket: adapter '$AGENT' is missing required function: $_fn" >&2
        exit 1
    fi
done

AGENTS_DIR=".claude/agents"
COMMANDS_DIR=".claude/commands"
LOG_DIR="features/_logs"
ADV_DIR="features/_adversaries"
PROMPT_DIR="features/_prompts"
FORCE_FEATURE=""

feature_cost=0
LAST_EXIT=0

# Branch the loop was launched from. Nested agent build/fix sessions may
# `git checkout -b feature/<x>` on their own; we pin every commit back onto this
# branch after each session (see pin_branch) so work never strands on a throwaway
# branch.
ROCKET_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$ROCKET_BRANCH_DEFAULT")"

# Parse args (override config)
while [[ $# -gt 0 ]]; do
    case $1 in
        --max) MAX_FEATURES="$2"; shift 2 ;;
        --feature) FORCE_FEATURE="$2"; shift 2 ;;
        --queue) QUEUE_FILE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

mkdir -p "$LOG_DIR" "$ADV_DIR" "$PROMPT_DIR"
processed=0

# ── Helpers ───────────────────────────────────────────────────────────────────
ts() { date +%Y-%m-%dT%H:%M:%S; }

log_phase() {
    local slug="$1" phase="$2" model="$3" status="$4"
    echo "| $(ts) | $slug | $phase | $model | $status |" >> features/RUN_LOG.md
    echo "Rocket: [$slug] $phase → $status ($model)"
}

# Portable in-place sed: GNU `sed -i` and BSD/macOS `sed -i ''` are incompatible, so
# avoid -i entirely — edit to a temp file and move it back. Works on Linux + macOS.
_sed_inplace() {  # _sed_inplace <sed-expr> <file>
    local expr="$1" file="$2"
    sed "$expr" "$file" > "${file}.sedtmp" && mv "${file}.sedtmp" "$file"
}

# ── Agent call (adapter-routed, cost-tracked) ─────────────────────────────────
# run_tracked <tier> <mode> <result_dest|""> <log_dest> <prompt>
#   tier ∈ plan|build|narrate (→ MODEL_PLAN/BUILD/NARRATE via the adapter)
#   mode ∈ write|readonly|plain (write = may edit files; readonly = read+run only)
# Delegates the actual CLI to the selected adapter (agent_build_cmd / agent_extract).
# Writes the agent's answer to result_dest (skipped when ""), accrues cost into
# feature_cost IF the adapter reports one (else logs "n/a" — graceful degrade), and
# exposes the agent exit code in LAST_EXIT. Never aborts under set -e.
run_tracked() {
    local tier="$1" mode="$2" result_dest="$3" log_dest="$4" prompt="$5"
    local tmp pf; tmp="$(mktemp)"; pf="$(mktemp)"
    LAST_EXIT=0
    # Prompt via STDIN, NOT argv: prompts concatenate agent file + manifest + diff +
    # log and can exceed the Windows CreateProcess ~32KB cap. The prompt file path is
    # also exported as AGENT_PROMPT_FILE for adapters whose CLI wants it as an arg.
    printf '%s' "$prompt" > "$pf"
    local cmd; cmd="$(agent_build_cmd "$tier" "$mode")"
    AGENT_PROMPT_FILE="$pf" eval "$cmd" < "$pf" > "$tmp" 2>"$log_dest" || LAST_EXIT=$?
    local cost; cost="$(agent_extract "$tmp" "$result_dest")"
    rm -f "$tmp" "$pf"
    local _lbl; _lbl=$(basename "$log_dest" 2>/dev/null | sed -E "s/^${SLUG:-}-//; s/-[0-9].*$//; s/\.(log|md)$//")
    if [ -n "$cost" ]; then
        feature_cost=$(_python -c "print(round(float('${feature_cost:-0}') + float('${cost:-0}'), 4))")
        log_phase "${SLUG:-?}" "cost:${_lbl:-call}" "$AGENT" "\$${cost}"
    else
        log_phase "${SLUG:-?}" "cost:${_lbl:-call}" "$AGENT" "n/a"
    fi
}

over_budget() {  # 0 (true) when feature_cost has crossed FEATURE_BUDGET_USD
    _python -c "import sys; sys.exit(0 if float('${feature_cost:-0}') > float(${FEATURE_BUDGET_USD}) else 1)"
}

halt_if_over_budget() {  # <feat> <slug> <phase-label> — block + exit 2 if over budget
    over_budget || return 0
    fail_feature "$2" "feature budget \$${FEATURE_BUDGET_USD} exceeded after $3 (~\$${feature_cost} spent)"
    block_in_queue "$1"
    narrate "$2" "🛑 HALTED — over \$${FEATURE_BUDGET_USD}/feature after $3 (~\$${feature_cost} spent). Human needed."
    echo "██████ ROCKET HALTED — over \$${FEATURE_BUDGET_USD} budget for $1 (~\$${feature_cost} spent) ██████"
    exit 2
}

get_next_feature() {
    if [ -n "$FORCE_FEATURE" ]; then
        echo "$FORCE_FEATURE"
        FORCE_FEATURE=""  # only force once
        return 0
    fi
    # Queue schema: | # | Feature | Brief | Depends On | Spec | Status |
    # `spec` is read but unused by the loop (the parser doesn't gate on it);
    # it exists for human/agent audit so every row links back to its product
    # spec section. Leaving it in the read pattern keeps `status` aligned with
    # the trailing column.
    while IFS='|' read -r _ num feature brief depends spec status _; do
        num=$(echo "$num" | xargs)
        depends=$(echo "$depends" | xargs)
        status=$(echo "$status" | xargs)
        [[ "$status" != "QUEUED" ]] && continue
        local deps_met=true
        if [[ "$depends" != "—" && "$depends" != "-" && "$depends" != "" ]]; then
            IFS=',' read -ra dep_list <<< "$depends"
            for dep in "${dep_list[@]}"; do
                dep=$(echo "$dep" | xargs)
                if ! grep -qE "\|\s*${dep}\s*\|.*\|\s*SHIPPED\s*\|" "$QUEUE_FILE"; then
                    deps_met=false; break
                fi
            done
        fi
        if $deps_met; then echo "$num"; return 0; fi
    done < <(grep -E "^\| ${FEATURE_ID_REGEX} " "$QUEUE_FILE")
    return 1
}

get_brief_path() {
    grep -E "^\| $1 " "$QUEUE_FILE" | awk -F'|' '{print $4}' | xargs
}

fail_feature() {
    local slug="$1" reason="$2"
    echo "## $(ts) — $slug BLOCKED" >> features/DEBUG.md
    echo "Reason: $reason" >> features/DEBUG.md
    echo "" >> features/DEBUG.md
    log_phase "$slug" "BLOCKED" "—" "$reason"
}

block_in_queue() {
    # Flip a feature's queue status to BLOCKED so get_next_feature won't re-pick it.
    # Handles BOTH QUEUED→BLOCKED and SHIPPED→BLOCKED — the latter matters when a
    # nested session falsely marks a feature SHIPPED despite a FAIL.
    local feat="$1"
    _sed_inplace "/^| ${feat} /{s/ QUEUED / BLOCKED /; s/ SHIPPED / BLOCKED /}" "$QUEUE_FILE"
}

pin_branch() {
    # Absorb commits a nested session made on a wandered branch back onto
    # ROCKET_BRANCH, then delete the stray. Safe: only fast-forwards when
    # ROCKET_BRANCH is an ancestor of the wandered branch (it always is — the
    # session branched FROM it). Never rewrites history.
    local slug="${1:-?}" cur
    cur="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$ROCKET_BRANCH")"
    [ "$cur" = "$ROCKET_BRANCH" ] && return 0
    if git merge-base --is-ancestor "$ROCKET_BRANCH" "$cur" 2>/dev/null; then
        git branch -f "$ROCKET_BRANCH" "$cur" 2>/dev/null || true
        git checkout "$ROCKET_BRANCH" 2>/dev/null || true
        git branch -D "$cur" 2>/dev/null || true
        narrate "$slug" "⚠️ session wandered to '$cur' — absorbed its commits onto $ROCKET_BRANCH and deleted the stray"
    else
        narrate "$slug" "⚠️ HEAD on '$cur' which diverged from $ROCKET_BRANCH — NOT auto-merging; human needed"
    fi
}

# ── Real-time narration → features/ROCKET_LIVE.md ─────────────────────────────
# Plain-English running commentary so a human can follow a background run live
# (`tail -f features/ROCKET_LIVE.md`). Best-effort: never aborts the build.
# Set NARRATE=0 to silence.
LIVE_LOG="features/ROCKET_LIVE.md"
narrate() {  # narrate <slug> <label> [artifact-file]
    [ "${NARRATE:-1}" = "0" ] && return 0
    local slug="$1" label="$2" artifact="${3:-}"
    { echo ""; echo "### $(ts) · $(echo "$slug" | tr '[:lower:]' '[:upper:]') · ${label}"; } >> "$LIVE_LOG" 2>/dev/null || true
    if [ -n "$artifact" ] && [ -s "$artifact" ]; then
        local summary
        summary=$(agent_narrate "In 2-3 plain-English sentences for a non-engineer following along live, say what this rocket build step decided or produced. No preamble, no headers, no code blocks. Aggregate only — never include sensitive identifiers or dollar amounts.

$(cat "$artifact")" 2>/dev/null) || summary=""
        [ -z "$summary" ] && summary="(summary unavailable)"
        echo "$summary" >> "$LIVE_LOG" 2>/dev/null || true
    fi
}

# ── Review / Fix / Ship helpers (rocket.sh OWNS these — deterministic) ─────────
# SKIPPED is a first-class verdict (NOT RUN ≠ PASS): gates that did not run report
# SKIPPED and are logged as such — never silently treated as PASS. Reviewers may
# NOT emit SKIPPED as their overall verdict (reviews_pass requires PASS; the
# resilience guard synthesizes FAIL for anything else).
verdict_of() {  # verdict_of <slug> <qa|code|qb-validation|exercise>
    grep -aoE 'VERDICT: (PASS|FAIL|SKIPPED)' "$LOG_DIR/${1}-${2}-verdict.md" 2>/dev/null | tail -1 | awk '{print $2}'
}

# ── Repo-mutation guard for READ-ONLY subprocesses (reviewers, QB gate) ───────
# Snapshot HEAD before the subprocess; after, revert ANY commit, tracked edit, or
# new untracked CODE file it left. Tool-deny is evadable via Bash subshell/sed;
# the parent harness is not. Verdict files live under features/_logs (gitignored,
# written by the PARENT) — reset --hard + the SCOPED clean never touch them.
guard_snapshot() { git rev-parse HEAD 2>/dev/null || echo ""; }
guard_revert() {  # guard_revert <label> <snapshot_sha>
    local label="$1" snap="$2" mutated=0
    [ -z "$snap" ] && return 0
    [ "$(git rev-parse HEAD 2>/dev/null)" != "$snap" ] && mutated=1
    [ -n "$(git status --porcelain 2>/dev/null)" ] && mutated=1
    [ "$mutated" = 0 ] && return 0
    git reset --hard "$snap" >/dev/null 2>&1 || true                 # commits + tracked edits
    # Untracked CODE only — NEVER features/ or .claude/ (verdicts/logs live there).
    if [ -n "$GUARD_CLEAN_PATHS" ]; then
        # shellcheck disable=SC2086
        git clean -fd -- $GUARD_CLEAN_PATHS >/dev/null 2>&1 || true
    else
        git clean -fd -e .claude -e features -e 'rocket.config*' -e ROCKET_SYNC_REPORT.md >/dev/null 2>&1 || true
    fi
    log_phase "${SLUG:-?}" "GUARD" "rocket" "reverted out-of-lane mutation by ${label}"
    narrate "${SLUG:-?}" "🛡️ ${label} mutated the repo out of lane — hard-reverted to ${snap:0:8}. Reviewers/gates are read-only."
    { echo ""; echo "## $(ts) — ${SLUG:-?} — ${label} mutated the repo (out of lane), auto-reverted to ${snap:0:8}"; } >> features/REVIEW_OVERRIDES.md 2>/dev/null || true
}

# ── Queue guard — only do_ship may change the queue ───────────────────────────
# A build/fix session once sed-flipped the queue to SHIPPED out of its lane.
# Restore the queue to its pre-build committed content. Handles an uncommitted
# edit AND one a session sneaked into its code commit — and ALWAYS leaves a clean
# tree so guard_revert never false-fires on a leftover queue diff. Needs
# PRE_BUILD_SHA set.
guard_queue() {  # guard_queue <phase-label>
    local phase="$1"
    [ -z "${PRE_BUILD_SHA:-}" ] && return 0
    git checkout "$PRE_BUILD_SHA" -- "$QUEUE_FILE" 2>/dev/null || return 0
    if ! git diff --cached --quiet -- "$QUEUE_FILE" 2>/dev/null; then
        git commit -q -m "guard(rocket): restore queue touched out-of-lane by ${phase} session (${SLUG:-?})" 2>/dev/null || true
        narrate "${SLUG:-?}" "🛡️ ${phase} session changed the queue out of lane — restored from ${PRE_BUILD_SHA:0:8}"
        { echo ""; echo "## $(ts) — ${SLUG:-?} — ${phase} session changed ${QUEUE_FILE} out of lane, restored from ${PRE_BUILD_SHA:0:8}"; } >> features/REVIEW_OVERRIDES.md 2>/dev/null || true
    fi
}

# ── QB validation gate (Phase 4b) — optional, off unless QB_GATE=1 ─────────────
# Detect QB-touching features and run a QB-MCP validation pass before ship.
# Triggers: keywords in the brief OR an explicit `<!-- qb-validation: required -->`.
# Skips: explicit `<!-- qb-validation: skip -->` (wins over keywords).
qb_validation_required() {  # <brief-path> → 0=require, 1=skip
    local brief="$1"
    [ -f "$brief" ] || return 1
    if grep -qaE '<!--[[:space:]]*qb-validation:[[:space:]]*skip[[:space:]]*-->' "$brief"; then return 1; fi
    if grep -qaE '<!--[[:space:]]*qb-validation:[[:space:]]*required[[:space:]]*-->' "$brief"; then return 0; fi
    if grep -qaiE 'validate against QB|match(es)? QB|compare(d)? (to|against) QB|QB MCP|QB validation|aging detail|aging summary|ap_aging_(detail|summary)|ar_aging_(detail|summary)' "$brief"; then return 0; fi
    return 1
}

run_qb_validation_gate() {  # <slug>
    local slug="$1"
    local _g; _g=$(guard_snapshot)   # gate is read-only — revert any mutation at exit
    local skill=".claude/skills/qb-validation/SKILL.md"
    local verdict="$LOG_DIR/${slug}-qb-validation-verdict.md"
    narrate "$slug" "🔬 QB validation gate running"
    if [ ! -f "$skill" ]; then
        # NOT RUN ≠ PASS: a gate that cannot execute fails CLOSED.
        {
            echo "## [rocket] qb-validation skill missing — gate could not run"
            echo "Expected skill at $skill. Restore it (or mark the brief"
            echo "'<!-- qb-validation: skip -->' if the feature truly has no QB data)."
            echo "VERDICT: FAIL"
        } > "$verdict"
        return 0
    fi
    run_tracked build write "$verdict" "$LOG_DIR/${slug}-qb-validation-$(ts).log" "$(cat "$skill")

You are running the QB validation gate as Phase 4b of the rocket loop. The build
finished and reviewers PASSED. Pick the most applicable Pattern from the skill
above for this feature and execute it.

FEATURE BRIEF:
$(cat "$BRIEF" 2>/dev/null)

BUILD MANIFEST:
$(cat "$LOG_DIR/${slug}-build-manifest.md" 2>/dev/null)

Compare local data against QB MCP. If the feature does NOT present numeric
QB-derived data the user can compare, say so and end with VERDICT: SKIPPED — a
not-applicable gate is SKIPPED, never PASS. Acceptable drift: small + explainable.
Real drift: a material discrepancy you cannot explain from timing or known cutoffs.

NOT RUN ≠ PASS: if you could not complete the comparison (MCP unavailable, data
missing, pattern inapplicable mid-way), report what you could not run and end with
VERDICT: FAIL — never PASS on an unexecuted comparison.

Write a short verdict report. Aggregate-only output. Your FINAL line MUST be
exactly 'VERDICT: PASS', 'VERDICT: FAIL', or 'VERDICT: SKIPPED'."
    # NOTE: the QB gate runs in WRITE mode (it needs MCP tool access the read-only
    # [Read,Bash,Grep,Glob] allowlist would exclude; its protection is guard_revert
    # alone. Acceptable: it compares data, it is not reviewing code it might "fix".
    guard_revert "qb-gate" "$_g"
}

run_review() {  # run_review <slug> <qa|code>
    local slug="$1" kind="$2"
    local _g; _g=$(guard_snapshot)   # snapshot HEAD; revert any mutation at exit
    narrate "$slug" "🔍 ${kind} review running"
    # Diff base = the commit BEFORE the build session. `git diff HEAD~1` is WRONG —
    # build + fix rounds produce multiple commits and HEAD~1 shows only the last.
    local base="${PRE_BUILD_SHA:-HEAD~1}"
    git diff "${base}"..HEAD > "$LOG_DIR/${slug}-build-diff.patch" 2>/dev/null || true

    local payload
    if [ "$kind" = "qa" ]; then
        # ── BLIND CRITIC ──────────────────────────────────────────────────────
        # QA deliberately receives ONLY the brief + the diff. NO manifest, NO build
        # prompt, NO builder pytest log — the builder's self-report anchors the
        # critic to the builder's framing and pre-rationalizes partial builds. The
        # blind critic judges the work against the brief, not the story about it.
        payload="$(cat "$AGENTS_DIR/reviewer-qa.md" 2>/dev/null || true)

FEATURE BRIEF (your ONLY statement of what should exist):
$(cat "$BRIEF" 2>/dev/null || true)

CHANGED FILES (git diff --name-status ${base}..HEAD):
$(git diff --name-status "${base}"..HEAD 2>/dev/null || true)

BUILD DIFF (truncated to 100KB; FULL diff at $LOG_DIR/${slug}-build-diff.patch — read it, or run: git diff ${base}..HEAD):
$(head -c 100000 "$LOG_DIR/${slug}-build-diff.patch" 2>/dev/null || true)

You are the BLIND critic: you received no builder-authored context, by design.
Judge the diff against the brief's success criteria only. Execute everything
yourself. Your FINAL line MUST be exactly 'VERDICT: PASS' or 'VERDICT: FAIL'."
    else
        payload="$(cat "$AGENTS_DIR/reviewer-${kind}.md" 2>/dev/null || true)

BUILD MANIFEST:
$(cat "$LOG_DIR/${slug}-build-manifest.md" 2>/dev/null || true)

BUILD PROMPT:
$(cat "$PROMPT_DIR/${slug}.cc-prompt.md" 2>/dev/null || true)

FEATURE BRIEF:
$(cat "$BRIEF" 2>/dev/null || true)

BUILD DIFF RANGE: git diff ${base}..HEAD  (full patch at $LOG_DIR/${slug}-build-diff.patch — use THIS range, not HEAD~1)

PYTEST LOG (tail — last 200 lines; full log at $LOG_DIR/${slug}-pytest.log):
$(tail -200 "$LOG_DIR/${slug}-pytest.log" 2>/dev/null || true)

Review against the acceptance criteria. Your FINAL line MUST be exactly 'VERDICT: PASS' or 'VERDICT: FAIL'."
    fi
    # Read-only: NO bypassPermissions + allowlist. The reviewer runs tests via Bash
    # but the sandbox blocks repo writes and the Write/Edit tools are absent.
    run_tracked build readonly "$LOG_DIR/${slug}-${kind}-verdict.md" "$LOG_DIR/${slug}-review-${kind}-$(ts).log" "$payload"

    # ── Resilience guarantee (do NOT remove) ──────────────────────────────────
    # The harness must NEVER crash because a reviewer subprocess failed. If the
    # verdict file has no explicit VERDICT line (crash, API error, empty output),
    # synthesize a FAIL so verdict_of / reviews_pass always see a definite verdict.
    local _vfile="$LOG_DIR/${slug}-${kind}-verdict.md"
    if ! grep -qaE 'VERDICT: (PASS|FAIL)' "$_vfile" 2>/dev/null; then
        {
            echo ""
            echo "## [rocket resilience guard] ${kind} review produced no verdict"
            echo "claude subprocess exit=${LAST_EXIT:-?}; output had no 'VERDICT:' line."
            echo "Synthesizing FAIL so the harness reacts instead of crashing. See review log."
            echo "VERDICT: FAIL"
        } >> "$_vfile"
        narrate "$slug" "⚠️ ${kind} review produced no verdict (exit ${LAST_EXIT:-?}) — recorded FAIL (resilience guard)"
    fi

    guard_revert "${kind}-review" "$_g"   # undo any commit/edit/untracked file the reviewer left
}

reviews_pass() {  # reviews_pass <slug> → 0 if BOTH PASS
    local slug="$1"
    [ "$(verdict_of "$slug" qa)" = "PASS" ] && [ "$(verdict_of "$slug" code)" = "PASS" ]
}

verdict_fingerprint() {  # stable hash of both verdicts — used to detect a stuck fixer
    # cksum is POSIX (present on Linux + macOS); md5sum is absent on stock macOS.
    cat "$LOG_DIR/${1}-qa-verdict.md" "$LOG_DIR/${1}-code-verdict.md" 2>/dev/null | cksum | awk '{print $1}'
}

run_fixer() {  # run_fixer <slug>
    local slug="$1"
    local combined="$LOG_DIR/${slug}-review-verdict.md"
    { echo "# Combined verdict — ${slug}"; echo "## QA"; cat "$LOG_DIR/${slug}-qa-verdict.md" 2>/dev/null;
      echo "## CODE"; cat "$LOG_DIR/${slug}-code-verdict.md" 2>/dev/null; } > "$combined"
    local rendered
    rendered=$(sed \
        -e "s|\$ARGUMENTS\.slug|${slug}|g" \
        -e "s|\$ARGUMENTS\.verdict|${combined}|g" \
        -e "s|\$ARGUMENTS\.build_prompt|${PROMPT_DIR}/${slug}.cc-prompt.md|g" \
        -e "s|\$ARGUMENTS\.log_dir|${LOG_DIR}|g" \
        "$COMMANDS_DIR/rocket-fix.md")
    run_tracked build write "" "$LOG_DIR/${slug}-fix-session-$(ts).log" "$rendered"
}

run_post_ship_hook() {  # <slug> — optional project command after a successful ship
    local slug="$1"
    [ -z "$POST_SHIP_HOOK" ] && return 0
    narrate "$slug" "🔄 running post-ship hook"
    bash -c "$POST_SHIP_HOOK" >> "$LOG_DIR/post-ship-${slug}.log" 2>&1 \
        || narrate "$slug" "⚠️ post-ship hook exited non-zero (non-fatal) — see post-ship-${slug}.log"
}

# ── Brief-FILES completeness gate ─────────────────────────────────────────────
# The build/fix sessions are the ONLY committers of code, and rocket-build.md
# FORBIDS `git add -A`. The cost: any file a session EDITS but forgets to name in
# its hand-written `git add` strands uncommitted → the shipped commit is broken.
# This rescues exactly the brief's FILES-list paths still dirty just before ship —
# NEVER `git add -A`, NEVER a "reuse / do NOT edit" path, NEVER a file absent from
# the FILES list. No-op on the happy path.
ensure_brief_files_committed() {  # <feat> <slug>
    local feat="$1" slug="$2"
    [ -f "$BRIEF" ] || return 0
    local paths
    paths=$(_python - "$BRIEF" <<'PY'
import re, sys
from pathlib import Path
txt = Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r"^##\s+FILES\s*$(.*?)(?=^##\s|\Z)", txt, re.MULTILINE | re.DOTALL)
sect = m.group(1) if m else ""
out = []
for line in sect.splitlines():
    if re.search(r"reuse|do not edit", line, re.IGNORECASE):
        continue
    for tok in re.findall(r"`([^`]+)`", line):
        tok = tok.strip()
        if "/" in tok and re.search(r"\.[A-Za-z0-9]+$", tok):
            out.append(tok)
print("\n".join(dict.fromkeys(out)))
PY
)
    [ -z "$paths" ] && return 0
    local staged=0 rescued=""
    while IFS= read -r p; do
        p="${p%$'\r'}"   # strip CR — Windows python prints \r\n; a trailing \r makes git miss the path
        [ -z "$p" ] && continue
        if [ -n "$(git status --porcelain -- "$p" 2>/dev/null)" ]; then
            git add -- "$p" 2>/dev/null && { staged=1; rescued="$rescued $p"; }
        fi
    done <<< "$paths"
    if [ "$staged" = 1 ]; then
        git commit -q -m "ship-fix(rocket): stage brief FILES paths the build left uncommitted (${slug})" || true
        log_phase "$slug" "files-rescue" "rocket" "staged:${rescued# }"
        narrate "$slug" "🩹 staged brief FILES paths the build forgot to commit:${rescued}"
    fi
}

do_ship() {  # do_ship <FEAT> <slug> — bash ship: no AI agent
    local feat="$1" slug="$2"
    _sed_inplace "/^| ${feat} /s/ QUEUED / SHIPPED /" "$QUEUE_FILE"    # 1. flip queue (reliable)
    local tc; tc=$(grep -aoE '[0-9]+ passed' "$LOG_DIR/${slug}-pytest.log" 2>/dev/null | tail -1)
    # Gate statuses recorded verbatim — a gate that didn't run says NOT RUN / SKIPPED, never PASS.
    local qbv exv
    qbv=$(verdict_of "$slug" qb-validation); exv=$(verdict_of "$slug" exercise)
    { echo ""; echo "## $(date +%Y-%m-%d) — ${feat} (shipped by rocket)";
      echo "Brief: $BRIEF"; echo "Tests: ${tc:-see pytest log}"; echo "Reviews: QA PASS (blind), Code PASS";
      echo "Gates: QB=${qbv:-NOT RUN}, Exercise=${exv:-NOT RUN}"; } >> features/SHIPPED.md

    # Optional PLAN_LOG index line (only when PLAN_LOG_MARKER is configured).
    if [ -n "$PLAN_LOG_MARKER" ] && [ -f ".claude/plans/PLAN_LOG.md" ]; then
        PLAN_FEAT="$feat" PLAN_SLUG="$slug" PLAN_BRIEF="$BRIEF" PLAN_MARKER="$PLAN_LOG_MARKER" \
        _python - <<'PYEOF' 2>/dev/null || echo "[ROCKET] WARN: PLAN_LOG append failed for ${slug} (non-fatal)"
import os, re, datetime
from pathlib import Path
feat = os.environ["PLAN_FEAT"]; slug = os.environ["PLAN_SLUG"]
brief = Path(os.environ["PLAN_BRIEF"]); marker = os.environ["PLAN_MARKER"]
plan_log = Path(".claude/plans/PLAN_LOG.md")
if not brief.exists() or not plan_log.exists():
    raise SystemExit("file missing")
m = re.search(r"^#\s*(?:Feature Brief:\s*)?(.+?)\s*$", brief.read_text(encoding="utf-8"), re.MULTILINE)
title = m.group(1).strip() if m else slug
date = datetime.date.today().isoformat()
line = f"- {date} [SHIPPED] {feat} — {title} (rocket).\n"
content = plan_log.read_text(encoding="utf-8")
mk = marker if marker.endswith("\n") else marker + "\n\n"
if mk in content:
    plan_log.write_text(content.replace(mk, mk + line, 1), encoding="utf-8")
elif marker in content:
    plan_log.write_text(content.replace(marker, marker + "\n\n" + line, 1), encoding="utf-8")
else:
    plan_log.write_text(content.rstrip() + "\n\n" + line, encoding="utf-8")
PYEOF
    fi

    # Human-judgment steps (spec/learnings/prompts curation) → queue for a human.
    local curate="curate specs + learnings"
    [ -n "$PROMPTS_PATH" ] && curate="$curate + $PROMPTS_PATH"
    echo "- [${slug}] ${curate} ($(ts))" >> features/PENDING_HUMAN_UPDATES.md

    # Commit ONLY aggregate, PII-free bookkeeping. Do NOT add "$LOG_DIR/${slug}-"* —
    # those verdict/manifest/session logs may contain sensitive values and their
    # timestamped filenames make `git add` error out. Logs stay local (gitignored).
    local add_list=("$QUEUE_FILE" features/SHIPPED.md features/PENDING_HUMAN_UPDATES.md features/RUN_LOG.md features/ROCKET_LIVE.md)
    [ -f ".claude/plans/PLAN_LOG.md" ] && add_list+=(.claude/plans/PLAN_LOG.md)
    git add "${add_list[@]}" 2>/dev/null || true
    git commit -q -m "ship: ${slug} — rocket (reviews PASS)" || true
    run_post_ship_hook "$slug"   # optional: bring shipped code live, etc.
}

# ── Main Loop ─────────────────────────────────────────────────────────────────
echo "──────────────────────────────────────────────────"
echo "Rocket Loop: starting. Max $MAX_FEATURES features. Queue: $QUEUE_FILE"
echo "──────────────────────────────────────────────────"

while [ $processed -lt $MAX_FEATURES ]; do
    FEAT=$(get_next_feature) || break
    BRIEF=$(get_brief_path "$FEAT")
    SLUG=$(echo "$FEAT" | tr '[:upper:]' '[:lower:]')
    feature_cost=0   # reset per-feature spend accumulator (gate: $FEATURE_BUDGET_USD)

    if [ ! -f "$BRIEF" ]; then
        fail_feature "$SLUG" "brief not found: $BRIEF"
        block_in_queue "$FEAT"   # mark BLOCKED so get_next_feature won't re-pick it (else: infinite loop)
        continue
    fi

    echo ""
    echo "─── Rocket: $FEAT from $BRIEF ───"
    narrate "$SLUG" "🚀 Starting $FEAT ($(basename "$BRIEF"))"

    # ── Phase 1a-c: Adversaries (3x standalone, MODEL_PLAN) ──────────────
    echo "Phase 1: Adversary debate"
    for role in design skeptic engineer; do
        run_tracked plan readonly "$ADV_DIR/${SLUG}-${role}.md" "$LOG_DIR/${SLUG}-adv-${role}-$(ts).log" "$(cat "$AGENTS_DIR/adversary-${role}.md")

Read this feature brief and produce your argument:
$(cat "$BRIEF")"
        log_phase "$SLUG" "adversary-${role}" "$MODEL_PLAN" "done"
        narrate "$SLUG" "🎭 ${role} agent weighed in" "$ADV_DIR/${SLUG}-${role}.md"
    done

    # ── Phase 1d: Reconcile (standalone, MODEL_PLAN) ─────────────────────
    run_tracked plan readonly "$ADV_DIR/${SLUG}-hardened.md" "$LOG_DIR/${SLUG}-reconcile-$(ts).log" "$(cat "$AGENTS_DIR/adversary-reconcile.md")

DESIGN ARGUMENT:
$(cat "$ADV_DIR/${SLUG}-design.md")

SKEPTIC ARGUMENT:
$(cat "$ADV_DIR/${SLUG}-skeptic.md")

ENGINEER ASSESSMENT:
$(cat "$ADV_DIR/${SLUG}-engineer.md")

ORIGINAL BRIEF:
$(cat "$BRIEF")"
    log_phase "$SLUG" "reconcile" "$MODEL_PLAN" "done"
    narrate "$SLUG" "⚖️ Reconciled the debate into a hardened plan" "$ADV_DIR/${SLUG}-hardened.md"
    halt_if_over_budget "$FEAT" "$SLUG" "Phase 1 (adversary debate)"

    # ── Phase 2: Prompt Generation (standalone, MODEL_BUILD) ─────────────
    echo "Phase 2: Prompt generation"
    run_tracked build readonly "$PROMPT_DIR/${SLUG}.cc-prompt.md" "$LOG_DIR/${SLUG}-promptgen-$(ts).log" "$(cat "$AGENTS_DIR/prompt-gen.md")

HARDENED DESIGN:
$(cat "$ADV_DIR/${SLUG}-hardened.md")

ORIGINAL BRIEF:
$(cat "$BRIEF")

Write the complete CC build prompt. If the brief names a spec reference, read it for shapes and conventions."
    log_phase "$SLUG" "prompt-gen" "$MODEL_BUILD" "done"
    narrate "$SLUG" "📝 Wrote the build prompt" "$PROMPT_DIR/${SLUG}.cc-prompt.md"
    halt_if_over_budget "$FEAT" "$SLUG" "Phase 2 (prompt-gen)"

    # ── Phase 3: BUILD (nested session — build + test + commit code ONLY) ──
    echo "Phase 3: Build"
    narrate "$SLUG" "🔨 Builder starting — code + tests + commit"
    bash .claude/hooks/record-baseline.sh
    # Pre-build commit = diff base for ALL review rounds (build + fix commits stack;
    # HEAD~1 only ever shows the last one). Persisted for post-run forensics.
    PRE_BUILD_SHA=$(git rev-parse HEAD 2>/dev/null || echo "HEAD~1")
    echo "$PRE_BUILD_SHA" > "$LOG_DIR/${SLUG}-prebuild-sha.txt"
    RENDERED_BUILD_PROMPT=$(sed \
        -e "s|\$ARGUMENTS\.slug|${SLUG}|g" \
        -e "s|\$ARGUMENTS\.build_prompt|${PROMPT_DIR}/${SLUG}.cc-prompt.md|g" \
        -e "s|\$ARGUMENTS\.feature_brief|${BRIEF}|g" \
        -e "s|\$ARGUMENTS\.log_dir|${LOG_DIR}|g" \
        "$COMMANDS_DIR/rocket-build.md")
    export ROCKET_SESSION=1
    run_tracked build write "" "$LOG_DIR/${SLUG}-build-session-$(ts).log" "$RENDERED_BUILD_PROMPT"
    BUILD_EXIT=$LAST_EXIT
    unset ROCKET_SESSION
    processed=$((processed + 1))

    if [ $BUILD_EXIT -ne 0 ]; then
        fail_feature "$SLUG" "build session exit=$BUILD_EXIT"
        block_in_queue "$FEAT"
        narrate "$SLUG" "🛑 HALTED — build session failed (exit $BUILD_EXIT)"
        echo "██████ ROCKET HALTED — build failed for $FEAT ██████"
        exit 2
    fi

    pin_branch "$SLUG"   # absorb commits if the build session wandered onto a feature branch
    guard_queue "build"  # only do_ship may change the queue — restore any out-of-lane edit
    halt_if_over_budget "$FEAT" "$SLUG" "Phase 3 (build)"

    # ── Phases 4-5: REVIEW + FIX (deterministic) ──
    # Anti-Ralph-Wiggum guards: hard cap of 3 fix rounds AND an early halt if a fix
    # round leaves the verdicts byte-identical (the fixer is stuck — stop, don't spin).
    echo "Phases 4-5: Review + fix (max 3 rounds)"
    attempt=0
    while true; do
        halt_if_over_budget "$FEAT" "$SLUG" "review/fix round $((attempt + 1))"
        run_review "$SLUG" qa
        run_review "$SLUG" code
        narrate "$SLUG" "🧪 review round $((attempt + 1)) — QA=$(verdict_of "$SLUG" qa) Code=$(verdict_of "$SLUG" code)"
        if reviews_pass "$SLUG"; then break; fi

        attempt=$((attempt + 1))
        if [ "$attempt" -ge 3 ]; then
            fail_feature "$SLUG" "reviews still FAIL after 3 fix rounds"
            block_in_queue "$FEAT"
            narrate "$SLUG" "🛑 HALTED — reviews FAIL after 3 rounds. Human needed."
            echo "██████ ROCKET HALTED — HUMAN INTERVENTION REQUIRED for $FEAT (review FAIL) ██████"
            echo "See $LOG_DIR/${SLUG}-qa-verdict.md , ${SLUG}-code-verdict.md"
            exit 2
        fi

        before_fp=$(verdict_fingerprint "$SLUG")
        run_fixer "$SLUG"
        pin_branch "$SLUG"   # absorb commits if the fixer session wandered
        guard_queue "fix"    # only do_ship may change the queue
        run_review "$SLUG" qa
        run_review "$SLUG" code
        if reviews_pass "$SLUG"; then break; fi
        if [ "$(verdict_fingerprint "$SLUG")" = "$before_fp" ]; then
            fail_feature "$SLUG" "fixer made no progress (identical verdict) — stuck"
            block_in_queue "$FEAT"
            narrate "$SLUG" "🛑 HALTED — fixer made no progress (Ralph-Wiggum guard). Human needed."
            echo "██████ ROCKET HALTED — fixer stuck on $FEAT (no progress) ██████"
            exit 2
        fi
    done

    # ── Phase 4b: QB MCP VALIDATION GATE (optional) ──
    if [ "$QB_GATE" = "1" ] && qb_validation_required "$BRIEF"; then
        echo "Phase 4b: QB validation gate"
        run_qb_validation_gate "$SLUG"
        case "$(verdict_of "$SLUG" qb-validation)" in
            PASS)
                log_phase "$SLUG" "qb-validation" "$MODEL_BUILD" "PASS"
                narrate "$SLUG" "🔬 QB validation PASS — proceeding to ship" ;;
            SKIPPED)
                log_phase "$SLUG" "qb-validation" "$MODEL_BUILD" "SKIPPED (gate found no QB-comparable data)"
                narrate "$SLUG" "🔬 QB validation SKIPPED — no QB-comparable data (recorded as skipped, NOT a pass)" ;;
            *)
                fail_feature "$SLUG" "QB validation gate FAILED — operator review required"
                block_in_queue "$FEAT"
                narrate "$SLUG" "🛑 HALTED — QB validation FAIL. See $LOG_DIR/${SLUG}-qb-validation-verdict.md"
                echo "██████ ROCKET HALTED — QB validation FAIL for $FEAT ██████"
                exit 2 ;;
        esac
    else
        log_phase "$SLUG" "qb-validation" "—" "SKIPPED (QB gate off / no QB keywords)"
    fi

    # ── Phase 4c: EXERCISE GATE — drive the built app (generic hook) ──
    # Reviewers read code and run tests; nothing above this line has DRIVEN the app.
    # The hook delegates to a project-supplied exercise script. Default (no script)
    # = SKIPPED: ship proceeds but the record says "not exercised". FAIL halts.
    echo "Phase 4c: Exercise gate"
    EX_HOOK=".claude/hooks/post-build-exercise.sh"
    if [ -f "$EX_HOOK" ]; then
        bash "$EX_HOOK" "$SLUG" "$BRIEF" "$LOG_DIR" || true
    fi
    if [ ! -f "$EX_HOOK" ] || ! grep -qaE 'VERDICT: (PASS|FAIL|SKIPPED)' "$LOG_DIR/${SLUG}-exercise-verdict.md" 2>/dev/null; then
        {
            echo "## [rocket] exercise gate could not run"
            echo "Hook missing or no verdict line written. Restore $EX_HOOK."
            echo "VERDICT: FAIL"
        } >> "$LOG_DIR/${SLUG}-exercise-verdict.md"
    fi
    case "$(verdict_of "$SLUG" exercise)" in
        PASS)
            log_phase "$SLUG" "exercise-gate" "hook" "PASS"
            narrate "$SLUG" "🕹️ exercise gate PASS — app flows verified" ;;
        SKIPPED)
            log_phase "$SLUG" "exercise-gate" "hook" "SKIPPED (no project exercise script)"
            narrate "$SLUG" "🕹️ exercise gate SKIPPED — app NOT exercised (no project script; recorded as skipped, NOT a pass)" ;;
        *)
            fail_feature "$SLUG" "exercise gate FAILED — app flows broken; operator review required"
            block_in_queue "$FEAT"
            narrate "$SLUG" "🛑 HALTED — exercise gate FAIL. See $LOG_DIR/${SLUG}-exercise-verdict.md"
            echo "██████ ROCKET HALTED — exercise gate FAIL for $FEAT ██████"
            exit 2 ;;
    esac

    # ── Phase 6: SHIP (bash — reliable, no classifier) ──
    ensure_brief_files_committed "$FEAT" "$SLUG"   # rescue any brief FILES path left dirty
    do_ship "$FEAT" "$SLUG"
    log_phase "$SLUG" "feature-cost" "—" "\$${feature_cost} / \$${FEATURE_BUDGET_USD}"
    log_phase "$SLUG" "SHIPPED" "rocket" "complete"
    narrate "$SLUG" "✅ SHIPPED — both reviews PASS, queue flipped, committed (~\$${feature_cost} spent)."
    echo "Rocket: ✓ shipped $SLUG ($processed/$MAX_FEATURES, ~\$${feature_cost})"
done

echo ""
echo "──────────────────────────────────────────────────"
echo "Rocket Loop: complete. $processed features processed."
echo "──────────────────────────────────────────────────"
