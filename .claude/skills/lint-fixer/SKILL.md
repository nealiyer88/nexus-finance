---
name: lint-fixer
description: Mechanically remediate lint and formatting violations after the lint or format gate fails. Use for style, import order, spacing, naming, and unused-symbol cleanup only — never for behavior changes, and never for lint errors that require restructuring code.
---

# lint-fixer

**Tier: worker (`MODEL_WORKER`).**

This skill is a thin entry point. The full contract — inputs, job, hard rules, and the
exact output format the harness parses — lives in **one** place:

    .claude/agents/lint-fixer.md

Read that file and follow it exactly. Nothing is restated here on purpose.

## Why the contract is not duplicated here

`rocket.sh` builds this agent's prompt by reading `.claude/agents/lint-fixer.md` directly, and
`install.sh` upgrades that file as harness-owned. A second copy of the rules in this file
would be a second source of truth for a contract the harness parses by exact line format —
and the copy nobody runs is the copy that silently goes stale. The whole reason this
harness has an `--upgrade` path at all is that fixes made in one copy never reached the
others.

So: this file exists to make the agent invocable by name and to say where the contract is.
The contract itself has exactly one home.
