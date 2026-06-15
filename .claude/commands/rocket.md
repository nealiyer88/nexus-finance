# Rocket — Autonomous Feature Build Loop

Process the feature brief at the path provided. Run all six phases
in order. Do not skip phases. Do not add scope.

Read .claude/rules/01-nexus-finance-v1.md before starting.

## Phase 1: ADVERSARIES

Spawn three adversary subagents against the feature brief:

1. Spawn .claude/agents/adversary-design.md — capture output.
2. Spawn .claude/agents/adversary-skeptic.md — capture output.
3. Spawn .claude/agents/adversary-engineer.md — capture output.

Then RECONCILE: Read all three outputs. Identify where they
genuinely disagreed vs. talked past each other. For each
disagreement, make a DECISION (not a compromise — pick a side
and state why the losing side is wrong on this specific point).

Produce a HARDENED DESIGN document containing:
- Each disagreement and which adversary's position won, with why
- Scope adjustments (anything added or removed from the brief)
- Implementation constraints the engineer surfaced
- Risks the skeptic raised that are real (vs phantom risks dismissed)
- Test cases the skeptic or engineer identified that the brief missed

Write to features/_adversaries/{brief-slug}.md

## Phase 2: PROMPT-GEN

Read the hardened design from Phase 1. Apply the cc-prompt-engineering
skill (SCOPE framework). Generate the build prompt.

The prompt MUST contain ALL of these sections. If any is missing,
regenerate before proceeding:
- SITUATION (files to read, current state, what shipped prior)
- TASK (one verb, one deliverable)
- FILE PATHS (every file created or modified, exact paths)
- CONVENTIONS (naming, imports, style from rules file)
- TEST COMMAND (exact pytest invocation that must pass)
- ACCEPTANCE CRITERIA (structural checks from brief Success Criteria)
- NON-GOALS (what not to build, from brief Out of Scope)
- EXECUTION (ONE STEP AT A TIME)

Save to features/_prompts/{brief-slug}.cc-prompt.md

## Phase 3: BUILD

Execute the generated prompt. Create feature branch
feature/{brief-slug} from current HEAD. Build exactly what the
prompt specifies. No scope expansion. No adjacent refactoring.
Write tests for every public function created.

## Phase 4: REVIEW

Two review agents evaluate the build independently:

1. Spawn .claude/agents/reviewer-qa.md — receives the diff and
   runs the test suite. Captures output.
2. Spawn .claude/agents/reviewer-code.md — receives the diff and
   reviews for quality, security, and spec compliance. Captures output.

Combine both reports into a REVIEW VERDICT:
- Status: PASS (both agents green) or FAIL (either agent red)
- If FAIL: numbered list of specific issues, each tagged
  [QA] or [CODE-REVIEW], each with the file and line reference

If PASS → proceed to Phase 6.
If FAIL → proceed to Phase 5.

## Phase 5: FIX

Spawn .claude/agents/fixer.md with:
- The REVIEW VERDICT from Phase 4 (the numbered issue list)
- The current diff
- The original build prompt from Phase 2

The fixer addresses each numbered issue. After fixes are applied,
return to Phase 4 (re-run both reviewers on the updated code).

Max 3 iterations of Phase 4 → Phase 5. After 3 failures:
a. Update FEATURE_QUEUE.md: status → BLOCKED
b. Append error summary to features/DEBUG.md
c. Append to CC-LEARNINGS.md: FAILED entry with the failure
   mode, the review verdicts from each iteration, and what
   the fixer tried
d. git add . && git commit -m "Blocked: {feature-name} (3 review cycles exhausted)"
e. Exit with non-zero status.

## Phase 6: SHIP + RECORD

On PASS from Phase 4:
1. Append to features/SHIPPED.md (date, feature, branch, commit, notes)
2. Update FEATURE_QUEUE.md status → SHIPPED
3. Append run summary to features/RUN_LOG.md with header:
   "## Run: {date} (Rocket, autonomous)"
4. Write audit entry to PROMPT_LOG.md (prompt-log skill format)
5. Append to CC-LEARNINGS.md:
   - WORKED entry if any adversary suggestion materially improved
     the build
   - TRICK entry if a novel pattern emerged
   - FAILED entry if fix iterations were needed (capture what broke
     and what fixed it, even if ultimately green)
6. git add . && git commit -m "Ship {feature-name} [Rocket]"
7. git push origin feature/{brief-slug}

## RULES

- Never skip Phase 1. The adversary debate is mandatory.
- Never skip Phase 4. Review is mandatory even if you are confident.
- Never modify TEMPLATE.md.
- Never merge to main — feature branches only.
- All log writes are append-only (SHIPPED.md, DEBUG.md, RUN_LOG.md,
  PROMPT_LOG.md, CC-LEARNINGS.md).
- Do not add scope beyond what the feature brief specifies.
- If a test that passed before your changes now fails, that is a
  regression. It must be fixed. It counts toward the 3 retry cap.
- The BUILD agent and the REVIEW agents must have separate contexts.
  The builder cannot review its own work. The reviewers cannot
  rationalize away their own findings.
