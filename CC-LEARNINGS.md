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

## 2026-06-14 — pairwise-scoring (Rocket)

### WORKED — Skeptic adversary cuts three signals before any code lands
Brief listed `name_inversion_score: 0.95` direct-override, `email +0.10` soft signal, and `shared_transaction_context` graph bonus. Skeptic flagged each: inversion already done by Stage 0 (override would override a 1.0 token_set match with a 0.95 ceiling), email already resolved by Stage 1 (Stage 3 never sees a matching-email pair by construction), no transactions table in V1 schema (signal returns silent 0 forever). Engineer concurred. All three cut from V1 scope BEFORE the first line of code. Estimated ~150 LOC + 6 tests of dead code avoided. Pattern: when a new stage references work the previous stage already did, ask if the signal is *reachable in production* — not just *implementable*.

### FAILED — Defaulting `get_aliases` to return canonical_name silently double-counted the perfect-match signal
First-pass `get_aliases(canonical_id)` SQL returned every alias row, including the V1 canonical-write convention's `(value=canonical_name, source='canonical')` seed alias. Alias-boost path then compared entity_name vs that seed alias — when they matched, +0.15 boost stacked on top of an already-perfect weighted sum. Inversion test passed for the wrong reason (alias seeded equal to canonical_name → boost fires; intended path was a token-reordered alias). Caught by code reviewer at iteration 1. **Lesson:** when a read function and a downstream consumer both look at the same column for "evidence of similarity," explicitly choose which side filters out the trivial-equal case. Add a SQL-level filter + a defensive guard in the consumer; never trust one of the two to do it.

### TRICK — Synthesized non-match pair generator must filter shared tokens
Random pairing of QB entity X with RUDDR entity Y (X≠Y) for the "non-matches score <0.50" test failed: "Atlas Media Group" / "Meridian Capital Group" scored 0.564 because of the shared "Group" token (token_set ratio inflated by common business noise word). The correct interpretation per the spec: those pairs LEGITIMATELY land in the 0.50–0.70 SURFACE band (human review), not below NO_MATCH. Fix: filter the random sample to pairs with EMPTY token intersection. Documents the spec interpretation: "known non-matches" means truly disjoint strings, not "any non-match including shared-noise-word coincidences."

### TRICK — Mid-band fixture for "single application" assertions
A boost-applies-at-most-once test cannot use a perfect-match candidate name: the weighted sum (0.85) + single boost (0.15) saturates the clamp, and a stacked-boost bug (e.g. +0.45) also clamps to 1.0 — the test passes for both correct and broken behaviors. Pick a candidate name that scores ~0.65 weighted sum, so single-boost yields ~0.80 and stacked-boost yields ~1.00 (clamp visible). Made the test meaningful in one substitution. Generalizes: any "X applies at most once" assertion needs a fixture that lives below whatever ceiling would otherwise mask the difference.

### TRICK — Hardened design as an algorithm spec, not just a decision log
The PSA-shortcode heuristic was originally described in the brief in one English sentence ("if candidate ≤4 chars from PSA, check abbreviation of incoming name"). Hardened design pinned the exact algorithm: (i) gate on category pair, (ii) identify shortcode side by category+length, (iii) match via prefix-of-token OR initialism. The build prompt's `_check_psa_abbreviation` reads as a direct translation. Pattern: ambiguous English in a brief → step-numbered pseudocode in the hardened design → near-mechanical Python implementation. Reviewer-flagged-bugs in this section dropped to 1 (the PSA-category gating on candidate side).

---

## 2026-06-20 — threshold-llm-fallback (Rocket, multi-session resume)

### TRICK — RESUME_HERE.md as session-bridge marker
Feature 9 paused after Phase 3 build commit with a deliberate RESUME_HERE.md committed to the branch — full state inventory + exact resume steps + delete-at-ship instruction. Six days later, fresh reviewer subagents picked up at Phase 4 with zero re-do work. The note's self-delete clause prevents it from outliving its purpose. Pattern reusable for any multi-session Rocket build.

### WORKED — Phase-4 reviewers spawned against an explicit build commit, not a branch diff
Resumed session passed `40e1335` (the build commit hash) into both reviewer prompts as the diff scope. The branch carried infrastructure merges (rocket-loop sync, v4 spec retrofit, Spec column) inherited via main-merges between pause and resume — narrowing to the build-commit-only diff kept reviewer focus on feature 9's code, not 30 files of unrelated infra deltas. Pattern: when resuming a paused branch, always give reviewers the precise hash range, not `main..HEAD`.

### WORKED — Default-deny redaction signatures (skeptic-driven)
`redact_org` / `redact_person` accept only allow-listed primitives — `category`, `entity_type`, shaped codes, role strings, counts, score, `forbidden_tokens`. No `NormalizedEntity`, no `raw_record`, no `dict`. Code review confirmed: architecturally impossible to leak PII via these functions because the type signatures don't permit it. Pattern: when a function MUST NOT leak something, encode that prohibition in the signature, not in the body.

### TRICK — Pre-send AND post-response leak checks
`leak_check` runs twice — once on the redacted prompt before the API call (defense in depth against future code changes), once on the LLM's `reasoning` field after the response. Out-of-bound text from the model gets replaced with `REDACTED_REASONING_PLACEHOLDER` and the scrubbed reasoning is what persists to the training-data row. The persisted DB row therefore contains audit-safe text regardless of model misbehavior.

### FAILED — Postgres migration uses destructive `DROP TABLE IF EXISTS CASCADE`
`db/migrations/002_llm_training_data.sql:14` opens with `DROP TABLE IF EXISTS llm_training_data CASCADE` while the SQLite mirror (`002_llm_training_data_sqlite.sql`) uses safe `CREATE TABLE IF NOT EXISTS`. Append-only audit log contract (rule §10) is undermined if a dev re-runs migration 002 manually. Captured as a follow-up nit in SHIPPED.md; should be normalized to additive pattern in next infra pass.
