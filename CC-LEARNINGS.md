# Nexus Finance — Claude Code Learnings

Append-only pattern library. Reviewed via /prune command.

Three entry types:
- WORKED: prompts/patterns that produced unusually good output
- FAILED: prompts that produced bad output with specific failure mode
- TRICK: workflow techniques discovered

Promotion threshold: any pattern appearing 3+ times becomes a
rule or skill via /prune.

---

## 2026-05-23 — deterministic-blocking (Rocket)

### WORKED — Skeptic adversary catches scalar-return collision silencer at DESIGN time
Brief said `lookup_alias(normalized_name) -> canonical_id or None`. Skeptic adversary flagged: a scalar return forces silent collision resolution (two canonicals with the same alias value would force the lookup function to pick one). Decision: change to `list[str]`. Stage 1 then declines (returns None) on multi-hit. Caught BEFORE any code was written. Pattern: when a deterministic-match function returns a scalar identifier, ask the skeptic "what if two rows match?" — if "pick one" is unacceptable, change the return shape, not the docstring.

### FAILED — First-pass test seeding hid a dedupe bug
`lookup_alias_exact` returned two rows for the same canonical_id when both `canonical_name` and `entity_aliases.value` equaled the query. The test suite I wrote never seeded a canonical with `canonical_name=X` AND an alias `value=X` on the same canonical — so the bug wasn't caught by the 34 first-pass tests. Code reviewer caught it. **Lesson:** when one function pulls rows from two SQL sources (UNION-shaped), at least one test must seed a case where the SAME logical entity appears via BOTH sources. Add to test-design checklist.

### TRICK — Reconciliation prompt picks winners, not compromises
Wrote the hardened design as a numbered list of disagreements with explicit DECISIONS ("Engineer wins on Metaphone. Design wins on Ngram."). Avoids the "compromise that pleases nobody" failure mode. Each decision cites the specific reason the losing adversary is wrong on THIS specific point — not a global ranking of the adversaries.

### TRICK — End-to-end fixture test that exercises the intra-system filter
When the same fixture data seeds both system_references AND is used to drive the query, the intra-system filter (correctly) excludes the candidate, causing the test to fail. Fix: seed only ONE side's sysrefs (e.g., RUDDR only) and route the query through the OTHER side's raw records. Documents the filter behavior implicitly and tests the cross-source thesis the V1 design depends on.

---
