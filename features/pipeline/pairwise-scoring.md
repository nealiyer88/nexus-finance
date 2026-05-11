# Feature Brief: Pairwise Scoring Engine (Pipeline Stage 3)

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

After blocking produces candidate sets of 5–20 per entity, each candidate pair needs a match score. This is the core of the matching engine — where RapidFuzz signals, category-pair weight dispatch, n-gram Jaccard, graph-corroborated scoring, and category-specific heuristics combine into a final score that determines disposition. A wrong weight profile here produces false matches that corrupt the graph permanently.

---

## Scope

### In Scope

- Create `core/matching/scoring.py` implementing Stage 3 pairwise scoring
- **Signal Set A — String metrics:**
  - `token_sort_ratio` (RapidFuzz)
  - `token_set_ratio` (RapidFuzz)
  - `partial_ratio` (RapidFuzz)
  - `jaro_winkler` (RapidFuzz)
  - N-gram Jaccard (character trigram set intersection / union)
  - Alias boost: if any existing alias scores >0.85 against candidate → +0.15 bonus

- **Category-pair weight dispatch:**
  - `Dict[Tuple[str, str], WeightConfig]` dispatching on `(source_category, target_category)`
  - PSA ↔ Accounting: weight prefix matching and abbreviation heuristics heavily
  - Person entities (any category pair): name inversion detection → 0.95 if tokens identical but order differs; email match → +0.10 soft signal
  - Default weights for unconfigured pairs

- **Signal Set B — Graph-corroborated scoring:**
  - Shared person entities: does a person assigned to this RUDDR project also appear under the candidate QB client? SQL join, not LLM. +0.05–0.10
  - Shared transaction context: temporal co-occurrence within same billing period → +0.03–0.05
  - Graph neighborhood overlap: shared connected nodes → +0.02–0.05 per shared node, capped

- **Signal Set C — N-gram Jaccard (V1 bridge signal):**
  - Character trigram decomposition as signal orthogonal to edit-distance metrics
  - "MCG" vs "Meridian Consulting Group" — catches shared character sequences missed by Jaro-Winkler

- **Category-specific heuristics:**
  - PSA shortcodes: if candidate ≤4 chars from PSA, check abbreviation of incoming name
  - Accounting class codes: parse hierarchical class, check segment matches
  - Person name inversion: token-identical but order-different → score 0.95 directly

- **Output:** `ScoredMatch(canonical_id, score, signal_breakdown, graph_evidence, category_pair)` for each candidate

- Create `core/matching/weights.py` — weight configuration:
  - `WeightConfig` dataclass with per-signal weights
  - Default weight profiles for V1 category pairs
  - `get_weights(source_category, target_category)` → WeightConfig

- **Test suite:** `tests/test_scoring.py`
  - Seed with ground truth, run all 44 canonical match pairs through scorer
  - Assert: known matches score above 0.70 (SURFACE threshold)
  - Assert: known non-matches (QB-only entities vs random RUDDR entities) score below 0.50
  - Assert: category-pair dispatch selects correct weight profile
  - Assert: PSA shortcode heuristic boosts "CEN" ↔ "Cenlar FSB" score
  - Assert: person name inversion "Chen, Michael" ↔ "Michael Chen" scores ≥0.95

### Out of Scope

- XGBoost classifier — V2+, replaces fixed weights
- fastText cosine similarity — V2+, replaces n-gram Jaccard position
- LLM parallel assessment — V2+ for all candidates; V1 only for <0.70 fallback zone
- Threshold application — that's Stage 4 (feature 9)
- Cluster conflict detection — that's Stage 4 (feature 9)
- Graph writes — that's Stage 6 (feature 10)

---

## Success Criteria

- [ ] `core/matching/scoring.py` exists with `score_pair()` function
- [ ] `core/matching/weights.py` exists with `WeightConfig` and `get_weights()`
- [ ] Signal breakdown returned for every scored pair (all individual signal values visible)
- [ ] Category-pair dispatch selects different weights for PSA↔Accounting vs default
- [ ] Graph-corroborated scoring queries entity_store for shared person entities and transaction context
- [ ] "CEN" (RUDDR) vs "Cenlar, LLC" (QB) — abbreviation heuristic fires, score >0.70
- [ ] "Chen, Michael" (QB) vs "Michael Chen" (RUDDR) — name inversion scores ≥0.95
- [ ] "BrightPath Machine Learning Corp" (QB) vs "Luminos AI" (RUDDR) — rebrand pattern scores <0.50 (correctly identified as requiring manual/alias resolution)
- [ ] All 44 ground truth matches produce scores >0.50
- [ ] `pytest tests/test_scoring.py` passes

---

## Dependencies

- [ ] Deterministic + Blocking shipped (feature 7) — Stage 3 receives CandidateSet from Stage 2
- [ ] Entity store (created in feature 7) — graph queries for Signal Set B
- [ ] Normalizer (feature 3) — input is NormalizedEntity pairs

---

## Estimated Complexity

**Rating:** L

**Rationale:** Most algorithmically complex feature in V1. Multiple signal sets, category-pair dispatch, graph-corroborated scoring, category-specific heuristics. Weight tuning directly affects match quality. Extensive test coverage required — every naming pattern in fixtures must produce a reasonable score.

---

## PROJECT CONTEXT

### Pipeline Position

```
Stage 2: Blocking (feature 7) → CandidateSet
    ↓
Stage 3: Pairwise Scoring ◄── THIS FEATURE
    ↓ ScoredMatch for each candidate
Stage 4: Threshold + Disposition (feature 9)
```

### Weight Config Structure

```python
@dataclass
class WeightConfig:
    token_sort_ratio: float      # default 0.25
    token_set_ratio: float       # default 0.25
    partial_ratio: float         # default 0.15
    jaro_winkler: float          # default 0.10
    ngram_jaccard: float         # default 0.10
    alias_boost: float           # default 0.15
    abbreviation_bonus: float    # PSA pairs: 0.20, others: 0.0
    name_inversion_score: float  # person entities: 0.95
```

### V1 Hard Constraints

- No XGBoost — deterministic category-pair weight dispatch
- No fastText — n-gram Jaccard is the V1 bridge signal
- Graph-corroborated scoring uses SQL joins against entity_store, not LLM calls
- Weight profiles are reversible — tune from approval feedback

### Relevant Spec Sections

- Section 9: Matcher Pipeline Architecture — Stage 3 (full signal set descriptions)
- Section 9: Category-pair weight dispatch
- Section 9: Graph-corroborated scoring
