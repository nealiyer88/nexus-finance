---
name: dedup-scout
description: After a parallel wave lands, find helpers that two or more units built independently — duplicate utilities, parallel implementations of the same transform, near-identical constants. Reports duplicates for a human or lead to act on; does not refactor.
---

# dedup-scout

**Tier: worker (`MODEL_WORKER`).**

This skill is a thin entry point. The full contract — inputs, job, hard rules, and the
exact output format the harness parses — lives in **one** place:

    .claude/agents/dedup-scout.md

Read that file and follow it exactly. Nothing is restated here on purpose.

## Why the contract is not duplicated here

`rocket.sh` builds this agent's prompt by reading `.claude/agents/dedup-scout.md` directly, and
`install.sh` upgrades that file as harness-owned. A second copy of the rules in this file
would be a second source of truth for a contract the harness parses by exact line format —
and the copy nobody runs is the copy that silently goes stale. The whole reason this
harness has an `--upgrade` path at all is that fixes made in one copy never reached the
others.

So: this file exists to make the agent invocable by name and to say where the contract is.
The contract itself has exactly one home.
