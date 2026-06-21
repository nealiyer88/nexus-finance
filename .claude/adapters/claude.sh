#!/usr/bin/env bash
# ── Adapter: Claude Code (claude) — the reference adapter ─────────────────────
# Fully wired: emits total_cost_usd (cost gate active) and supports a true
# read-only tool allowlist. See adapters/README.md for the contract.
# Requires the `_python` helper from rocket.sh (sourced after it is defined).
# ─────────────────────────────────────────────────────────────────────────────

agent_model_for_tier() {
  case "$1" in
    plan)    echo "${MODEL_PLAN:-opus}" ;;
    narrate) echo "${MODEL_NARRATE:-claude-haiku-4-5-20251001}" ;;
    *)       echo "${MODEL_BUILD:-sonnet}" ;;   # build (default)
  esac
}

agent_build_cmd() {  # <tier> <mode> — prompt is piped via stdin by rocket.sh
  local tier="$1" mode="$2" model; model="$(agent_model_for_tier "$tier")"
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
  claude -p "$1" --model "$(agent_model_for_tier narrate)" --output-format text 2>/dev/null || true
}
