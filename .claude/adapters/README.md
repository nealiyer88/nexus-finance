# Agent adapters

The Rocket Loop is **agent-agnostic**. `rocket.sh` never calls a specific AI CLI directly —
it calls an *adapter* selected by `AGENT` in `rocket.config.sh` (`AGENT="claude"` by default).
The adapter translates the loop's generic request — *"run this prompt at this tier with this
access mode"* — into one concrete CLI invocation.

Everything else in the loop (phase orchestration, the git-based `guard_revert`/`guard_queue`
read-only enforcement, blind-critic QA, NOT-RUN≠PASS gates, branch pinning, the queue) is
already agent-agnostic and unchanged by your choice of agent.

## Selecting an adapter

In `rocket.config.sh`:
```sh
AGENT="claude"     # → sources .claude/adapters/claude.sh
# Set MODEL_PLAN / MODEL_BUILD / MODEL_NARRATE to model ids YOUR agent understands.
```
`rocket.sh` sources `.claude/adapters/${AGENT}.sh`. Ship adapters live in this repo's
`adapters/`; `install.sh` copies them into a project's `.claude/adapters/`.

## The contract — every adapter defines three functions

```sh
agent_build_cmd <tier> <mode>   # prints the CLI command + flags (NO prompt — it's piped via stdin)
agent_extract   <raw> <dest>    # writes the agent's answer text to <dest>; echoes call cost in USD, or "" if unknown
agent_narrate   <prompt>        # echoes a short plain-text summary (best-effort; may echo nothing)
```

- **tier** ∈ `plan` | `build` | `narrate` → map to `MODEL_PLAN` / `MODEL_BUILD` / `MODEL_NARRATE`.
- **mode** ∈ `write` | `readonly` | `plain`:
  - `write` — the agent may edit files (build / fix / gate). Use the CLI's auto-approve / bypass mode.
  - `readonly` — the agent may read + run commands but must NOT edit (reviews, adversaries, prompt-gen).
    Use the CLI's read-only sandbox if it has one; **either way the loop's `guard_revert` hard-reverts
    any file a read-only step leaves behind**, so an agent without a true read-only mode is still safe.
  - `plain` — a simple text completion (narration); no tools needed.

### How the prompt reaches the CLI
`rocket.sh` writes the prompt to a temp file and runs your command with that file on **stdin**
(`cmd < promptfile`), and also exports its path as `$AGENT_PROMPT_FILE`. Prefer stdin (dodges the
~32 KB command-line cap on Windows). If your CLI only accepts the prompt as an argument, reference
`"$AGENT_PROMPT_FILE"` inside `agent_build_cmd`.

### Cost / budget (graceful degradation)
If `agent_extract` echoes a USD number, it accrues toward `FEATURE_BUDGET_USD` (the hard ceiling).
If it echoes `""` (the agent reports no cost — true for most non-Claude CLIs today), the loop logs
`cost: n/a` and the budget gate simply never trips. Everything else works.

## Bundled adapters

| `AGENT` | CLI | Cost gate | Native read-only | Notes |
|---|---|---|---|---|
| `claude` | `claude` (Claude Code) | ✅ `total_cost_usd` | ✅ `--allowedTools` | reference adapter, fully wired |
| `codex`  | `codex` (OpenAI Codex CLI) | ✖ | ✅ `--sandbox read-only` | VERIFY flags for your version |
| `gemini` | `gemini` (Gemini CLI) | ✖ | ➖ (guard_revert backstop) | VERIFY flags for your version |
| `cursor` | `cursor-agent` (Cursor CLI) | ✖ | ➖ (guard_revert backstop) | VERIFY flags for your version |

> The non-Claude adapters are **reference configs**: CLI flags drift between releases, so confirm
> them against `--help` for your installed version and tweak the adapter file. The loop's safety
> (read-only enforcement, queue guard, branch pinning) does NOT depend on getting those flags
> perfect — it's enforced by the harness in git, not by the agent's own sandbox.

## Adding your own
Copy `claude.sh` to `.claude/adapters/<name>.sh`, implement the three functions for your CLI,
set `AGENT="<name>"`. That's the whole integration.
