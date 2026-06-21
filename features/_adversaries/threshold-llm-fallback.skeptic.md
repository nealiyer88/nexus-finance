# Adversary-Skeptic: threshold-llm-fallback

## Gaps in the Brief

- [BLOCKING] Threshold band boundary ambiguity: brief writes `≥0.90`, `0.70–0.90`, `0.50–0.70`, `<0.50` but does not state inclusivity at the lower bound. A score of exactly 0.70 is in BOTH the QUEUE_FOR_REVIEW range and the LLM_FALLBACK range. Same for exactly 0.90 and exactly 0.50. Stage 3 clamps to [0,1] and float arithmetic regularly produces exact 0.90 (alias_boost + max ratios on identical strings). Without a single inequality rule across all four zones every reviewer will get an inconsistent answer.
- [BLOCKING] No graph in V1: cluster-conflict detection presupposes a populated `entity_edges` / canonical store with prior B↔C contradictions. Stage 6 (resolution / graph update) is NOT shipped per `features/SHIPPED.md`. Fresh DB means there are zero prior canonicals to conflict with — so "A matched B and C above threshold" can only mean two ScoredMatches for the SAME entity against TWO candidates in this run. The brief never defines "matched" in graph terms vs. in-run scored terms. Without disambiguation, the implementation will either no-op (graph empty) or invent a Stage-6-shaped graph write.
- [BLOCKING] Tie-break in conflict demotion is undefined: "A matches B with 0.95 and C with 0.95 — which gets demoted?" Brief says "demote lower-confidence" — with equal scores there IS no lower. Without a deterministic tie-break (e.g., ascending canonical_id like Stage 3 does), runs are nondeterministic and tests will flake.
- [HIGH] Empty / None candidate set: brief never says what disposition an entity with zero ScoredMatches receives. NO_MATCH? Or undefined? Same for entity whose top match is exactly 0.50.
- [HIGH] No tenant_id on disposition rows or training rows: Disposition dataclass and training capture both lack tenant_id. Per rules §10 every query and write must be tenant-scoped.
- [HIGH] Unicode and whitespace in redaction: brief says "strip names" but does not specify Unicode normalization (NFKC), zero-width spaces, RTL marks, or homoglyph names ("Cеnlar" with Cyrillic e). Naive regex strip leaks.
- [NOTE] `candidates_ranked` shape unspecified: list of canonical_ids? Tuples of (id, score, breakdown)? Inline vs. by-reference matters for storage size and downstream queue UI.

## Scope Creep Risk

- Claude Code will build a full `LLMClient` abstraction with retry, backoff, circuit-breaker — none of which the brief requests. Pre-empt: instruct the build prompt to use the `anthropic` SDK directly with a hard-coded timeout and no retry layer in V1.
- It will add a "while we're here" `ApprovalQueue` table or writer because disposition outputs feel like they want persistence. Brief explicitly out-of-scopes the queue UI but is silent on the queue STORAGE — CC will fill the silence.
- It will reach into `core/matching/scoring.py` to "harmonize" the SignalBreakdown into the Disposition record and refactor field names.
- It will scaffold a `tests/fixtures/llm_responses/` mock harness and a generic `MockClaudeClient` class.
- It will add structured logging beyond what's specified, including the redacted prompt itself, recreating the PII leak in logs.
- It will create a `core/matching/thresholds.py` constants module duplicating values that already live in confidence/weights.

## Dependency Assumptions

- Stage 6 graph update: NOT SHIPPED. Cluster conflict detection that reads `entity_edges` will return zero conflicts on a fresh DB. The brief should explicitly state in-run cluster conflict semantics, not graph-stored.
- `ANTHROPIC_API_KEY` in `.env.example`: brief marks DONE but I cannot verify from SHIPPED.md. Should be confirmed before build.
- No `core.matching.confidence` module shipped per SHIPPED.md — brief references the section-5 constants but defines them inline. The brief should either ship `confidence.py` first or commit to inline constants.
- `entity_category` ('organization' vs 'person') is the discriminator the redaction code needs. It lives on `canonical_entities` per `get_canonical_name_and_category`. But `NormalizedEntity` carries `entity_type`, not `entity_category`. The brief never specifies which field drives `redact_org` vs `redact_person` dispatch — and they don't have the same vocabulary.

## Missing Tests

- Score == 0.70, 0.90, 0.50 exact-boundary tests (catches `>=` vs `>` slip).
- Two candidates with identical scores test (catches nondeterministic conflict demotion).
- Person entity whose attributes dict contains a name in a free-form field like `notes`, `description`, or `display_name` — redaction must walk the whole payload, not just hard-coded fields.
- QB `Class` code containing person initials (e.g. `Commercial.NI.Sands` where NI = Neal Iyer) — does redaction preserve "structural patterns" without leaking initials?
- Concatenated tokens: `external_id="cenlar-fsb-neal-iyer-2026"` — string-level scrub required.
- LLM returns malformed JSON / 429 / timeout / empty string / `{match: true, confidence: 1.5}` (out of range) / `{match: "yes"}` (wrong type). Test each.
- Two parallel calls for the same entity_pair within the same run — does training capture dedupe or double-write?
- Training row with tenant_id = NULL vs a real tenant: GDPR delete path must work for both.
- Cluster conflict test where A matches B at 0.95 and the graph has NO B↔C edge yet — current "in-graph contradiction" check must not silently pass.
- Test that the redacted prompt string contains zero substrings from `entity.normalized_name` AND zero substrings from `entity.attributes.values()` for person entities.

## Security / Audit Findings

- [BLOCKING] Free-form attribute leak path: rules §10 says person entities — strip ALL identifiers including names, emails, IDs. The redaction examples in the brief only mention "role" and "category" but `NormalizedEntity.attributes` is a free-form dict that QB/RUDDR connectors populate with display names, emails, employee_ids, notes, descriptions. Redaction MUST walk attributes recursively, not just whitelist top-level fields.
- [BLOCKING] Class-code initials leak: QB Class codes like `Commercial.GenAI.Sands` are explicitly "preserved as structural pattern" in the brief's example. If `Class` ever contains owner initials (common in consultancy COA conventions), structural preservation = identifier leak. Rules §10 person redaction is absolute — but org-entity redaction passes class strings through verbatim. An owner's initials embedded in a Class code are PII when re-identifiable.
- [BLOCKING] Training data store schema undefined: brief says "store (entity_pair, redacted_context, ...)" but does not specify table, tenant_id column, retention, or deletion path. Rules §10 audit log is append-only (no UPDATE/DELETE). That contradicts GDPR right-to-erasure for the redacted-but-still-customer-derived training corpus. Must specify: separate `llm_training_log` table with explicit tenant_id, deletion on customer offboarding, and a documented carve-out from the append-only audit rule.
- [HIGH] Cost runaway: no per-tenant daily LLM cap, no per-run cap, no batching. A bad blocking run that surfaces 500 candidates each scoring 0.55 will fire 500 Claude API calls. Brief's "<15% of entities" is a target, not a circuit breaker.
- [HIGH] Concurrency / idempotency: two pipeline runs scoring the same entity pair concurrently will produce two LLM calls and two training rows. No idempotency key in the spec. Minimum: `(tenant_id, entity_signature, candidate_canonical_id, redacted_prompt_hash)` unique index.
- [HIGH] Tenant scoping on `Disposition` writes and training writes is not specified — same risk as rules §10 baseline.
- [HIGH] LLM response logging: if Claude's `reasoning` text echoes a name it inferred from "structural patterns," storing it in `LLM_reasoning` reintroduces PII through the model's own output. Need an outbound scrub on the response, not just the prompt.
- [NOTE] `ANTHROPIC_API_KEY` must not be loaded at module import time — only at call site — to avoid leaking via stack traces and to allow tests without the key.

## Rules File Contradictions

- None outright. The brief stays inside §11 NOT-SCOPE (no self-hosted LLM, no agent framework, no Neo4j). But §10 redaction is under-implemented as specified — see Security findings above — and the training-data append-only-vs-GDPR tension needs an explicit carve-out documented in the rules file or the brief, not assumed.

## Position Statement

Do NOT ship this brief without addressing the BLOCKING items. The four blockers — threshold boundary inclusivity, fresh-DB cluster conflict semantics, deterministic tie-break, and PII leak paths through free-form attributes plus Class-code initials plus training data retention — are not edge cases; the first three break correctness on the first run, and the last is a regulatory-grade leak that ships PII to Anthropic the moment a customer's chart of accounts contains owner initials. Add explicit cost caps, idempotency keys, tenant_id on every row, and a documented GDPR carve-out for the training log, then the HIGH items become survivable.
