---
name: test-author
description: Write the missing tests when the coverage gate fails. Use to close a real coverage shortfall by pinning behavior that matters — not to inflate a percentage. Also use when shipped code has untested branches that would fail silently.
---

# test-author

**Tier: lead (`MODEL_BUILD`).**

This skill is a thin entry point. The full contract — inputs, job, hard rules, and the
exact output format the harness parses — lives in **one** place:

    .claude/agents/test-author.md

Read that file and follow it exactly. Nothing is restated here on purpose.

## Why the contract is not duplicated here

`rocket.sh` builds this agent's prompt by reading `.claude/agents/test-author.md` directly, and
`install.sh` upgrades that file as harness-owned. A second copy of the rules in this file
would be a second source of truth for a contract the harness parses by exact line format —
and the copy nobody runs is the copy that silently goes stale. The whole reason this
harness has an `--upgrade` path at all is that fixes made in one copy never reached the
others.

So: this file exists to make the agent invocable by name and to say where the contract is.
The contract itself has exactly one home.
