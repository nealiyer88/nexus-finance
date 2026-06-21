# Nexus Finance

Python 3.10+. Run tests: `pytest tests/ -x --tb=short` from repo root.

snake_case functions, PascalCase classes, UPPER_CASE constants.
Do not modify `.claude/settings.json` outside an explicit harness-config task.
Files under `.claude/hooks/` and `.claude/agents/` are synced from the upstream `rocket-loop` repo — modify them only via a deliberate Rocket-infra sync, not as a side effect of feature work.
Do not modify TEMPLATE.md.
All log writes (SHIPPED.md, DEBUG.md, RUN_LOG.md, PROMPT_LOG.md, CC-LEARNINGS.md) are append-only.
Feature branches only — never commit directly to main.