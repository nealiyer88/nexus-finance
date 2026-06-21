#!/usr/bin/env bash
# ── Adapter: Cursor CLI (cursor-agent) ───────────────────────────────────────
# Reference config — VERIFY flags against your version: `cursor-agent --help`.
# Headless mode: `cursor-agent -p` (print). Reads the prompt from stdin. `--force`
# auto-approves edits/commands (unattended). No machine-readable cost surfaced here
# → cost gate degrades gracefully. No strict read-only mode, so read-only steps rely
# on the loop's guard_revert (which hard-reverts any file they touch).
# ─────────────────────────────────────────────────────────────────────────────

agent_model_for_tier() {
  case "$1" in
    plan)    echo "${MODEL_PLAN:-sonnet-4.5}" ;;
    narrate) echo "${MODEL_NARRATE:-sonnet-4.5}" ;;
    *)       echo "${MODEL_BUILD:-sonnet-4.5}" ;;
  esac
}

agent_build_cmd() {  # <tier> <mode> — prompt piped via stdin
  local tier="$1" mode="$2" model; model="$(agent_model_for_tier "$tier")"
  local base="cursor-agent -p --output-format text -m $model"
  case "$mode" in
    # write: --force auto-approves edits/commands (unattended).
    write)    echo "$base --force" ;;
    # readonly: no --force; guard_revert backstops any stray write.
    readonly) echo "$base" ;;
    *)        echo "$base" ;;
  esac
}

agent_extract() {  # cursor-agent (text format) prints the answer; no cost JSON here
  local raw="$1" dest="$2"
  [ -n "$dest" ] && cp "$raw" "$dest" 2>/dev/null || true
  echo ""   # cost unknown → graceful degrade
}

agent_narrate() {  # <prompt>
  cursor-agent -p --output-format text -m "$(agent_model_for_tier narrate)" <<<"$1" 2>/dev/null || true
}
