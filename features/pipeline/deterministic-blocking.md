# Feature Brief: Deterministic Match + Blocking (Pipeline Stages 1–2)

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

Normalized entities from QB and RUDDR connectors need to be matched against the canonical registry. Stages 1 and 2 of the pipeline handle the zero-ambiguity cases (exact alias lookup, deterministic key joins) and reduce the comparison space for fuzzy matching from O(n²) to O(n×k). Without these stages, Stage 3 (pairwise scoring) must compare every entity against every other entity — computationally prohibitive and architecturally wrong.

These stages are grouped because they share the same data structures (canonical registry, inverted indices) and must be tested together to verify the handoff: "Stage 1 resolved 45% of entities; the remaining 55% passed to Stage 2 blocking, which produced candidate sets of 5–20 per entity."

---

## Scope

### In Scope

- Create `core/matching/deterministic.py` implementing Stage 1:
  - **1a — Known alias lookup:** Query alias table for exact normalized string match. If hit → resolve to canonical_id, confidence ≥0.95
  - **1b — Deterministic key join:** Email match across categories (confidence 0.99). Employee ID match (confidence 1.0). Tax ID/EIN if available
  - **1c — Canonical ID echo:** If incoming record carries canonical_id from prior sync, verify and resolve
  - Return: `DeterministicMatch(canonical_id, confidence, match_key_type)` or `None`

- Create `core/matching/blocking.py` implementing Stage 2:
  - **2a — Token inverted index:** Tokenize normalized name, look up each token → candidate canonical IDs
  - **2b — Phonetic index:** Generate Metaphone codes for each token, look up in phonetic index
  - **2c — Character n-gram index:** Generate character trigrams, look up in trigram index
  - **2d — Category-aware filtering:** Remove intra-system candidates (don't match QB against QB)
  - **2e — Candidate cap:** If candidate set exceeds 50, rank by blocking signals hit and truncate
  - Return: `CandidateSet(List[CandidateEntity])` or empty (flag as potential new entity)

- Create `core/matching/indices.py` — inverted index data structures:
  - `TokenIndex`: token → set of canonical_ids
  - `PhoneticIndex`: metaphone code → set of canonical_ids
  - `NgramIndex`: character trigram → set of canonical_ids
  - Methods: `build(entities)`, `lookup(query)`, `update(canonical_id, new_alias)`

- Create `core/graph/entity_store.py` — read interface to SQLite graph store:
  - `get_aliases(canonical_id)` → list of aliases with source and category
  - `lookup_alias(normalized_name)` → canonical_id or None
  - `lookup_email(email)` → canonical_id or None
  - `get_candidates_by_tokens(tokens)` → list of canonical_ids
  - Reads from the canonical schema (feature 2). No write methods in this feature.

- **Test suite:** `tests/test_deterministic.py` and `tests/test_blocking.py`
  - Seed SQLite with ground truth fixture data, run all 91 entities through Stage 1 → Stage 2
  - Assert: entities with exact alias matches resolve at Stage 1
  - Assert: remaining entities produce candidate sets of ≤50 per entity at Stage 2
  - Assert: no intra-system candidates survive Stage 2d filtering

### Out of Scope

- Pairwise scoring (Stage 3) — separate feature
- Threshold/disposition (Stage 4) — separate feature
- LLM fallback (Stage 5) — separate feature
- Graph writes / resolution (Stage 6) — separate feature
- Category-pair weight dispatch — that's Stage 3
- Graph-corroborated scoring — that's Stage 3

---

## Success Criteria

- [ ] `core/matching/deterministic.py` exists with `deterministic_match()` function
- [ ] `core/matching/blocking.py` exists with `generate_candidates()` function
- [ ] `core/matching/indices.py` exists with `TokenIndex`, `PhoneticIndex`, `NgramIndex` classes
- [ ] `core/graph/entity_store.py` exists with read-only interface to SQLite
- [ ] Stage 1 resolves entities with known aliases (exact string match returns canonical_id)
- [ ] Stage 1 resolves person entities with matching emails across categories (confidence 0.99)
- [ ] Stage 2 produces candidate sets ≤50 per entity
- [ ] Stage 2 filters out intra-system candidates (no QB-to-QB matches)
- [ ] Stage 2 returns empty set for entities with no blocking signal overlap (potential new entities)
- [ ] All inverted indices are updatable (new aliases can be added without full rebuild)
- [ ] `pytest tests/test_deterministic.py tests/test_blocking.py` passes

---

## Dependencies

- [ ] Canonical schema deployed (feature 2) — entity_store reads from this
- [ ] Normalizer shipped (feature 3) — input is NormalizedEntity
- [ ] ConnectorInterface base class (feature 4) — NormalizedEntity definition
- [ ] At least one connector shipped (feature 5 or 6) — need real-shaped data to test against

---

## Estimated Complexity

**Rating:** L

**Rationale:** Four files, three index data structures, entity store read interface, two test suites. The indices must be correct — false negatives at Stage 2 mean the matcher never considers the correct candidate. Phonetic encoding and n-gram decomposition have edge cases across org and person entity types.

---

## PROJECT CONTEXT

### Pipeline Position

```
Stage 0: Normalizer (feature 3) → NormalizedEntity
    ↓
Stage 1: Deterministic Match ◄── THIS FEATURE
    ↓ (unresolved entities)
Stage 2: Blocking ◄── THIS FEATURE
    ↓ (candidate sets)
Stage 3: Pairwise Scoring (feature 8)
    ↓
Stage 4: Threshold + Disposition (feature 9)
```

### Entity Store Schema (from feature 2)

```sql
canonical_entities: canonical_id, canonical_name, entity_type, entity_category, confidence
entity_aliases: alias_id, canonical_id, value, source, category, confidence
entity_edges: edge_id, source_node, target_node, relationship, source_category, target_category, weight
```

### V1 Hard Constraints

- SQLite only (no Neo4j)
- No fastText — trigram index uses character n-gram overlap, not embeddings
- No agent orchestration — sequential Python functions
- Person entity matching: email is near-deterministic join key, name inversion detection at Stage 1

### Relevant Spec Sections

- Section 9: Matcher Pipeline Architecture — Stage 1 (Deterministic Match), Stage 2 (Blocking)
- Section 9: Knowledge Graph — entity node, alias, edge structures
