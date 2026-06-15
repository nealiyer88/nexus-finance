# /prune — Learnings Consolidation

Read CC-LEARNINGS.md. Identify WORKED and TRICK entries that have
appeared 3+ times (same underlying pattern, different titles).

For each cluster of 3+:
1. Extract the canonical pattern.
2. Determine if it belongs in .claude/rules/01-nexus-finance-v1.md
   (project-specific guardrail) or as a new skill in
   .claude/skills/user/ (reusable across projects).
3. Draft the addition. Show the diff.
4. Apply the change.

After consolidation, move pruned entries to a new section at the
bottom of CC-LEARNINGS.md titled
"## ARCHIVED (pruned to rules/skills on {date})"
Do not delete them.

If CC-LEARNINGS.md exceeds 200 entries, warn about context bloat
and recommend aggressive prune pass.

If .claude/rules/01-nexus-finance-v1.md exceeds 250 lines after
additions, flag that the rules file needs trimming (per project
convention: trim after shipped code makes inline schemas redundant).
