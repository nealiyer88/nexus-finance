#!/usr/bin/env bash
# ── Adapter: Claude Code (claude) — the reference adapter ─────────────────────
# Fully wired: emits total_cost_usd (cost gate active) and supports a true
# read-only tool allowlist. See adapters/README.md for the contract.
# Requires the `_python` helper from rocket.sh (sourced after it is defined).
# ─────────────────────────────────────────────────────────────────────────────

# Tier policy: plan = adversaries, reconcile, reality-check, prompt-gen
# (Opus class); build = builder, fixer, both reviewers (Sonnet);
# narrate = live narration only (Haiku).
agent_model_for_tier() {
  case "$1" in
    plan)    echo "${MODEL_PLAN:-claude-opus-5}" ;;
    narrate) echo "${MODEL_NARRATE:-claude-haiku-4-5-20251001}" ;;
    *)       echo "${MODEL_BUILD:-claude-sonnet-5}" ;;   # build (default)
  esac
}

# Fan-out roles on the BUILD tier (design §2): lead = Sonnet (judgment slices,
# integration, fixer); worker = Haiku (provably-mechanical slices only).
agent_model_for_role() {
  case "$1" in
    worker) echo "${MODEL_WORKER:-${MODEL_NARRATE:-claude-haiku-4-5-20251001}}" ;;
    *)      echo "${MODEL_BUILD:-claude-sonnet-5}" ;;   # lead / unset (default)
  esac
}

agent_build_cmd() {  # <tier> <mode> [role] — prompt piped via stdin by rocket.sh.
  # Optional 3rd arg selects model by fan-out ROLE (lead/worker) instead of tier;
  # worktree confinement is handled by the caller (cwd = the unit's worktree).
  local tier="$1" mode="$2" role="${3:-}" model
  if [ -n "$role" ]; then model="$(agent_model_for_role "$role")"
  else model="$(agent_model_for_tier "$tier")"; fi
  local base="claude -p --model $model --output-format json"
  case "$mode" in
    write)    echo "$base --permission-mode bypassPermissions" ;;
    # Read-only: NO bypassPermissions + a read+execute allowlist. Write/Edit are then
    # absent AND the sandbox blocks Bash file writes, while Bash still RUNS commands
    # (pytest/tsc/git diff). A deny-list under bypass would be a NO-OP.
    readonly) echo "$base --allowedTools Read --allowedTools Bash --allowedTools Grep --allowedTools Glob" ;;
    *)        echo "$base" ;;
  esac
}

agent_extract() {  # <raw_file> <result_dest> — write answer text, echo cost (USD) or ""
  _python - "$1" "$2" <<'PY'
import json, sys
raw = sys.argv[1]
dest = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    d = json.load(open(raw, encoding="utf-8"))
except Exception:
    print(""); raise SystemExit(0)
if dest:
    try:
        open(dest, "w", encoding="utf-8").write(d.get("result", "") or "")
    except Exception:
        pass
c = d.get("total_cost_usd", "")
print(c if c not in (None, "") else "")
PY
}

agent_narrate() {  # <prompt> — short plain-text summary, best-effort
  # Tool-less + capped: the narrator summarizes instruction-shaped artifacts (build
  # prompts). A Read-only allowlist (no bypass) means it cannot write or run Bash even
  # if it tries to EXECUTE the artifact, and the hard budget cap kills a runaway in
  # seconds. Pairs with the DATA-not-instructions framing rocket.sh puts in the prompt.
  claude -p "$1" --model "$(agent_model_for_tier narrate)" --output-format text \
    --allowedTools "Read" --max-budget-usd 0.25 2>/dev/null || true
}
