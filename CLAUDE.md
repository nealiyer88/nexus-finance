# Nexus Finance

Python 3.10+, via the project venv. Run tests: `.venv/bin/python -m pytest tests/ -x --tb=short` from repo root.
Bare `pytest` is NOT on PATH, and `.venv/bin/pytest` fails to put the repo root on `sys.path` — every `from core...` / `from connectors...` import in tests/ then fails to collect. Use the `python -m` form.
Rebuild the venv with: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt` (system `python3` is 3.9.6, which this project does not support).

snake_case functions, PascalCase classes, UPPER_CASE constants.
Do not modify `.claude/settings.json` outside an explicit harness-config task.
Files under `.claude/hooks/` and `.claude/agents/` are synced from the upstream `rocket-loop` repo — modify them only via a deliberate Rocket-infra sync, not as a side effect of feature work.
Do not modify TEMPLATE.md.
All log writes (SHIPPED.md, DEBUG.md, RUN_LOG.md, PROMPT_LOG.md, CC-LEARNINGS.md) are append-only.
Feature branches only — never commit directly to main.