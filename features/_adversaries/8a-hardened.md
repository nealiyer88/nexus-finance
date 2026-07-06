# Reconciler: Hardened Design — Feature 8a

## Resolved Disagreements

**D1. Bundle fastText + Signal Set B, or split into 8a/8b?**
Winner: **Design.** Engineer confirms feasibility as L-rated if sequenced as two commits on one branch (Stage 2c + Signal C first, Set B reconciliation second). Split would fragment the v4 retrofit; keep bundled with sequenced landing.

**D2. Monkeypatched stub vectors sufficient for abbreviation-lift assertion?**
Winner: **Skeptic.** Engineer confirms subword semantics cannot be met by stubs. Split the assertion: unit tests use monkeypatched `embed` to verify weight application; one integration test gated on `skipif(not model_present)` exercises the real quantized model on the two fixture pairs.

**D3. B2 utilities as "hand-waved reuse" vs. new implementation?**
Winner: **Skeptic.** Engineer confirms `_check_psa_abbreviation` takes name/alias strings and returns bool — wrong shape for parsing `external_fields.class`/`memo` JSON against PSA project codes. B2 gets a NEW utility `_extract_project_code_fragments(external_fields: dict) -> set[str]` in `scoring.py`.

**D4. Is the 95% gate provable within 8a?**
Winner: **Design.** The gate is measured by Feature 12 over real cycles — this is documented feature ordering, not scope creep. Overfit risk mitigated by the *delta* assertion (score >0.70 with C, <0.70 without) which is not trivially tunable.

**D5. Defensive grep guard vs. structurally-typed B3 exclusion?**
Winner: **Skeptic.** Adopt Engineer's `BoostEntry` recommendation: `_compute_b_boosts` returns `tuple[BoostEntry, ...]` where `BoostEntry.signal_id: Literal["B1","B2","B4","B5","B6"]`. Grep guard retained as belt-and-suspenders.

## Engineer Flags

- **`_compute_b_boosts` does not exist** → Accept. Function must be **authored** (not "modified"). Brief updated.
- **B2/B4/B5 have zero DB accessors** → Accept. Add `get_created_at(canonical_id)` and `get_external_field(canonical_id, field_name)` to `core/graph/entity_store.py` per rules §12.
- **`alias_boost`/`abbreviation_bonus` fate unspecified** → Resolved. Leave as Set A signals, additive to B1–B6, unaffected by the +0.20 Set B cap.
- **Global +0.20 cap does not exist in `_weighted_score`** → Accept. New logic in `_compute_b_boosts` sums, then caps.
- **`SignalBreakdown` frozen dataclass** → Accept. Extend with `fasttext_cosine: float` and `b_boosts: tuple[BoostEntry, ...]` where `BoostEntry(signal_id, raw, applied)`.
- **`compress-fasttext` transitively pulls `gensim` + numpy/scipy** → Accept. Note in dependencies section; verified importable on this host.
- **5–6 session estimate** → Accept via sequencing (see D1).

## Hardened Brief

**Feature 8a: fastText + Signal Set B Reconciliation (v4 Retrofit)** — Complexity L, sequenced as two commits on one branch.

### Commit 1: Stage 2c + Signal Set C (the 80→95 bridge)

- **NEW `core/matching/embeddings.py`**: `embed(name) -> tuple[float,...] | None`; `cosine(a,b) -> float`. Empty/whitespace inputs return `None`; cosine returns 0.0 if either operand is `None`. Pure-Python loader via `compress-fasttext` on quantized `cc.en.300` (<100MB).
- **NEW `scripts/fetch_fasttext.py`**: idempotent download into gitignored `models/`, documents source + SHA.
- **Extend `core/matching/indices.py`**: `EmbeddingIndex` built once per pipeline from `canonical_entities.canonical_name` + `entity_aliases.value`, tenant-scoped per existing pattern.
- **Extend `core/matching/blocking.py`**: Stage 2c returns top-k by flat cosine scan (NO ANN); union with token+trigram candidates; each carries `embed:<rank>` blocking signal.
- **Extend `core/matching/scoring.py` + `weights.py`**: add `fasttext_cosine` as weighted ensemble signal in `WeightConfig` per category-pair. Surface raw cosine in `SignalBreakdown.fasttext_cosine`.
- **Tests**: `tests/test_embeddings.py` (new), extend `tests/test_blocking.py` (Stage 2c candidate surfacing), extend `tests/test_scoring.py` (weight application via monkeypatched `embed`).
- **Integration test**: one test gated on `skipif(not model_present)` asserts `"meridian cap"` ↔ `"meridian capital group"` and `"pacrim tech"` ↔ `"pacific rim technologies international"` score >0.70 with Signal C, <0.70 without.

### Commit 2: Signal Set B Reconciliation

- **AUTHOR `_compute_b_boosts` in `core/matching/scoring.py`** with signature returning `tuple[BoostEntry, ...]` where `BoostEntry.signal_id: Literal["B1","B2","B4","B5","B6"]`. Signature structurally excludes B3.
- **B1** (shared_person, +0.05/n cap +0.10): reconcile from shipped `count_shared_person_neighbors` at `entity_store.py:353`.
- **B2** (project-code fragment, +0.08–0.12): NEW utility `_extract_project_code_fragments(external_fields: dict) -> set[str]`; new `get_external_field(canonical_id, field)` accessor in `entity_store.py`.
- **B4** (email domain, +0.05–0.08): reuse `get_external_field`, split on `@`, case-insensitive compare.
- **B5** (temporal co-occurrence, +0.03–0.05): NEW `get_created_at(canonical_id)` accessor; fires when `abs(delta_days) <= 30`.
- **B6** (neighborhood overlap, +0.025/n cap +0.10): reconcile from shipped `count_shared_graph_neighbors` at `entity_store.py:395`.
- **+0.20 hard cap** applied AFTER summation in `_compute_b_boosts`. Only B-boosts capped; Set A signals (`alias_boost`, `abbreviation_bonus`) unaffected.
- **Extend `SignalBreakdown`**: add `b_boosts: tuple[BoostEntry, ...]` field; each applied boost itemized (signal_id, raw, applied).
- **Gating**: B-signals fire only in 0.70–0.90 ambiguous band.
- **B3 guard**: reviewers grep diff for `amount_cooccurrence`, `transaction`; typed `Literal` in `BoostEntry` prevents accidental B3 population.
- **Tests**: synthetic pair tripping ≥4 B-signals asserts total = exactly +0.20 with each itemized; negative-match pair (`"brightpath machine learning"` vs `"luminos ai"`) still <0.50.

### Non-Goals

- B3 (amount co-occurrence) and transactions table → **Feature 8b**.
- Fine-tuned fastText, ANN library, contextual embeddings, XGBoost, GraphRAG, self-hosted LLM, write-back, disposition cutoff changes.
- Persistence of `EmbeddingIndex`; no `update()`.
- Raising 0.90/0.70/0.50 cutoffs — the 95% figure is measured by Feature 12, not gated in 8a.

### Success Criteria

Per original brief, unchanged EXCEPT:
- Abbreviation-lift assertion split into (a) unit test verifying Signal C weight application via monkeypatched `embed`, and (b) integration test with real model gated on `skipif(not model_present)`.
- `_compute_b_boosts` MUST be authored (not modified); reviewers verify via grep for `def _compute_b_boosts`.
- `SignalBreakdown` extension shape: `fasttext_cosine: float` + `b_boosts: tuple[BoostEntry, ...]`.
- New accessors `get_created_at` and `get_external_field` land in `core/graph/entity_store.py`, not inline SQL in `scoring.py`.

## Risk Register

- **Deadlock replay on `SignalBreakdown` shape ripple.** Likelihood: **med**. 20+ `score_pair` assertions in `test_scoring.py` cascade on type change. Mitigation: land Commit 1's `fasttext_cosine` field first; Commit 2 adds `b_boosts` incrementally with per-file test updates.
- **B2 utility contract drift.** Likelihood: **med**. New `_extract_project_code_fragments` must match PSA `project_codes` shape exactly or fires wrong-direction boosts silently under the +0.20 cap. Mitigation: unit test asserts B2 fires on positive fixture AND does NOT fire on negative fixture with cap-independent assertion.
- **Apple-Silicon CI wheel breakage from `compress-fasttext` + `gensim` transitive chain.** Likelihood: **low** (Engineer verified importable on host). Mitigation: pin exact versions; CI job runs full `pytest` on M-series runner before merge.
- **Integration test flake from model-fetch nondeterminism.** Likelihood: **low**. Mitigation: `fetch_fasttext.py` verifies SHA; integration test skipped (not failed) when model absent so CI stays green without the download.