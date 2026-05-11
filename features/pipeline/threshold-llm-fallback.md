# Feature Brief: Threshold + Disposition + LLM Fallback (Pipeline Stages 4–5)

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

Scored candidates need disposition: auto-approve, queue for review, escalate to LLM, or flag as new entity. Without this stage, every match requires human review regardless of confidence. Stage 4 applies thresholds and catches cluster conflicts (entity A matched both B and C, but B≠C). Stage 5 handles the <15% of entities scoring 0.50–0.70 via Claude API with mandatory PII redaction. LLM results NEVER auto-approve — they always route to human review.

---

## Scope

### In Scope

- Create `core/matching/disposition.py` implementing Stage 4:
  - Apply confidence thresholds: ≥0.90 AUTO_APPROVE, 0.70–0.90 QUEUE_FOR_REVIEW, 0.50–0.70 LLM_FALLBACK, <0.50 NO_MATCH
  - **Cluster conflict detection:** If entity A matched both B and C above threshold and B≠C in graph, demote lower-confidence match to QUEUE_FOR_REVIEW
  - For entities with multiple candidates above 0.70, surface all ranked by score
  - Output: `Disposition(canonical_id, action, score, signal_breakdown, candidates_ranked)`

- Create `core/matching/llm_fallback.py` implementing Stage 5:
  - Invoked only when Stage 4 disposition = LLM_FALLBACK (score 0.50–0.70)
  - **Mandatory redaction protocol:**
    - Organizational entities: strip names, preserve category metadata and structural patterns
    - Person entities: NO names, emails, employee IDs reach the LLM. Only role, category, token overlap count, score
  - Claude API call with structured output: `{match: bool, confidence: float, reasoning: str, signals_examined: list}`
  - LLM result NEVER auto-approves — disposition is QUEUE_FOR_REVIEW with LLM reasoning attached
  - **Training data capture:** Store `(entity_pair, redacted_context, category_pair, LLM_reasoning, signals)` for every LLM call

- Create `core/matching/redaction.py`:
  - `redact_org(entity_pair)` → redacted prompt preserving category metadata
  - `redact_person(entity_pair)` → stricter redaction, no identifying information
  - Unit tests verifying no PII leaks in redacted output

- **Test suite:** `tests/test_disposition.py`, `tests/test_llm_fallback.py`
  - Assert: scores ≥0.90 auto-approve
  - Assert: scores 0.70–0.90 queue for review
  - Assert: scores 0.50–0.70 route to LLM fallback
  - Assert: cluster conflicts detected and demoted
  - Assert: LLM results never produce AUTO_APPROVE disposition
  - Assert: redacted prompts contain zero entity names for person entities
  - Assert: training data captured for every LLM call

### Out of Scope

- Approval queue UI — separate feature (feature 11)
- Graph writes on approval — separate feature (feature 10)
- Self-hosted LLM — V2+. V1 uses Claude API only
- LLM parallel assessment on all candidates — V2+. V1 is fallback only (<15%)

---

## Success Criteria

- [ ] `core/matching/disposition.py` exists with `apply_thresholds()` function
- [ ] `core/matching/llm_fallback.py` exists with `llm_assess()` function
- [ ] `core/matching/redaction.py` exists with `redact_org()` and `redact_person()` functions
- [ ] Threshold application produces correct disposition for all 4 zones
- [ ] Cluster conflict detection flags contradictory matches
- [ ] LLM fallback calls Claude API with redacted context
- [ ] LLM result disposition is always QUEUE_FOR_REVIEW, never AUTO_APPROVE
- [ ] Person entity redaction contains zero names, emails, or employee IDs
- [ ] Training data structure captured for every LLM assessment
- [ ] Expected Tier 3 usage: <15% of fixture entities
- [ ] `pytest tests/test_disposition.py tests/test_llm_fallback.py` passes

---

## Dependencies

- [ ] Pairwise scoring (feature 8) — input is ScoredMatch
- [ ] Entity store (feature 7) — cluster conflict checks query graph
- [ ] `.env.example` has ANTHROPIC_API_KEY (DONE)

---

## Estimated Complexity

**Rating:** M

**Rationale:** Three files, but disposition logic is straightforward (threshold application + set intersection for conflicts). LLM fallback requires careful redaction — the security implications of PII leakage make this the highest-stakes code in the feature. Claude API integration is well-documented.

---

## PROJECT CONTEXT

### Confidence Thresholds

```python
AUTO_APPROVE_THRESHOLD = 0.90
SURFACE_THRESHOLD = 0.70
LLM_FALLBACK_THRESHOLD = 0.50
NO_MATCH_THRESHOLD = 0.50
AMOUNT_TOLERANCE_PCT = 0.02  # min(TotalAmt * 0.02, $500)
```

### Redaction Examples

**Org entity redaction (Stage 5 prompt):**
"Entity A from accounting category has class code pattern X.Y.Z; Entity B from PSA category has project code pattern ABC-XYZ-123. Are these the same organization?"

**Person entity redaction (Stage 5 prompt):**
"Person entity from PSA category with [ROLE] role; person entity from accounting category with inverted name format. Token overlap: 2/2 tokens match. Score: 0.64. Are these the same person?"

### V1 Hard Constraints

- LLM fallback: Claude API only, <15% of entities
- LLM NEVER auto-approves — always routes to human review
- Person entity PII: NO names, emails, employee IDs reach any external API
- Training data capture from Day 1 — load-bearing decision for V2+ fine-tuning

### Relevant Spec Sections

- Section 9: Stage 4 — Threshold + Disposition + Cluster Conflict Detection
- Section 9: Stage 5 — LLM Assessment (redaction protocol, training data capture)
