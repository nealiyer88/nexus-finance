# 8a Build Manifest

**Branch:** rocket-run-8a-v2  
**Tests:** 326 passed, 2 skipped (model-gated), 0 failed  
**Timestamp:** 2026-07-02T03:00:40Z

---

## Commit 1 — Stage 2c fastText blocking + Signal Set C scoring

**SHA:** daa8518

### Created
- `core/matching/embeddings.py` — lazy singleton fastText loader; `embed()`, `cosine()`, `_model_present()`
- `scripts/fetch_fasttext.py` — idempotent model download with SHA256 log
- `models/.gitkeep` — tracks models/ directory without binaries
- `tests/test_embeddings.py` — 5 unit tests + 1 model-gated integration test

### Modified
- `core/matching/indices.py` — added `EmbeddingIndex` class (flat cosine ANN, `build()` + `query()`)
- `core/matching/blocking.py` — added Stage 2c after trigram step; `embedding_index: Optional[EmbeddingIndex] = None` parameter
- `core/matching/weights.py` — added `fasttext_cosine: float` to `WeightConfig`; DEFAULT=0.05, PSA=0.12 (additive, not reducing existing weights)
- `core/matching/types.py` — added `fasttext_cosine: float = 0.0` to `SignalBreakdown`
- `core/matching/scoring.py` — Signal Set C: `_embeddings.cosine(embed(a), embed(b))` in `_compute_signal_breakdown`; `_base_weighted_score` adds `weights.fasttext_cosine * breakdown.fasttext_cosine`
- `tests/test_blocking.py` — added `test_embedding_stage_2c_surfaces_candidate` (AC-6)
- `tests/test_scoring.py` — added `test_signal_c_lift_meridian_and_pacrim` (AC-10, model-gated); updated `test_signal_breakdown_carries_every_weighted_signal`
- `requirements.txt` — added `compress-fasttext==0.1.5`
- `.gitignore` — added `models/*.bin`, `models/*.gz`, `models/*.bin.gz`

---

## Commit 2 — Signal Set B adaptive graph-corroborated boosts

### Modified
- `core/graph/entity_store.py` — added `get_created_at()` and `get_external_field()` helpers (Stage 3 reads)
- `core/matching/types.py` — added `b_boosts: tuple[BoostEntry, ...] = ()` to `SignalBreakdown`; `TYPE_CHECKING` import for `BoostEntry`
- `core/matching/scoring.py`:
  - Added `BoostEntry(signal_id, raw, applied)` frozen dataclass
  - Added `_FREEMAIL_DOMAINS`, `MAX_B_BOOST = 0.20`, `_FRAGMENT_SPLIT_RE`
  - Added `_extract_project_code_fragments()` — parses class/memo/project_codes fields
  - Added `_get_candidate_external_fields()` — merges system_references JSON blobs
  - Added `_compute_b_boosts()` — B1 (shared persons), B2 (project code fragments), B4 (email domain), B5 (temporal co-occurrence), B6 (neighborhood overlap); band-gated [0.70, 0.90); proportional cap at MAX_B_BOOST
  - Refactored `_weighted_score` → `_base_weighted_score` (unclamped, no B-boosts) + `_weighted_score` (adds B-boosts, clamps)
  - `score_pair` now computes B-boosts for score, `_compute_graph_evidence` for GraphEvidence metadata (always accurate, not band-gated)
  - `SignalBreakdown.b_boosts` carries fired boosts for audit
- `tests/test_scoring.py`:
  - Added `_compute_b_boosts`, `MAX_B_BOOST` imports
  - Added `test_b2_fires_on_overlapping_project_code_fragments` (AC-17+)
  - Added `test_b2_does_not_fire_on_disjoint_fragments` (AC-17-)
  - Added `test_b_boosts_total_applied_capped_at_max` (AC-24)
  - Added `test_b_boosts_do_not_fire_outside_ambiguous_band`
  - Added `test_ac25_brightpath_vs_luminos_scores_below_no_match` (AC-25)

---

## Signals NOT implemented (deferred)

- **B3** — transaction amount co-occurrence: requires transaction join not yet in V1 schema. Deferred to 8b per brief §B3-deferred.

## Key design decisions

- **Weight budget**: `fasttext_cosine` is additive on top of the 6 existing weights (which still sum to 1.0). Reducing existing weights would drop person-inversion pairs below the 0.95 gate.
- **GraphEvidence accuracy**: `GraphEvidence` is always computed from `_compute_graph_evidence` (live DB queries), independent of whether B-boosts are in the ambiguous band. B-boosts drive the score; GraphEvidence is metadata.
- **Band gate**: B-boosts only fire for 0.70 ≤ base_score < 0.90. Below the band, the pair is likely a non-match; above it, the string signals are already conclusive.
