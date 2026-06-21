#!/usr/bin/env bash
# ── Adapter: Google Gemini CLI (gemini) ──────────────────────────────────────
# Reference config — VERIFY flags against your version: `gemini --help`.
# Non-interactive: gemini reads the prompt from stdin. `--yolo` auto-approves all
# tool actions (needed for unattended file edits). No machine-readable cost → cost
# gate degrades gracefully. Gemini has no strict read-only tool mode, so read-only
# steps rely on the loop's guard_revert (which hard-reverts any file they touch).
# ─────────────────────────────────────────────────────────────────────────────

agent_model_for_tier() {
  case "$1" in
    plan)    echo "${MODEL_PLAN:-gemini-2.5-pro}" ;;
    narrate) echo "${MODEL_NARRATE:-gemini-2.5-flash}" ;;
    *)       echo "${MODEL_BUILD:-gemini-2.5-pro}" ;;
  esac
}

agent_build_cmd() {  # <tier> <mode> — prompt piped via stdin
  local tier="$1" mode="$2" model; model="$(agent_model_for_tier "$tier")"
  local base="gemini -m $model"
  case "$mode" in
    # write: --yolo auto-approves edits/commands (unattended).
    write)    echo "$base --yolo" ;;
    # readonly: no --yolo → Gemini won't auto-apply edits; guard_revert backstops.
    readonly) echo "$base" ;;
    *)        echo "$base" ;;
  esac
}

agent_extract() {  # gemini prints plain text; no cost JSON
  local raw="$1" dest="$2"
  [ -n "$dest" ] && cp "$raw" "$dest" 2>/dev/null || true
  echo ""   # cost unknown → graceful degrade
}

agent_narrate() {  # <prompt>
  gemini -m "$(agent_model_for_tier narrate)" <<<"$1" 2>/dev/null || true
}
