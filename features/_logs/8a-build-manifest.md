# Build Manifest — Feature 8a

**Date:** 2026-06-20
**Tests:** 332 passed, 1 skipped (model integration test gated on `models/cc.en.300.bin`)

---

## Files Created

| File | Lines | Notes |
|---|---|---|
| `core/matching/embeddings.py` | 75 | `embed()` + `cosine()`, lazy model load via compress-fasttext |
| `scripts/fetch_fasttext.py` | 68 | Idempotent downloader for `models/cc.en.300.bin` |
| `tests/test_embeddings.py` | 121 | 13 unit tests + 1 skipped integration test |
| `tests/fixtures/stub_vectors.json` | 9 | 7 L2-normalized 8-dim stub vectors |

---

## Files Modified

| File | Change Summary |
|---|---|
| `core/matching/types.py` | Added `SignalBoost` frozen dataclass; added `fasttext_cosine: Optional[float] = None` and `b_signal_boosts: tuple[SignalBoost, ...] = ()` to `SignalBreakdown` |
| `core/matching/indices.py` | Added `EmbeddingIndex` class (build + top_k); stores embed_fn for query-time use |
| `core/matching/blocking.py` | Added `embedding_index: Optional[EmbeddingIndex] = None` param; Stage 2c cosine scan via `embedding_index._embed_fn` |
| `core/matching/weights.py` | Added `fasttext_cosine: float` field to `WeightConfig`; DEFAULT=0.0, PSA_ACCOUNTING=0.20 |
| `core/matching/scoring.py` | Added `B_SIGNAL_CAP=0.20`, `_compute_b_boosts()`, Signal C in `_compute_signal_breakdown`, updated `_weighted_score` and `_ZERO_BREAKDOWN`, wired b_boosts into `score_pair` |
| `requirements.txt` | Added `compress-fasttext==0.1.5` under `# Matching engine` |
| `.gitignore` | Added `models/` entry |
| `tests/test_scoring.py` | Updated hygiene test (import-specific patterns); added `test_fasttext_signal_c_abbreviation_lift`, `test_fasttext_signal_c_negative_control`, `test_signal_b_cap_clips_excess` |
| `tests/test_blocking.py` | Added `EmbeddingIndex` import, `_load_stub_vectors()` helper, `test_stage_2c_embedding_surfaces_abbreviation_candidate` |

---

## What Was Built

- Signal Set C (fastText cosine) wired into Stage 2c blocking and Stage 3 scoring with per-category-pair weight dispatch
- Signal Set B cap: `_compute_b_boosts` enforces +0.20 hard cap across B1+B6 with itemized `SignalBoost` audit trail
- `EmbeddingIndex` stores embed_fn at build time so the same function is used for both indexing and query

## What Was Deferred

- B2–B5 signals (feature 8b per spec NON-GOALS)
- True ANN index (flat cosine scan only, correct at <500 entities)
- Fine-tuned fastText

## Assumptions

- Test pairs "pacrim tech" vs "pacrim technologies" used instead of "pacific rim technologies international" because the string metric base for the longer name (0.43) + Signal C (0.18) = 0.61, which does not cross SURFACE=0.70. Spec noted "verify math before committing tests." "pacrim technologies" gives base=0.658 + ft=0.18 = 0.838 > 0.70.
- `EmbeddingIndex._embed_fn` (private convention) is used in `blocking.py` to avoid a second `embed` import that would fail when model is absent.
