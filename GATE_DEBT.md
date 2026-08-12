# Gate debt

Checks that WORK but that the existing code does not pass. They are
switched off so they cannot block new features on old problems.
Fix one, re-run `python3 scripts/detect_gates.py --write`, and it
becomes a real gate from then on.

## test

    .venv/bin/pytest tests/ -x --tb=short

warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))  [configured anyway — see note]

## format

    .venv/bin/ruff format . --extend-exclude .claude,features,scripts/rocket_schedule.py,scripts/rocket_cost.py,scripts/detect_gates.py

34 files would be reformatted, 30 files already formatted

## lint

    .venv/bin/ruff check . --extend-exclude .claude,features,scripts/rocket_schedule.py,scripts/rocket_cost.py,scripts/detect_gates.py

[*] 300 fixable with the `--fix` option (9 hidden fixes can be enabled with the `--unsafe-fixes` option).

## typecheck

    .venv/bin/mypy . --exclude '\.claude' --exclude 'features' --exclude 'scripts/rocket_schedule\.py' --exclude 'scripts/rocket_cost\.py' --exclude 'scripts/detect_gates\.py'

Found 1 error in 1 file (errors prevented further checking)

## coverage

    .venv/bin/pytest --cov=. --cov-report=term

warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))

## depaudit

    .venv/bin/pip-audit

Found 15 known vulnerabilities in 5 packages

