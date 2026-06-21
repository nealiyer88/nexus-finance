#!/usr/bin/env bash
# ── Adapter: OpenAI Codex CLI (codex) ────────────────────────────────────────
# Reference config — VERIFY flags against your version: `codex exec --help`.
# `codex exec` is the non-interactive mode; it reads the prompt from stdin.
# No machine-readable cost → cost gate degrades gracefully (logs "cost: n/a").
# Read-only is enforced both by --sandbox read-only AND the loop's guard_revert.
# ─────────────────────────────────────────────────────────────────────────────

agent_model_for_tier() {
  case "$1" in
    plan)    echo "${MODEL_PLAN:-gpt-5-codex}" ;;
    narrate) echo "${MODEL_NARRATE:-gpt-5-codex}" ;;
    *)       echo "${MODEL_BUILD:-gpt-5-codex}" ;;
  esac
}

agent_build_cmd() {  # <tier> <mode> — prompt piped via stdin
  local tier="$1" mode="$2" model; model="$(agent_model_for_tier "$tier")"
  local base="codex exec -m $model"
  case "$mode" in
    # write: workspace-write sandbox, no approval prompts (unattended).
    write)    echo "$base --full-auto" ;;
    # readonly: read-only sandbox, never prompt. guard_revert backstops regardless.
    readonly) echo "$base --sandbox read-only -a never" ;;
    *)        echo "$base --sandbox read-only -a never" ;;
  esac
}

agent_extract() {  # codex prints the final message as plain text; no cost JSON
  local raw="$1" dest="$2"
  [ -n "$dest" ] && cp "$raw" "$dest" 2>/dev/null || true
  echo ""   # cost unknown → graceful degrade
}

agent_narrate() {  # <prompt>
  codex exec -m "$(agent_model_for_tier narrate)" --sandbox read-only -a never <<<"$1" 2>/dev/null || true
}
