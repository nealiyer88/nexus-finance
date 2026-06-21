# Rocket Builder (Phase 3 — BUILD ONLY)

You are the BUILDER. Your ONLY job is to build the feature, test it, and commit the code.
You do NOT review, you do NOT ship, you do NOT touch the feature queue, the spec, the
learnings file, or `SHIPPED.md`. The outer `rocket.sh` runs the reviewers, gates on
PASS/FAIL, and performs the ship itself (in bash, deterministically). Stay in your lane.

## Inputs (from $ARGUMENTS)
- `slug` — feature identifier (e.g. "b5")
- `build_prompt` — path to the CC build prompt from Phase 2
- `feature_brief` — path to the original feature brief
- `log_dir` — path to the log directory

---

## UNATTENDED — never pause
There is NO human in this session. NEVER ask for confirmation, branching choices, or
acknowledgment. Make the safe default and execute end-to-end. Pausing to ask = silent
no-op exit (nothing gets built). The only legitimate stop is the 3-attempt build-failure
rule in `agents/builder.md`.

## LIVE PROGRESS — narrate as you work
A human follows `features/ROCKET_LIVE.md` live. Append a timestamped line (via Bash
`echo`, this file is under `features/`, not `.claude/`) at: build start, and tests green.
```bash
echo "### $(date +%H:%M:%S) · $ARGUMENTS.slug · 🔨 building" >> features/ROCKET_LIVE.md
echo "### $(date +%H:%M:%S) · $ARGUMENTS.slug · 🧪 tests green (N passed)" >> features/ROCKET_LIVE.md
```
Aggregate-only — no sensitive identifiers or dollar amounts.

## GIT STAGING
NEVER `git add -A` / `git add .` — the working tree may hold unrelated changes. Stage ONLY
the build prompt's Create/Modify **code** paths, via explicit `git add <path> ...`.
Do NOT stage anything under `features/_logs/` — that dir is gitignored; the build manifest +
test log are the LOCAL audit trail (reviewers + do_ship read them from disk, not git). Staging
them by path bypasses `.gitignore` and leaks build logs (secret/PII-leak risk) into history.

## GIT BRANCH — do NOT create or switch branches
Commit on the CURRENT branch. NEVER run `git checkout -b`, `git switch -c`, `git branch <new>`,
or `git checkout <other>`. rocket.sh manages the branch; if you spawn a `feature/<x>` branch your
commits strand off the loop's working branch (rocket pins them back, but don't rely on it). Just
`git commit` where you are.

---

## BUILD STEPS

1. Read `.claude/agents/builder.md` — internalize its rules.
2. Read the build prompt at `$ARGUMENTS.build_prompt` and the brief at `$ARGUMENTS.feature_brief`.
3. Build EXACTLY what the prompt specifies. Write tests for every public function.
   No protected-file modifications. No sensitive values in terminal output.
4. Run the project's test command (the build prompt names it; default `python -m pytest tests/ -x --tb=short`) and fix the CODE until green.
5. Capture the raw test log (reviewers' source of truth):
   `python -m pytest tests/ --tb=long --no-header | tee $ARGUMENTS.log_dir/$ARGUMENTS.slug-pytest.log`
   (substitute your test runner if not pytest).
6. Write a build manifest to `$ARGUMENTS.log_dir/$ARGUMENTS.slug-build-manifest.md`:
   files created (with line counts), files modified (diff summary), test pass count,
   and a short "what was built / what was deferred" note for the reviewers.
7. Commit the code (scoped staging, never `git add -A`):
   `git commit -m "feat: $ARGUMENTS.slug — built by rocket"`.

That's it. STOP after the commit. Do NOT run reviewers, do NOT update the queue, do NOT
ship. `rocket.sh` takes over from here.
