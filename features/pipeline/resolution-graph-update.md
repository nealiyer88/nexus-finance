# Feature Brief: Resolution + Graph Update (Pipeline Stage 6)

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

After disposition (auto-approve or human approval), the graph must be updated: new aliases added, edges created with category metadata, inverted indices updated, and structured training pairs stored. Without this stage, approvals don't compound — the graph never learns, and every sync cycle starts from scratch.

---

## Scope

### In Scope

- Create `core/graph/resolution.py` implementing Stage 6:
  - **Match confirmed:** Add alias to canonical node, create/update graph edge with category metadata (source_category, target_category, weight, approval_count, approved_by, timestamp), update all inverted indices, store structured training pair
  - **New entity confirmed:** Create canonical node, generate canonical_id, add system references, initialize edges to related entities, update indices, store negative training pairs for rejected candidates
  - **Match rejected:** Log as hard negative training pair with full signal breakdown

- Extend `core/graph/entity_store.py` with write methods:
  - `create_canonical_entity(name, entity_type, entity_category, confidence)` → canonical_id
  - `add_alias(canonical_id, value, source, category, confidence)`
  - `create_edge(source_node, target_node, relationship, source_category, target_category, weight, approved_by)`
  - `increment_approval_count(edge_id)`
  - `update_confidence(canonical_id, new_confidence)`

- Create `core/matching/training_data.py`:
  - `TrainingPair` dataclass: entity_pair, signal_breakdown, graph_evidence, category_pair, disposition, reasoning_trace
  - `store_training_pair(pair: TrainingPair)` → writes to approval_decisions table
  - Captures both positive (match confirmed) and negative (match rejected) pairs

- Create `core/graph/audit.py`:
  - `log_resolution(canonical_id, incoming_entity_raw, match_type, confidence, signals, category_pair, user_id)`
  - Append-only audit log entry for every resolution decision

- **Test suite:** `tests/test_resolution.py`
  - Seed empty graph, resolve 10 entities from fixtures, verify graph state
  - Assert: aliases added after match confirmation
  - Assert: edge created with correct category metadata
  - Assert: inverted indices updated (new alias findable in subsequent lookups)
  - Assert: training pair stored with full signal breakdown
  - Assert: rejected match produces negative training pair
  - Assert: audit log entries created for every resolution

### Out of Scope

- Approval queue UI — separate feature
- Confidence decay — separate feature (feature 13)
- Write-back to source systems — Shadow Ledger only
- Batch resolution (process 1000 entities at once) — V1 processes sequentially

---

## Success Criteria

- [ ] `core/graph/resolution.py` exists with `resolve_match()`, `create_new_entity()`, `reject_match()` functions
- [ ] `core/graph/entity_store.py` extended with write methods
- [ ] `core/matching/training_data.py` exists with `TrainingPair` dataclass and `store_training_pair()`
- [ ] `core/graph/audit.py` exists with `log_resolution()` function
- [ ] After match confirmation: alias exists in alias table, edge exists in edge table, inverted index updated
- [ ] After new entity creation: canonical node exists, system_references populated, canonical_id generated
- [ ] After match rejection: negative training pair stored with signal breakdown
- [ ] Audit log entries are append-only (no UPDATE/DELETE on audit_log table)
- [ ] Training data captures all fields: entity_pair, signal_breakdown, graph_evidence, category_pair, disposition, reasoning_trace
- [ ] `pytest tests/test_resolution.py` passes

---

## Dependencies

- [ ] Threshold + LLM fallback (feature 9) — input is Disposition
- [ ] Entity store read methods (feature 7)
- [ ] Canonical schema (feature 2) — write targets

---

## Estimated Complexity

**Rating:** M

**Rationale:** Four files, but logic is straightforward — INSERT/UPDATE operations against SQLite. Complexity is in ensuring idempotency (re-resolving the same entity doesn't create duplicates) and index consistency (every write updates all three inverted indices).

---

## PROJECT CONTEXT

### Resolution Outputs (from spec Section 9, Stage 6)

| Decision | Graph Action | Training Data |
|----------|-------------|---------------|
| Match confirmed | Add alias, create/update edge, update indices | Positive pair: entity_pair + signals + disposition |
| New entity | Create canonical node, initialize edges, update indices | Negative pairs: all rejected candidates |
| Match rejected | No graph change | Hard negative: rejected pair + full signal breakdown |

### Idempotency Requirements

- Same entity resolved twice → same canonical_id, no duplicate aliases
- Edge approval_count increments on re-confirmation, not duplicate edge creation
- Training pairs are append-only — duplicate resolution produces new training pair (captures temporal signal)

### V1 Hard Constraints

- SQLite graph store
- Audit log: append-only, no UPDATE/DELETE, tagged by system category
- Every database query RLS-scoped to tenant_id
- Training data capture from Day 1 — load-bearing for V2+ fine-tuning

### Relevant Spec Sections

- Section 9: Stage 6 — Resolution + Graph Update
- Section 8: System Architecture (idempotency everywhere, audit trail non-negotiable)
