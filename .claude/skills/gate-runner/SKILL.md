---
name: gate-runner
description: Run the harness quality gates for a scope and return a compact structured verdict. Use whenever a work unit or feature needs pre-merge, post-integration, or full gate results — and you want the answer without dragging test, lint, or coverage output into context.
---

# gate-runner

**Tier: worker (`MODEL_WORKER`).**

This skill is a thin entry point. The full contract — inputs, job, hard rules, and the
exact output format the harness parses — lives in **one** place:

    .claude/agents/gate-runner.md

Read that file and follow it exactly. Nothing is restated here on purpose.

## Why the contract is not duplicated here

`rocket.sh` builds this agent's prompt by reading `.claude/agents/gate-runner.md` directly, and
`install.sh` upgrades that file as harness-owned. A second copy of the rules in this file
would be a second source of truth for a contract the harness parses by exact line format —
and the copy nobody runs is the copy that silently goes stale. The whole reason this
harness has an `--upgrade` path at all is that fixes made in one copy never reached the
others.

So: this file exists to make the agent invocable by name and to say where the contract is.
The contract itself has exactly one home.
