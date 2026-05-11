# Feature Brief: Matcher Orchestrator (End-to-End Pipeline)

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

Stages 0–6 exist as individual modules but nothing wires them together. The matcher needs an orchestrator that takes raw connector output and runs the complete pipeline: normalize → deterministic match → blocking → scoring → threshold/LLM fallback → resolution. Without this, each stage must be called manually in sequence. The orchestrator is the `match()` function from spec Section 9 — the single entry point for entity resolution.

---

## Scope

### In Scope

- Create `core/matching/engine.py` implementing the `match()` function:
  ```python
  def match(incoming: NormalizedEntity, registry: EntityRegistry) -> MatchResult
  ```
  - Calls stages sequentially: Stage 1 → if unresolved → Stage 2 → Stage 3 → Stage 4 → if LLM_FALLBACK → Stage 5 → Stage 6
  - Returns `MatchResult` with: canonical_id (or None), confidence, match_type (deterministic|scored|llm|human|new), signal_breakdown, audit_entry

- Create `core/ingestion/pipeline.py` implementing batch processing:
  - `run_ingestion(connector: ConnectorInterface, tenant_id: str)` → pulls entities from connector, normalizes, matches each against registry
  - Handles both org and person entity types
  - Produces summary: total entities, auto-approved count, queued count, LLM fallback count, new entities count
  - Sequential processing in V1 (no parallel/async)

- **Test suite:** `tests/test_engine.py`, `tests/test_pipeline.py`
  - End-to-end test: load all 91 fixture entities into empty graph via pipeline
  - Assert: first run produces mostly QUEUE_FOR_REVIEW (graph is empty, no aliases)
  - Simulate approvals, re-run pipeline
  - Assert: second run produces AUTO_APPROVE for previously approved entities
  - Assert: match type distribution roughly matches spec expectations

### Out of Scope

- Celery/Redis async queue — V1 runs synchronously
- Webhook-triggered ingestion — V1 uses manual/scheduled trigger
- Multi-connector orchestration (run QB + RUDDR in sequence) — that's the ingestion worker (feature 14)
- Dashboard integration — separate features

---

## Success Criteria

- [ ] `core/matching/engine.py` exists with `match()` function
- [ ] `core/ingestion/pipeline.py` exists with `run_ingestion()` function
- [ ] End-to-end test: 91 entities processed through full pipeline without errors
- [ ] First run on empty graph: 0 auto-approvals (expected — no prior aliases)
- [ ] After simulating 44 approvals: re-run produces ≥40 auto-approvals (aliases now exist)
- [ ] Match type distribution tracked: deterministic, scored, llm, new
- [ ] Pipeline summary reports entity counts per disposition
- [ ] `pytest tests/test_engine.py tests/test_pipeline.py` passes

---

## Dependencies

- [ ] All pipeline stages shipped: deterministic+blocking (7), scoring (8), threshold+LLM (9), resolution (10)
- [ ] Both connectors shipped (5, 6) — pipeline needs real connector output
- [ ] Normalizer (3) — first step in pipeline

---

## Estimated Complexity

**Rating:** M

**Rationale:** Orchestration logic is straightforward — sequential function calls. Complexity is in the end-to-end test: seeding an empty graph, running the full pipeline, simulating approvals, re-running, and verifying the graph learned from approvals. This is the integration test for the entire matching engine.

---

## PROJECT CONTEXT

### Pipeline Flow (from spec Section 9)

```python
def match(incoming: NormalizedEntity, registry: EntityRegistry) -> MatchResult:
    result = deterministic_match(incoming, registry)       # Stage 1
    if result: return result
    
    candidates = generate_candidates(incoming, registry)   # Stage 2
    if not candidates: return MatchResult(new_entity=True)
    
    scored = score_candidates(incoming, candidates)        # Stage 3
    disposition = apply_thresholds(scored)                  # Stage 4
    
    if disposition.action == LLM_FALLBACK:
        llm_result = llm_assess(incoming, disposition.top_candidate)  # Stage 5
        disposition = Disposition(action=QUEUE_FOR_REVIEW, llm_reasoning=llm_result)
    
    resolve(disposition)                                    # Stage 6
    return MatchResult(disposition)
```

### V1 Hard Constraints

- Sequential processing — no async/parallel
- No agent orchestration framework
- Every resolution logged to audit trail

### Relevant Spec Sections

- Section 9: Complete Pipeline — `matcher.py` code block
- Section 13: Workflow — Customer Data Flow
