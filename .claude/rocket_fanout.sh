#!/usr/bin/env bash
# ── rocket_fanout.sh — fan-out build machinery (design §6) ────────────────────
# Sourced by rocket.sh AFTER _python and the adapter are defined. Turns the
# single-writer Build phase into: freeze-first contracts → isolated concurrent
# worktrees → clean-union merge → integration. Consumes the step-2 scheduler's
# execution plan (scripts/rocket_schedule.py --emit-plan) so it never re-parses
# YAML here.
#
# OPT-IN / DO NO HARM: nothing below runs unless ROCKET_FANOUT=1. When off, or on
# a solo/mapless/invalid feature, rocket.sh keeps its existing single-writer path.
#
# Testability: the actual build call goes through fanout_invoke_builder, which is
# overridable via ROCKET_FANOUT_BUILDER_FN. The git worktree/merge/enforcement
# plumbing is therefore verifiable with a fake builder (no live agent) — see
# .claude/rocket_fanout_selfcheck.sh.
# ──────────────────────────────────────────────────────────────────────────────

: "${ROCKET_WT_DIR:=.rocket/worktrees}"
: "${MAX_PARALLEL:=3}"
: "${FIX_QUEUE_FILE:=FIX_QUEUE.md}"
# Wall-clock ceiling for ONE unit — the builder call and, separately, that unit's
# own test. A live agent can hang indefinitely (a stuck tool call, a wedged API
# socket); with no limit the whole unattended run stops forever with nothing in
# the log. 0 disables. A timeout is a unit FAILURE, never a silent pass.
: "${UNIT_TIMEOUT_SECONDS:=1800}"

_fanout_schedule() { _python "$SCRIPT_DIR/scripts/rocket_schedule.py" "$@"; }

# _fanout_safe_uid <unit-id> — filesystem-safe form of a unit id (ids contain /).
_fanout_safe_uid() { printf '%s' "$1" | tr '/ ' '__'; }

# ── Per-unit wall-clock limit ─────────────────────────────────────────────────
# Implemented in bash ON PURPOSE: `timeout(1)` is GNU coreutils and is simply
# absent on macOS and on minimal images, so depending on it would silently
# disable the limit on exactly the hosts most likely to run this.

# _fanout_kill_tree <pid> — kill a pid and everything under it. Killing only the
# direct child leaves the agent CLI (its grandchild) running and holding the API
# session, so the "timeout" would free the slot but not the money.
_fanout_kill_tree() {
    local pid="$1" child
    for child in $(ps -e -o pid=,ppid= 2>/dev/null | awk -v p="$pid" '$2 == p {print $1}'); do
        _fanout_kill_tree "$child"
    done
    kill -TERM "$pid" 2>/dev/null || true
}

# _fanout_timed <seconds> <command…> — run the command with a wall-clock limit.
# Returns the command's exit code, or 124 when the deadline killed it.
_fanout_timed() {
    local secs="$1"; shift
    case "$secs" in ''|*[!0-9]*) secs=0 ;; esac
    if [ "$secs" -le 0 ]; then "$@"; return $?; fi
    "$@" &
    local cpid=$! waited=0 rc=0
    while [ "$waited" -lt "$secs" ]; do
        kill -0 "$cpid" 2>/dev/null || break
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$cpid" 2>/dev/null; then
        echo "fanout: TIMEOUT after ${secs}s — killing the unit's process tree" >&2
        _fanout_kill_tree "$cpid"
        sleep 2
        kill -KILL "$cpid" 2>/dev/null || true
        wait "$cpid" 2>/dev/null || true
        return 124
    fi
    wait "$cpid" || rc=$?
    return $rc
}

# fanout_should_run <feat> <slug> — 0 (yes) iff opt-in is on, a map exists, the
# feature is NOT solo (>1 unit), and it validates+schedules cleanly. Any doubt
# returns non-zero so the caller falls back to the single-writer path.
fanout_should_run() {
    [ "${ROCKET_FANOUT:-0}" = "1" ] || return 1
    local feat="$1" slug="$2"
    if [ ! -f "features/_plan/${slug}.units.yml" ]; then
        # Diagnose the one silent-fallback trap that a live run actually hits: the
        # sizing pass names a map after the DRAFT BRIEF, the build loop looks it up
        # by the lowercased queue id, and map approval is keyed on that same
        # filename. A map under any other name is invisible here and the feature
        # quietly builds single-writer. Say so instead of saying nothing.
        local _other
        _other="$(grep -ls "^feature:[[:space:]]*${feat}[[:space:]]*$" \
                  features/_plan/*.units.yml 2>/dev/null | head -1)"
        if [ -n "$_other" ]; then
            echo "fanout: NOTE — $feat has an ownership map at $_other but the loop looks for" >&2
            echo "        features/_plan/${slug}.units.yml (and approval is keyed on that name)." >&2
            echo "        Rename it to fan out; building single-writer for now." >&2
        fi
        return 1
    fi
    local plan
    plan="$(_fanout_schedule --emit-plan "$feat" 2>/dev/null)" || return 1
    [ "$(printf '%s\n' "$plan" | grep -c .)" -gt 1 ]  # solo (1 unit) → don't fan out
}

# _deblank <val> — the scheduler emits "-" for empty fields (so a tab `read`
# can't collapse them); turn it back into the empty string.
_deblank() { [ "$1" = "-" ] && echo "" || echo "$1"; }

# _git_add_owns <owns_csv> — stage ONLY the owned paths (never `git add -A`, which
# would sweep in the .rocket/ worktree dirs as embedded repos).
_git_add_owns() {
    local p; local IFS=','
    for p in $1; do
        # NOT `[ … ] || [ … ] && continue` — that parses as (a||b)&&continue and
        # returns 1 for the common case, which under `set -e` aborts the caller.
        if [ -z "$p" ] || [ "$p" = "-" ]; then continue; fi
        git add -- "$p" 2>/dev/null || true
    done
}

# _git_add_lines <file> — stage every path listed one-per-line in <file> (the
# unit's kept-but-unpredicted files). No-op when the file is missing/empty.
_git_add_lines() {
    local f="$1" p
    [ -s "$f" ] || return 0
    while IFS= read -r p; do
        if [ -z "$p" ]; then continue; fi
        git add -- "$p" 2>/dev/null || true
    done < "$f"
}

# _fanout_exempt <path> — 0 if the path is harness-managed and must NEVER be
# reverted by ownership enforcement. Mirrors rocket.sh's guard exemptions:
# enforcement polices project CODE only; queue/state/config/secret files are
# managed by the queue guard and ship logic, and ship-rest itself writes
# FIX_QUEUE between steps.
_fanout_exempt() {
    case "$1" in
        # NOT exempt, ahead of the features/ blanket below: the map-approval records
        # are the one thing under features/ that a unit must never write. A unit that
        # could approve a map could approve its own — see guard_approvals.
        features/_plan/.approved/*) return 1 ;;
        .rocket/*|.git/*|.claude/*|features/*|.gitignore) return 0 ;;
        .env|.env.local|rocket.sh|rocket.config.sh)        return 0 ;;
        *QUEUE*.md|*INBOX*.md|*SHIPPED*.md)                return 0 ;;
    esac
    return 1
}

# _fo_norm <p> — comparable path form. Kept byte-identical in intent to
# rocket.sh's `_norm_own_path`, and DUPLICATED rather than shared because
# rocket_fanout.sh must load standalone (rocket_fanout_selfcheck.sh sources it
# without rocket.sh). Case-folded, ./-stripped, //-collapsed, /./-free, no
# trailing slash.
_fo_norm() {
    local p="$1"
    p="$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')"
    while [ "${p#./}" != "$p" ]; do p="${p#./}"; done
    while [ "$p" != "${p//\/\//\/}" ]; do p="${p//\/\//\/}"; done
    while [ "$p" != "${p//\/.\//\/}" ]; do p="${p//\/.\//\/}"; done
    while [ "${p%/}" != "$p" ] && [ "$p" != "/" ]; do p="${p%/}"; done
    printf '%s' "$p"
}

# _csv_contains <csv> <path> — 0 if path is owned.
#
# A trailing slash used to be the ONLY way to declare a directory, and the entry
# had to match the write byte for byte otherwise. Both assumptions failed against
# real, model-written maps: `owns: calclib` (no slash) meant every write under it
# read as unowned, and `Foo.py` vs `foo.py` — one file on macOS and Windows — read
# as two. On the `foreign` side of _fanout_enforce_owns that is a land-grab into a
# peer unit's file passing as "an unpredicted new file", which then MERGES.
# So: normalise both sides, then match exactly or at a path-SEGMENT boundary.
# The boundary is what still separates `src/api.py` from `src/api`.
_csv_contains() {
    local csv="$1" path="$2" item np ni
    np="$(_fo_norm "$path")"
    local IFS=','
    for item in $csv; do
        if [ -z "$item" ]; then continue; fi
        ni="$(_fo_norm "$item")"
        if [ -z "$ni" ]; then continue; fi
        if [ "$ni" = "$np" ]; then return 0; fi
        case "$np" in "$ni"/*) return 0 ;; esac
    done
    return 1
}

# _fanout_unit_prompt <uid> <owns_csv> <reads_csv> <test> <brief> — scoped build
# prompt. The ownership fence is stated in prose AND enforced by git afterward.
_fanout_unit_prompt() {
    local uid="$1" owns="$2" reads="$3" test="$4" brief="$5"
    cat <<EOF
You are building ONE unit of a fanned-out feature: ${uid}.

Feature brief: ${brief}

You OWN — and may create or modify — EXACTLY these files (nothing else):
$(printf '%s\n' "$owns" | tr ',' '\n' | sed 's/^/  - /')

You may READ but MUST NOT modify these (they are frozen contracts / patterns):
$(printf '%s\n' "$reads" | tr ',' '\n' | sed 's/^/  - /')

Any file you touch outside your OWN set will be reverted before merge. Build your
unit, then prove it with:
  ${test}

Do not edit the queue, other units' files, or shared surfaces you do not own.
EOF
}

# fanout_invoke_builder <wt> <role> <uid> <owns> <reads> <test> <brief> <log>
# Overridable for tests via ROCKET_FANOUT_BUILDER_FN. Default: real agent, run
# with cwd = the unit's worktree (confinement) and model chosen by role.
fanout_invoke_builder() {
    if [ -n "${ROCKET_FANOUT_BUILDER_FN:-}" ]; then
        "$ROCKET_FANOUT_BUILDER_FN" "$@"; return $?
    fi
    local wt="$1" role="$2" uid="$3" owns="$4" reads="$5" test="$6" brief="$7" log="$8"
    local prompt cmd raw rc=0 cost=""
    prompt="$(_fanout_unit_prompt "$uid" "$owns" "$reads" "$test" "$brief")"
    cmd="$(agent_build_cmd build write "$role")"   # role → lead=Sonnet / worker=Haiku
    raw="${log}.raw"
    # Redirections are attached OUTSIDE the `cd`, by the caller's shell. Written
    # inside (`( cd "$wt" && … >"$log" )`) they resolved relative to the WORKTREE,
    # where features/_logs/ does not exist (it is gitignored, so it is not in the
    # checkout) — the redirect failed and the builder never ran at all. Every
    # concurrent unit died instantly with "No such file or directory"; only the
    # in-place unit (cwd unchanged) worked. This is the whole reason fan-out had
    # never actually built anything with a live agent.
    # RETRY ON A TRANSIENT API FAILURE. run_tracked has always done this; fan-out
    # units never did, and they are where it matters MOST — a fan-out step is the
    # moment this harness has the most simultaneous long-lived streams open, and
    # every one of them is a chance for a socket to close. Measured on a live
    # concurrent run: two of six units came back "API Error: Connection closed
    # mid-response", both units failed, their feature blocked and the whole run
    # halted. Nothing was wrong with the build.
    #
    # Same narrow rule run_tracked uses, for the same reason: a REAL failure must
    # never be retried, because that spends the same money three times instead of
    # reaching the fix loop. And the same write-safety rule — a builder that had
    # already changed the worktree is NOT re-run, because re-running it from a
    # half-modified tree can double-apply an edit. That tree goes to the unit's own
    # test gate and enforcement, which is what they are for.
    local attempt=1 max_attempts="${AGENT_MAX_RETRIES:-3}" pre_state="" wait_s=0
    pre_state="$( cd "$wt" 2>/dev/null && git status --porcelain 2>/dev/null | sort )"
    while :; do
        rc=0
        ( cd "$wt" && printf '%s' "$prompt" | eval "$cmd" ) >"$raw" 2>>"$log" || rc=$?
        [ "$rc" -eq 0 ] && break
        [ "$attempt" -ge "$max_attempts" ] && break
        declare -F is_transient_failure >/dev/null 2>&1 || break
        is_transient_failure "$raw" "$log" || break
        if [ "$( cd "$wt" 2>/dev/null && git status --porcelain 2>/dev/null | sort )" != "$pre_state" ]; then
            echo "fanout: unit $uid — transient API failure but the worktree changed; NOT retrying" >&2
            break
        fi
        # Jitter, so units that all hit one overload do not resynchronise on the
        # way back and hammer the API together.
        wait_s=$(( ${AGENT_RETRY_BASE_SECS:-20} * attempt + RANDOM % 8 ))
        echo "fanout: unit $uid — transient API failure (attempt $attempt/$max_attempts), retrying in ${wait_s}s" >&2
        cat "$raw" >> "$log" 2>/dev/null || true
        sleep "$wait_s"
        attempt=$((attempt + 1))
    done
    # Cost: fan-out builders do NOT go through run_tracked (they run concurrently,
    # in subshells, with cwd inside a worktree), so every dollar they spend used to
    # vanish — the per-feature ceiling was blind to the ENTIRE build phase, which
    # is the phase fan-out multiplies. A subshell cannot mutate the parent's
    # accumulator, so each unit drops its own cost file and the parent folds them
    # in after the step (same pattern plan mode uses for the debate).
    if declare -F agent_extract >/dev/null 2>&1; then
        cost="$(agent_extract "$raw" "${log}.result" 2>/dev/null || true)"
    fi
    cat "$raw" >> "$log" 2>/dev/null || true
    rm -f "$raw"
    _fanout_record_cost "$uid" "$cost" "$rc" "$log"
    return $rc
}

# _fanout_record_cost <uid> <cost> <exit> <log> — one file per unit, folded in by
# _fanout_collect_costs in the parent shell. Also mirrors the call into the agent
# ledger so `scripts/rocket_cost.py` sees fan-out spend like any other call.
_fanout_record_cost() {
    local uid="$1" cost="$2" rc="$3" log="$4"
    if [ -z "${FANOUT_COST_DIR:-}" ]; then return 0; fi
    mkdir -p "$FANOUT_COST_DIR" 2>/dev/null || return 0
    printf '%s\n' "${cost:-}" > "$FANOUT_COST_DIR/$(_fanout_safe_uid "$uid").cost" 2>/dev/null || true
    if declare -F record_agent_call >/dev/null 2>&1; then
        record_agent_call "${SLUG:-?}" "fanout:${uid}" "build" "write" "$rc" "$cost" "" "$log" || true
    fi
}

# _fanout_collect_costs — fold the step's per-unit cost files into feature_cost /
# run_cost. Runs in the PARENT shell. Consumed files are removed so a later step
# cannot double-count them.
#
# "The PARENT shell" means the FEATURE's shell, and under concurrency that is a
# worker SUBSHELL whose `run_cost` dies with it. So each amount is also appended
# to ROCKET_COST_FILE, which is the only thing the scheduler can total while
# features are still in flight — and RUN_BUDGET_USD is checked against that total.
# Without this line the run budget was blind to fan-out spend, which is most of
# the money: a live two-feature run spent ~$0.95 and reported ~$0.41.
_fanout_collect_costs() {
    if [ -z "${FANOUT_COST_DIR:-}" ]; then return 0; fi
    local f c any=0
    for f in "$FANOUT_COST_DIR"/*.cost; do
        [ -e "$f" ] || continue
        c="$(cat "$f" 2>/dev/null || true)"
        rm -f "$f"
        if [ -z "$c" ]; then continue; fi
        any=1
        if [ -n "${ROCKET_COST_FILE:-}" ]; then
            printf '%s\n' "$c" >> "$ROCKET_COST_FILE" 2>/dev/null || true
        fi
        if declare -F _python >/dev/null 2>&1; then
            feature_cost="$(_python -c "print(round(float('${feature_cost:-0}') + float('$c'), 4))" 2>/dev/null || printf '%s' "${feature_cost:-0}")"
            run_cost="$(_python -c "print(round(float('${run_cost:-0}') + float('$c'), 4))" 2>/dev/null || printf '%s' "${run_cost:-0}")"
        fi
    done
    if [ "$any" = 1 ]; then
        echo "fanout: spend so far — feature \$${feature_cost:-0} / run \$${run_cost:-0}"
    fi
}

# _fanout_enforce_owns <dir> <baseline> <owns_csv> [extras_abs_file] [foreign_owns_csv]
# The load-bearing guard (§6.3). A live agent produces THREE kinds of out-of-owns
# write, and lumping them together is wrong in both directions:
#
#   (a) it changed a file that already EXISTED at the baseline → REVERT. This is
#       the only case that can corrupt the clean-union merge, and it always does.
#   (b) it created a brand-new file that falls inside ANOTHER unit's declared
#       owns → REVERT and FAIL the unit. That is a land-grab, not a surprise.
#   (c) it created a brand-new file nobody predicted and nobody claims → KEEP it.
#       A new, unclaimed path cannot collide with anything, and deleting a
#       builder's genuine output (a helper module, a fixture) silently produced
#       code that does not import. Kept paths are DECLARED in <extras_file> so
#       the caller can fail the units loudly if two of them invented the same one.
#
# Returns non-zero when it hit case (b) — the unit must not be treated as passing.
# <extras_abs_file> MUST be absolute: this runs with cwd inside the worktree.
_fanout_enforce_owns() {
    local dir="$1" base="$2" owns="$3" extras="${4:-}" foreign="${5:-}"
    if [ -n "$extras" ]; then : > "$extras"; fi
    ( cd "$dir" || exit 0
      local changed f viol=0
      changed="$( { git diff --name-only "$base" 2>/dev/null; \
                    git ls-files --others --exclude-standard 2>/dev/null; } | sort -u )"
      local IFS=$'\n'
      for f in $changed; do
          if [ -z "$f" ]; then continue; fi
          if _fanout_exempt "$f"; then continue; fi
          if _csv_contains "$owns" "$f"; then continue; fi
          if git cat-file -e "$base:$f" 2>/dev/null; then
              echo "fanout: ENFORCE — reverting out-of-scope write to existing file: $f" >&2
              git checkout "$base" -- "$f" 2>/dev/null || rm -f "$f"
          elif [ -n "$foreign" ] && _csv_contains "$foreign" "$f"; then
              echo "fanout: ENFORCE — unit created a file owned by ANOTHER unit: $f" >&2
              rm -rf "$f"
              viol=1
          else
              echo "fanout: NOTE — keeping unpredicted new file: $f" >&2
              if [ -n "$extras" ]; then printf '%s\n' "$f" >> "$extras"; fi
          fi
      done
      exit $viol )
}

# _fanout_enforce_main_tree <baseline> — the guard for writes that escape the
# worktree ENTIRELY.
#
# Observed live: of three concurrent units, two stayed in their worktrees and one
# wrote its files into the MAIN repository instead, then ran its own test there
# and reported success. Its worktree was empty, so the unit failed and nothing of
# its work merged — but the files it dropped in the parent tree were invisible to
# _fanout_enforce_owns, which only ever looks INSIDE a worktree. Harmless that
# time (untracked, uncommitted, unit already failed); a write landing on a file
# another unit owns is exactly the merge corruption §6.3 exists to prevent.
#
# During a concurrent step the main tree has NO legitimate writer — every builder
# is confined to its own worktree — so the rule is absolute: anything non-exempt
# that changed here is an escape. Revert it and say so loudly. It cannot be
# attributed to a unit (that is the nature of the escape), so this reverts rather
# than failing a specific unit; the unit itself fails on its own empty worktree.
_fanout_enforce_main_tree() {
    local base="$1" changed f n=0
    changed="$( { git diff --name-only "$base" 2>/dev/null; \
                  git ls-files --others --exclude-standard 2>/dev/null; } | sort -u )"
    local IFS=$'\n'
    for f in $changed; do
        if [ -z "$f" ]; then continue; fi
        if _fanout_exempt "$f"; then continue; fi
        echo "fanout: ESCAPE — a unit wrote OUTSIDE its worktree, into the main tree: $f" >&2
        if git cat-file -e "$base:$f" 2>/dev/null; then
            git checkout "$base" -- "$f" 2>/dev/null || true
        else
            rm -rf -- "$f" 2>/dev/null || true
        fi
        n=$((n + 1))
    done
    IFS=$' \t\n'
    if [ "$n" -gt 0 ]; then
        echo "fanout: reverted $n escaped write(s) in the main tree (worktree confinement is advisory to the agent, enforced here)" >&2
    fi
}

# _fanout_owned_changed <dir> <baseline> <owns_csv> — 0 iff the unit actually
# wrote at least one path it owns. An agent can exit 0 having produced nothing
# (empty response, refusal, a tool loop that never wrote). NOT-RUN IS NEVER PASS:
# a build unit that built nothing is a failure, not a no-op success.
_fanout_owned_changed() {
    ( cd "$1" || exit 1
      local changed f
      changed="$( { git diff --name-only "$2" 2>/dev/null; \
                    git ls-files --others --exclude-standard 2>/dev/null; } | sort -u )"
      local IFS=$'\n'
      for f in $changed; do
          if [ -z "$f" ]; then continue; fi
          if _csv_contains "$3" "$f"; then exit 0; fi
      done
      exit 1 )
}

# _fanout_unit_test <dir> <uid> <test_cmd> <log> — the per-slice test gate (§7.3):
# every unit must prove ITSELF, in its own tree, before it may merge. This was
# missing entirely — the harness took the builder's exit code as proof, which is
# the agent grading its own work. An empty test command is a map defect and FAILS
# the unit: a gate that could not run is never a pass.
_fanout_unit_test() {
    local dir="$1" uid="$2" cmd="$3" log="$4"
    if [ -z "$cmd" ] || [ "$cmd" = "-" ]; then
        echo "fanout: unit $uid declares NO test command — NOT-RUN IS NEVER PASS, failing it" >&2
        return 1
    fi
    echo "fanout: unit $uid — running its own test gate: $cmd" >&2
    ( cd "$dir" && eval "$cmd" ) >>"$log" 2>&1
}

# _fanout_merge_unit <unit_branch> <owns_csv> [extras_file] — pull ONLY the paths
# this unit legitimately produced (its owns set, plus the unpredicted-but-kept
# files it declared) from the unit branch into the feature tree. Deterministic
# clean union: owns sets are disjoint (validated in step 2) and extras were
# collision-checked by the caller, so no two units write the same path.
_fanout_merge_unit() {
    local branch="$1" owns="$2" extras="${3:-}" item
    local IFS=','
    for item in $owns; do
        if [ -z "$item" ]; then continue; fi
        git checkout "$branch" -- "$item" 2>/dev/null || true
    done
    IFS=$' \t\n'
    if [ -n "$extras" ] && [ -s "$extras" ]; then
        while IFS= read -r item; do
            if [ -z "$item" ]; then continue; fi
            git checkout "$branch" -- "$item" 2>/dev/null || true
        done < "$extras"
    fi
}

# _fanout_restore_paths <baseline> <owns_csv> [extras_file] — put the given paths
# back exactly as the baseline had them (deleting the ones it never had).
_fanout_restore_paths() {
    local base="$1" owns="$2" extras="${3:-}" p
    _fanout_restore_one() {
        if git cat-file -e "$base:$1" 2>/dev/null; then
            git checkout "$base" -- "$1" 2>/dev/null || true
        else
            rm -rf -- "$1" 2>/dev/null || true
        fi
    }
    local IFS=','
    for p in $owns; do
        if [ -z "$p" ] || [ "$p" = "-" ]; then continue; fi
        _fanout_restore_one "$p"
    done
    IFS=$' \t\n'
    if [ -n "$extras" ] && [ -s "$extras" ]; then
        while IFS= read -r p; do
            if [ -z "$p" ]; then continue; fi
            _fanout_restore_one "$p"
        done < "$extras"
    fi
}

# _fanout_alloc <slug> <uid> <baseline> — create an isolated worktree+branch from
# the baseline. Echoes "<dir>|<branch>".
_fanout_alloc() {
    local slug="$1" uid="$2" base="$3" safe dir br
    safe="$(printf '%s' "$uid" | tr '/ ' '__')"
    dir="$ROCKET_WT_DIR/${slug}__${safe}"
    br="rocket/fanout/${slug}/${safe}"
    git worktree remove --force "$dir" 2>/dev/null || true
    git branch -D "$br" 2>/dev/null || true
    git worktree add -q -b "$br" "$dir" "$base" || return 1
    printf '%s|%s\n' "$dir" "$br"
}

# _fanout_file_fix <feat> <uid> <reason> — ship-rest: record a failed independent
# slice for later instead of blocking the whole feature (§7.5).
_fanout_file_fix() {
    local feat="$1" uid="$2" reason="$3"
    printf '\n<!-- fanout auto-filed: %s slice %s failed (%s); needs a fix brief -->\n' \
        "$feat" "$uid" "$reason" >> "$FIX_QUEUE_FILE" 2>/dev/null || true
    echo "fanout: ship-rest — filed $uid to $FIX_QUEUE_FILE, continuing"
}

# fanout_cleanup <slug> — remove this feature's worktrees + branches.
fanout_cleanup() {
    local slug="$1" d b
    for d in "$ROCKET_WT_DIR"/${slug}__*; do
        [ -e "$d" ] || continue
        git worktree remove --force "$d" 2>/dev/null || rm -rf "$d"
    done
    while IFS= read -r b; do
        [ -n "$b" ] && git branch -D "$b" 2>/dev/null || true
    done < <(git for-each-ref --format='%(refname:short)' "refs/heads/rocket/fanout/${slug}" 2>/dev/null)
    git worktree prune 2>/dev/null || true
}

# _fanout_run_inplace <feat> <slug> <brief> <group-line> <baseline> — single-unit
# step (contracts freeze / integration / lone fan-out): build on the feature
# branch itself (single writer), enforce, commit. Returns non-zero only when a
# block-policy unit fails.
_fanout_run_inplace() {
    local feat="$1" slug="$2" brief="$3" line="$4" base="$5"
    local step uid role model onfail owns reads test
    IFS=$'\t' read -r step uid role model onfail owns reads test <<<"$line"
    reads="$(_deblank "$reads")"; test="$(_deblank "$test")"
    if [ "$onfail" = "-" ]; then onfail="block"; fi
    local log; log="$LOG_DIR/${slug}-fanout-$(_fanout_safe_uid "$uid")-$(ts).log"
    local extras; extras="$(mktemp)"
    echo "fanout: step $step — in-place unit $uid ($role/$model)"
    local rc=0 why=""
    # `cmd; rc=$?` would abort under `set -e` before $? is ever read.
    _fanout_timed "$UNIT_TIMEOUT_SECONDS" \
        fanout_invoke_builder "." "$role" "$uid" "$owns" "$reads" "$test" "$brief" "$log" || rc=$?
    if [ "$rc" -eq 124 ]; then why="timeout>${UNIT_TIMEOUT_SECONDS}s"
    elif [ "$rc" -ne 0 ]; then why="builder exit=$rc"; fi
    # Enforce BEFORE judging: an escaped write must be reverted even when the unit
    # is about to fail, or the failure leaves the tree dirty for the next step.
    if ! _fanout_enforce_owns "." "$base" "$owns" "$extras" ""; then
        rc=1; why="${why:-created a file owned by another unit}"
    fi
    if [ "$rc" -eq 0 ] && ! _fanout_owned_changed "." "$base" "$owns"; then
        echo "fanout: unit $uid exited 0 but wrote NOTHING it owns — failing it" >&2
        rc=1; why="produced no changes in its owns set"
    fi
    if [ "$rc" -eq 0 ]; then
        _fanout_timed "$UNIT_TIMEOUT_SECONDS" _fanout_unit_test "." "$uid" "$test" "$log" || rc=$?
        if [ "$rc" -eq 124 ]; then why="unit test timeout>${UNIT_TIMEOUT_SECONDS}s"
        elif [ "$rc" -ne 0 ]; then why="unit test failed (exit $rc)"; fi
    fi
    if [ "$rc" -ne 0 ]; then
        # A FAILED in-place unit must not leave its half-built output on the feature
        # branch. The group path already drops a failed unit's work (it simply never
        # merges the unit branch); the two paths have to agree, or "failed" means
        # something different depending on how many units a step happened to have.
        # Restore exactly the paths this unit was allowed to touch — never
        # `git reset --hard`, which would also wipe the harness/queue files written
        # since the baseline.
        _fanout_restore_paths "$base" "$owns" "$extras"
    fi
    _git_add_owns "$owns"; _git_add_lines "$extras"
    git commit -q -m "fanout unit $uid" 2>/dev/null || true
    rm -f "$extras"
    _fanout_collect_costs
    if [ "$rc" -ne 0 ]; then
        if [ "$onfail" = "ship-rest" ]; then _fanout_file_fix "$feat" "$uid" "$why"; return 0; fi
        echo "fanout: unit $uid FAILED ($why) (on_failure=block) — BLOCK feature"; return 1
    fi
    return 0
}

# _fanout_run_group <feat> <slug> <brief> <group-lines> <baseline> — concurrent
# step: isolated worktrees run in waves of MAX_PARALLEL, each enforced+committed,
# then merged (passing units only) as a clean union. Returns non-zero to block.
_fanout_run_group() {
    local feat="$1" slug="$2" brief="$3" group="$4" base="$5"
    local -a uid=() role=() onf=() owns=() reads=() test=() dir=() br=() log=() extra=()
    local line s u r m o ow rd tt
    local IFS=$'\n'
    for line in $group; do
        IFS=$'\t' read -r s u r m o ow rd tt <<<"$line"
        if [ "$o" = "-" ]; then o="block"; fi
        rd="$(_deblank "$rd")"; tt="$(_deblank "$tt")"
        local info; info="$(_fanout_alloc "$slug" "$u" "$base")" || { echo "fanout: alloc failed for $u"; return 1; }
        uid+=("$u"); role+=("$r"); onf+=("$o"); owns+=("$ow"); reads+=("$rd"); test+=("$tt")
        dir+=("${info%%|*}"); br+=("${info##*|}")
        log+=("$LOG_DIR/${slug}-fanout-$(_fanout_safe_uid "$u")-$(ts).log")
        extra+=("$(mktemp)")
    done
    IFS=$' \t\n'
    local n=${#uid[@]} i
    # Every OTHER unit's declared owns, per unit: a brand-new file that lands in a
    # peer's territory is a land-grab and fails the unit (see _fanout_enforce_owns).
    local -a foreign=()
    for ((i=0; i<n; i++)); do
        local acc="" j2
        for ((j2=0; j2<n; j2++)); do
            if [ "$j2" -ne "$i" ]; then acc="${acc:+$acc,}${owns[$j2]}"; fi
        done
        foreign+=("$acc")
    done
    echo "fanout: step $s — $n units concurrent (cap $MAX_PARALLEL): ${uid[*]}"
    local -a rc=() why=()
    for ((i=0; i<n; i++)); do rc[$i]=0; why[$i]=""; done
    # run in waves so exit codes are collected reliably (no double-wait)
    local start=0
    while [ $start -lt $n ]; do
        local end=$((start + MAX_PARALLEL)); if [ $end -gt $n ]; then end=$n; fi
        local -a wpid=() widx=()
        local j
        for ((j=start; j<end; j++)); do
            (
                # Async subshells inherit the parent EXIT/INT/TERM trap; rocket.sh
                # uses it to remove the single-instance lock. Reset it here so a
                # finishing builder can't delete the lock (or other cleanup) mid-run.
                trap - EXIT INT TERM 2>/dev/null || true
                # ONE GLOBAL POOL (design §5.5): MAX_PARALLEL is not "3 for features
                # plus more for within-feature; it is 3, total". Concurrent FEATURES
                # each hold a slot from this same pool. Without it, 2 concurrent
                # features x 3 slices = 6 live builder sessions against a stated
                # ceiling of 3 — a spend-rate and API-rate overrun (the worktree
                # fences are unaffected).
                #
                # POOL_SLOT_HELD is cleared FIRST: this subshell inherits the
                # parent's value and must never release a slot it did not take.
                # `|| true` because a slot timeout must not silently skip a build —
                # NOT-RUN IS NEVER PASS; the unit still runs, it just stops waiting.
                # Guarded on the function existing so rocket_fanout.sh keeps working
                # standalone (rocket_fanout_selfcheck.sh sources it without rocket.sh).
                if declare -F pool_acquire >/dev/null 2>&1; then
                    POOL_SLOT_HELD=""
                    pool_acquire "slice:${uid[$j]}" || true
                fi
                brc=0
                _fanout_timed "$UNIT_TIMEOUT_SECONDS" \
                    fanout_invoke_builder "${dir[$j]}" "${role[$j]}" "${uid[$j]}" \
                    "${owns[$j]}" "${reads[$j]}" "${test[$j]}" "$brief" "${log[$j]}" || brc=$?
                if [ "$brc" -eq 124 ]; then
                    echo "fanout: unit ${uid[$j]} TIMED OUT after ${UNIT_TIMEOUT_SECONDS}s — unit FAILS" >&2
                fi
                # Enforce even on failure: an escaped write must never survive into
                # the merge just because the unit also happened to error out.
                if ! _fanout_enforce_owns "${dir[$j]}" "$base" "${owns[$j]}" \
                        "${extra[$j]}" "${foreign[$j]}"; then
                    brc=1
                fi
                if [ "$brc" -eq 0 ] && ! _fanout_owned_changed "${dir[$j]}" "$base" "${owns[$j]}"; then
                    echo "fanout: unit ${uid[$j]} exited 0 but wrote NOTHING it owns — failing it" >&2
                    brc=1
                fi
                if [ "$brc" -eq 0 ]; then
                    _fanout_timed "$UNIT_TIMEOUT_SECONDS" \
                        _fanout_unit_test "${dir[$j]}" "${uid[$j]}" "${test[$j]}" "${log[$j]}" || brc=$?
                fi
                ( cd "${dir[$j]}" && _git_add_owns "${owns[$j]}" \
                  && _git_add_lines "${extra[$j]}" \
                  && git commit -q -m "fanout unit ${uid[$j]}" 2>/dev/null || true )
                # Release on EVERY path out of this subshell — `exit $brc` below is
                # the only one, including the timeout and enforcement-failure cases.
                # A slice that exits holding a slot shrinks the pool for the rest of
                # the run, and the shrinkage is invisible until throughput drops.
                if declare -F pool_release >/dev/null 2>&1; then pool_release; fi
                exit $brc
            ) &
            wpid+=("$!"); widx+=("$j")
        done
        local k
        for k in "${!wpid[@]}"; do
            if ! wait "${wpid[$k]}"; then
                rc[${widx[$k]}]=1; why[${widx[$k]}]="unit failed (build/enforce/test)"
            fi
        done
        start=$end
    done
    _fanout_collect_costs
    # Nothing but the units themselves ran during the wave, and every unit was
    # confined to a worktree — so any non-exempt change HERE escaped confinement.
    _fanout_enforce_main_tree "$base"
    # Unpredicted-path collision: two units inventing the SAME new path is the one
    # thing the disjoint-owns validation cannot have caught, because neither
    # declared it. Whichever merged last would silently win. Fail BOTH, loudly.
    local a b pa
    for ((a=0; a<n; a++)); do
        if [ ! -s "${extra[$a]}" ]; then continue; fi
        while IFS= read -r pa; do
            if [ -z "$pa" ]; then continue; fi
            for ((b=a+1; b<n; b++)); do
                if [ -s "${extra[$b]}" ] && grep -qxF -- "$pa" "${extra[$b]}" 2>/dev/null; then
                    echo "fanout: COLLISION — ${uid[$a]} and ${uid[$b]} both created the" >&2
                    echo "        unpredicted path '$pa'. Neither owns it; failing both." >&2
                    rc[$a]=1; why[$a]="unpredicted-path collision on $pa"
                    rc[$b]=1; why[$b]="unpredicted-path collision on $pa"
                fi
            done
        done < "${extra[$a]}"
    done
    # merge: passing units always; failed units per policy
    local blocked=0
    for ((i=0; i<n; i++)); do
        if [ "${rc[$i]}" -eq 0 ]; then
            _fanout_merge_unit "${br[$i]}" "${owns[$i]}" "${extra[$i]}"
        elif [ "${onf[$i]}" = "ship-rest" ]; then
            _fanout_file_fix "$feat" "${uid[$i]}" "${why[$i]:-unit failed}"
        else
            echo "fanout: unit ${uid[$i]} FAILED — ${why[$i]:-unit failed} (on_failure=block)"; blocked=1
        fi
    done
    for ((i=0; i<n; i++)); do rm -f "${extra[$i]}"; done
    # _fanout_merge_unit already staged the owned paths via `git checkout -- `;
    # commit exactly those (no `git add -A`, which would grab the worktree dirs).
    git commit -q -m "fanout: merge step $s of $feat (${uid[*]})" 2>/dev/null || true
    return $blocked
}

# fanout_build <feat> <slug> <brief> — orchestrator. Returns 0 on success, or
# non-zero to BLOCK the feature (caller halts). Execution across steps is strictly
# ordered (freeze-first); only WITHIN a concurrent step do builders run parallel.
fanout_build() {
    local feat="$1" slug="$2" brief="$3"
    local plan
    plan="$(_fanout_schedule --emit-plan "$feat")" || { echo "fanout: no valid plan for $feat — BLOCK"; return 1; }
    mkdir -p "$ROCKET_WT_DIR" "$LOG_DIR" 2>/dev/null || true
    # Per-unit cost drop box. Absolute (mktemp -d), because units write to it from
    # subshells whose cwd is a worktree.
    FANOUT_COST_DIR="$(mktemp -d 2>/dev/null || echo "")"
    local baseline; baseline="$(git rev-parse HEAD)"
    echo "fanout: building $feat from baseline ${baseline:0:8} (MAX_PARALLEL=$MAX_PARALLEL, unit timeout ${UNIT_TIMEOUT_SECONDS}s)"
    local steps step rcx=0
    steps="$(printf '%s\n' "$plan" | awk -F'\t' 'NF{print $1}' | sort -un)"
    for step in $steps; do
        local group; group="$(printf '%s\n' "$plan" | awk -F'\t' -v s="$step" '$1==s')"
        local nunits; nunits="$(printf '%s\n' "$group" | grep -c . || true)"
        if [ "$nunits" -eq 1 ]; then
            _fanout_run_inplace "$feat" "$slug" "$brief" "$group" "$baseline" || rcx=1
        else
            _fanout_run_group "$feat" "$slug" "$brief" "$group" "$baseline" || rcx=1
        fi
        if [ "$rcx" -ne 0 ]; then
            fanout_cleanup "$slug"
            _fanout_drop_cost_dir
            return 1
        fi
        baseline="$(git rev-parse HEAD)"   # advance/refreeze baseline after each step
    done
    fanout_cleanup "$slug"
    _fanout_drop_cost_dir
    echo "fanout: $feat complete — merged to ${baseline:0:8}"
    return 0
}

# _fanout_drop_cost_dir — fold in anything left (a unit that failed after paying)
# and remove the drop box. Money spent by a FAILED unit still counts.
_fanout_drop_cost_dir() {
    _fanout_collect_costs
    if [ -n "${FANOUT_COST_DIR:-}" ]; then rm -rf "$FANOUT_COST_DIR"; fi
    FANOUT_COST_DIR=""
}
