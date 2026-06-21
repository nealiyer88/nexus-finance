# Adversary-Design: threshold-llm-fallback

## Brief Recap

Stage 4 turns a tuple of `ScoredMatch` (the Stage 3 output frozen in
`core/matching/types.py`) into a `Disposition` by applying the rules-file
threshold bands and, critically, by detecting cluster conflicts via the
existing graph store. Stage 5 handles the 0.50–0.70 band by calling Claude
with a category-aware redacted prompt, captures training data on every call,
and never auto-approves the LLM verdict. Three new files
(`disposition.py`, `llm_fallback.py`, `redaction.py`) plus two test modules
land the V1 thesis from rules §1 ("LLM fallback: Claude API, redacted,
Tier 3 only (<15% of entities, 0.50–0.70 confidence band). Never
auto-approves.").

## Success Criteria Verifiability

- File-existence rows (`disposition.py`, `llm_fallback.py`,
  `redaction.py` with named functions): VERIFIABLE — `python -c "import
  core.matching.disposition; assert disposition.apply_thresholds"`.
- "Threshold application produces correct disposition for all 4 zones":
  VERIFIABLE via parametrized pytest on the four bands 0.49 / 0.69 / 0.89
  / 0.95.
- "Cluster conflict detection flags contradictory matches": VERIFIABLE —
  seed two canonicals with an `entity_edges` row that distinguishes them
  (the schema is already ridden by `count_shared_graph_neighbors`), assert
  the lower-confidence match demotes to `QUEUE_FOR_REVIEW`.
- "LLM fallback calls Claude API with redacted context": NEEDS REWRITE —
  "calls Claude API" is hard to assert deterministically in CI. Rewrite:
  "`llm_assess` accepts an injected `LLMClient` protocol; default
  implementation targets Claude; tests use an in-process fake and assert
  the prompt passed in matches `redact_org`/`redact_person` output
  byte-for-byte." That matches the dependency-injection pattern already
  used by `connectors/quickbooks.py` (`HTTPClient`, `TokenStore`,
  `RateLimiter` injection).
- "LLM result disposition is always QUEUE_FOR_REVIEW, never AUTO_APPROVE":
  VERIFIABLE — assert across a fake LLM that returns `match=True,
  confidence=0.99`.
- "Person entity redaction contains zero names, emails, or employee IDs":
  NEEDS REWRITE — "zero names" is undefined. Rewrite: "for every fixture
  person-pair, the redacted prompt string contains none of `entity.name`,
  `entity.aliases`, `system_references[*].email`, or
  `system_references[*].employee_id`, asserted by substring containment
  over the fixture set."
- "Training data captured for every LLM assessment": VERIFIABLE — assert
  one row written per call, with schema-checked fields.
- "Expected Tier 3 usage: <15% of fixture entities": VERIFIABLE — run the
  pipeline over the loaded fixture (44 + 45 = 89 entities per SHIPPED log)
  and assert `count(LLM_FALLBACK) / count(scored) < 0.15`.

## Scope vs. Problem Statement

The brief's thesis is that disposition decouples auto-approve from review
queue, and that Tier 3 LLM is a narrow band. The shipped Stage 3 output
(`ScoredMatch` with `score`, `signal_breakdown`, `graph_evidence`,
`category_pair`, `weight_profile_id`) carries everything Stage 4 needs to
produce a `Disposition` without additional reads. Cluster conflict
detection over `entity_edges` works at V1 scale (<500 entities,
~46 + 45 canonicals in fixtures); the graph queries already exist
(`_neighbors`, `count_shared_graph_neighbors`). Scope solves the problem.

## Defended Decisions

1. **Cluster conflict detection lives in Stage 4, not Stage 6.** Right
   because the brief catches contradictions *before* graph writes, so the
   review queue surfaces the conflict to a human while the graph stays
   clean. The plausible alternative — detect at write time in Stage 6 —
   forces Stage 6 to either roll back or write tentative edges, both of
   which mutate the store of truth before human approval. At <500 V1
   entities, doing the set-intersection on `_neighbors(A) & _neighbors(B)`
   per ambiguous candidate is microseconds; the >50K re-evaluation
   threshold (rules §11) is two orders of magnitude away.

2. **Three sibling modules under `core/matching/` rather than one fat
   `engine.py`.** Right because `engine.py` (rules §12 owner of the
   pipeline) orchestrates; the disposition / llm_fallback / redaction
   split mirrors what already shipped — `deterministic.py`, `blocking.py`,
   `scoring.py`, `weights.py`. The plausible alternative — fold redaction
   into `llm_fallback.py` — couples the security boundary to the
   network-bound module, making the "no PII leaves the process" assertion
   harder to unit-test in isolation. Separate `redaction.py` lets the test
   suite import it without touching the LLM client at all.

## Refinements

1. **Make `redaction.py` produce a typed `RedactedPrompt(category_pair,
   text, leak_check_tokens)` dataclass, and have `llm_assess` re-run the
   leak check on the prompt bytes immediately before send.** No new files,
   no new deps, frozen-dataclass pattern already used in `types.py`. This
   strengthens the brief's "no PII leaks" assertion from a unit-test
   property to a runtime invariant — if a future caller bypasses
   `redact_org`/`redact_person`, the second-pass guard refuses.

2. **Persist the training-data row inside the same SQLite transaction as
   the `Disposition` write.** The brief already requires capture from Day
   1; tying it to the disposition transaction means a training row exists
   iff a disposition exists, so V2+ fine-tuning data cannot drift from
   the audit log. No new tables outside the brief's footprint — the row
   is appended via the existing `sqlite3.Connection` already threaded
   through scoring (`score_pair(..., conn)`).

## Position Statement

Ship this brief with the two refinements above. The brief correctly
identifies the highest-stakes code in the feature (redaction), correctly
forbids LLM auto-approval, and correctly sites cluster-conflict detection
*before* graph writes. The refinements harden the security boundary at
runtime and bind training data capture to the disposition transaction —
both preserve scope, both use shipped infrastructure, both make the
success criteria mechanically verifiable.
