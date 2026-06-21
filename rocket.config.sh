#!/usr/bin/env bash
# ── rocket.config.sh — nexus-finance per-project Rocket Loop config ───────────
# Sourced (NOT executed) by rocket.sh at startup. Set variables only — no side
# effects, no `set -e`. Any value left unset falls back to the rocket.sh default.
# Upstream template: rocket-loop/templates/rocket.config.example.sh
# ──────────────────────────────────────────────────────────────────────────────

# ── Agent (drives the loop) ───────────────────────────────────────────────────
AGENT="claude"

# ── Models ────────────────────────────────────────────────────────────────────
# Tiering: PLAN = high-leverage upstream reasoning (debate + reconcile);
# BUILD = expensive code work (prompt-gen, build, reviews, fix, gates);
# NARRATE = cheap plain-English commentary for tail -f.
MODEL_PLAN="opus"
MODEL_BUILD="sonnet"
MODEL_NARRATE="claude-haiku-4-5-20251001"

# ── Per-feature cost ceiling ──────────────────────────────────────────────────
FEATURE_BUDGET_USD=50

# ── Queue + feature selection ─────────────────────────────────────────────────
QUEUE_FILE="FEATURE_QUEUE.md"
# Nexus uses integer feature IDs with an optional lowercase suffix for sub-features
# (1, 2, ..., 8, 8a, 9, ..., 17). The suffix lets a retrofit / patch feature land
# between the integer it amends and the next one without renumbering downstream.
FEATURE_ID_REGEX='[0-9]+[a-z]?'
MAX_FEATURES=3

# ── Tests ─────────────────────────────────────────────────────────────────────
# python3 explicitly — the system has both `python` (3.9.x) and `python3`
# (3.10+); the project pins to 3.10+ via CLAUDE.md.
TEST_CMD="python3 -m pytest tests/ -x --tb=short"
BASELINE_COLLECT_CMD="python3 -m pytest tests/ --collect-only -q"
PYTHON_BIN="python3"

# ── Branch pinning ────────────────────────────────────────────────────────────
ROCKET_BRANCH_DEFAULT="main"

# ── Repo-mutation guard (cleans untracked files left by read-only subprocesses) ─
# Nexus codebase top-level source dirs.
GUARD_CLEAN_PATHS="core connectors api dashboard db tests scripts"

# ── Optional: QB validation gate (Phase 4b) ───────────────────────────────────
# Nexus uses QuickBooks Online (one of two V1 connectors). Flip to 1 and add
# .claude/skills/qb-validation/SKILL.md to enable keyword-triggered QB-MCP
# data-truth checks before ship. Off by default until the skill exists.
QB_GATE=0

# ── Optional: post-ship hook ──────────────────────────────────────────────────
POST_SHIP_HOOK=""

# ── Optional: ship bookkeeping ────────────────────────────────────────────────
PLAN_LOG_MARKER=""
PROMPTS_PATH=""
