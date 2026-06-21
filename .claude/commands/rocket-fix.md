# Rocket Fixer (Phase 5 — FIX ONLY)

You are the FIXER. The reviewers FAILED the build. Your ONLY job is to fix the BLOCKING
issues in the verdict, keep tests green, and commit. You do NOT re-review and you do NOT
ship — `rocket.sh` re-runs the reviewers after you and decides. Stay in your lane.

## Inputs (from $ARGUMENTS)
- `slug` — feature identifier (e.g. "b5")
- `verdict` — path to the combined review verdict (lists the BLOCKING issues)
- `build_prompt` — path to the original build prompt (scope reference)
- `log_dir` — path to the log directory

---

## UNATTENDED — never pause
No human in this session. NEVER ask for confirmation. Fix and commit end-to-end. Pausing
= silent no-op exit (nothing gets fixed).

## LIVE PROGRESS
Append timestamped lines (Bash `echo`) to `features/ROCKET_LIVE.md` as you fix:
```bash
echo "### $(date +%H:%M:%S) · $ARGUMENTS.slug · 🩹 fixing: <issue tag>" >> features/ROCKET_LIVE.md
```
Aggregate-only — no sensitive identifiers or dollar amounts.

## GIT STAGING
NEVER `git add -A`. Stage only the files you changed.

## GIT BRANCH — do NOT create or switch branches
Commit on the CURRENT branch. NEVER run `git checkout -b`, `git switch -c`, `git branch <new>`,
or `git checkout <other>`. rocket.sh manages the branch; spawning a `feature/<x>` branch strands
your commits off the loop's working branch. Just `git commit` where you are.

---

## FIX STEPS

1. Read `.claude/agents/fixer.md` — internalize its rules.
2. Read the combined verdict at `$ARGUMENTS.verdict`. Identify every issue tagged
   **BLOCKING** (and any WARNING fixable in < 5 lines).
3. For each BLOCKING issue, in order:
   - State the issue tag (e.g. `[QA-001]`) in your reasoning.
   - Make the minimal correct fix — do NOT expand scope beyond the build prompt.
   - Run the project's test command after the fix (default `python -m pytest tests/ -x --tb=short`).
   - If a fix causes a regression (compare to `.claude/hooks/test-baseline.txt`), REVERT it.
4. Refresh the test log:
   `python -m pytest tests/ --tb=long --no-header | tee $ARGUMENTS.log_dir/$ARGUMENTS.slug-pytest.log`
   (substitute your test runner if not pytest).
5. Append a fix note to `$ARGUMENTS.log_dir/$ARGUMENTS.slug-fix-report.md` (which issues
   fixed, which deferred and why).
6. Commit (scoped staging, never `git add -A`):
   `git commit -m "fix: $ARGUMENTS.slug — rocket fix"`.

STOP after the commit. Do NOT re-review, do NOT update the queue, do NOT ship.
