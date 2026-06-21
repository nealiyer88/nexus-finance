# Feature Brief: fastText Integration + Signal Set B Reconciliation (Stages 2c + 3) — v4 Retrofit

**Author:** Neal Iyer
**Date:** 2026-06-20
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 8a (retrofit of shipped features 7 + 8; must land before feature 12)

---

## Problem Statement

Product spec v4 (May 2026) promoted pre-trained fastText from V2+/NOT-SCOPE to **V1-mandatory** and raised the Phase 1 auto-match gate from 90% to 95%. Features 7 (blocking) and 8 (pairwise scoring) shipped under the V3 rules file, which put fastText in NOT-SCOPE and treated n-gram Jaccard as the bridge signal. Per v4 §5, §9, §17, that gap is unbridgeable: "the accuracy gap between 80% (RapidFuzz alone) and 95% (revised gate) is unbridgeable without subword embedding similarity on abbreviation patterns." Abbreviation pairs in the fixture — `pacrim tech` ↔ `pacific rim technologies international`, `meridian cap` ↔ `meridian capital group`, PSA-shortcode ↔ accounting-full-name — score near-zero on token_set_ratio and trigram Jaccard. fastText subword cosine is the only V1 signal that surfaces them.

This feature retrofits the shipped matching stack to v4: adds pre-trained fastText to Stage 2c (blocking) and Stage 3 (Signal Set C cosine), and reconciles Stage 3 Signal Set B to the enumerated B1–B6 with the +0.20 cap and auditability logging that v4 §9 specifies. No FP&A or pipeline-shape change — same six stages, same interfaces.

---

## Scope

### In Scope

- **fastText vector loader** (`core/matching/embeddings.py`, NEW): load a compressed/quantized pre-trained English model (<100MB), expose `embed(normalized_name: str) -> tuple[float, ...] | None` and `cosine(a, b) -> float`. Pure-Python load path. No C++ compile. Model file lives under `models/`, gitignored, fetched by a script.
- **Stage 2c — fastText candidate retrieval** (extend `core/matching/indices.py` + wire into `core/matching/blocking.py`): build an in-memory `EmbeddingIndex` (canonical_id → vector) at pipeline start from the same seed strings as TokenIndex/NgramIndex. On query, compute the incoming entity's embedding and return top-k nearest by **flat cosine scan** (no ANN library). Union these candidates with the existing token + trigram candidates, recording a `embed:<rank>` blocking signal per candidate. Intra-system filter (2d) and CANDIDATE_CAP (2e) apply unchanged.
- **Stage 3 Signal Set C — fastText cosine** (extend `core/matching/scoring.py` + `core/matching/weights.py`): add `fasttext_cosine` as a weighted signal in the ensemble, with a per-category-pair weight in `WeightConfig`. Surface the raw cosine in `signal_breakdown`.
- **Stage 3 Signal Set B reconciliation** (`core/matching/scoring.py`): read what shipped, then ensure all six signals exist, each gated to the 0.70–0.90 ambiguous zone, with the v4 boost ranges:
  - B1 shared person entity (+0.05–0.10)
  - B2 project-code fragment in QB ref/class/memo (+0.08–0.12)
  - B3 amount co-occurrence within AMOUNT_TOLERANCE same period (+0.10–0.15)
  - B4 shared email domain (+0.05–0.08)
  - B5 temporal co-occurrence, same 30-day first-seen window (+0.03–0.05)
  - B6 graph neighborhood overlap (+0.02–0.05 per shared node, capped +0.10)
  - **Total Signal Set B boost hard-capped at +0.20.** Every applied boost logged in `signal_breakdown` with signal id, raw value, and applied value (auditability per v4 §9).
- **Model fetch script** (`scripts/fetch_fasttext.py`, NEW): downloads/places the compressed model into `models/`. Idempotent; skips if present. Documents the source and SHA.
- **Tests:** extend `tests/test_blocking.py`, `tests/test_scoring.py`; add `tests/test_embeddings.py`. Use a tiny vendored/stub vector set or monkeypatched `embed` for unit tests so the suite does not depend on the full model download.

### Out of Scope

- Fine-tuned fastText (V2+; corpus-dependent — rules §11).
- True ANN index (faiss/annoy/hnswlib). Flat cosine is correct at <500 entities; revisit at >50K.
- Contextual / Layer-3 embeddings on transactional co-occurrence (V2+).
- XGBoost, GraphRAG, self-hosted LLM, write-back — all unchanged NOT-SCOPE.
- Any change to Stages 0, 1, 4, 5, 6 interfaces.
- Raising hardcoded disposition cutoffs (0.90/0.70/0.50 unchanged). The 95% figure is a Phase-1 *success gate* measured by the orchestrator over real cycles, not a constant in this feature.

---

## Success Criteria

- [ ] `core/matching/embeddings.py` exists; `embed("")` and `embed("   ")` return `None` without raising; `cosine` returns 0.0 when either vector is `None`.
- [ ] `from core.matching.embeddings import embed, cosine` works; model loads via pure-Python path with no C++ compile step in CI.
- [ ] Stage 2c: for query `"pacrim tech"` against a registry seeded with `"pacific rim technologies international"`, the fastText path surfaces that canonical as a candidate when token + trigram blocking alone do not. Asserted in `tests/test_blocking.py`.
- [ ] Stage 3 Signal Set C: `score_pair` output `signal_breakdown` contains a `fasttext_cosine` entry for every scored pair; weight is category-pair-dispatched (PSA↔Accounting ≠ Accounting↔Accounting).
- [ ] Abbreviation lift: `"meridian cap"` ↔ `"meridian capital group"` and `"pacrim tech"` ↔ `"pacific rim technologies international"` score **above SURFACE (0.70)** with Signal Set C enabled, and **below 0.70** with it disabled (proves the signal is doing the work). Asserted in `tests/test_scoring.py`.
- [ ] Signal Set B: all of B1–B6 present and reachable; a synthetic pair that trips ≥4 B-signals receives total B boost **exactly capped at +0.20**, with each boost itemized in `signal_breakdown`.
- [ ] Known non-match `"brightpath machine learning"` (QB) vs `"luminos ai"` (RUDDR) still scores <0.50 — graph corroboration does not override strong negative string + embedding signal (the +0.20 cap holds).
- [ ] `requirements.txt` adds exactly one new pinned dependency for vector loading; no compiled/transitive C++ build required on Apple Silicon CI.
- [ ] `models/` raw model file is gitignored; `scripts/fetch_fasttext.py` is idempotent.
- [ ] `pytest tests/ -x --tb=short` passes with no regression to the prior shipped suite.

---

## Dependencies

- [ ] Feature 7 (deterministic-blocking) SHIPPED — extends `indices.py`, `blocking.py`.
- [ ] Feature 8 (pairwise-scoring) SHIPPED — extends `scoring.py`, `weights.py`. **CC must read the shipped versions and reconcile, not greenfield.**
- [ ] Rules file edits applied (§1, §6, §11, §13 — pre-trained fastText IN SCOPE).
- [ ] Vector-loader library selection confirmed in Phase 1 adversary debate (see Implementation Notes).

---

## Estimated Complexity

**Rating:** L

**Rationale:** New embedding subsystem + model logistics (fetch, footprint, CI safety), two existing matcher modules modified in place, Signal Set B reconciliation against unseen shipped code, and three test surfaces. The dependency-footprint decision (avoid 7GB model and C++ `fasttext`) is the load-bearing risk.

---

## PROJECT CONTEXT

### Pipeline position (unchanged shape)

```
Stage 2 Blocking:  TokenIndex + NgramIndex + [NEW] EmbeddingIndex flat-cosine top-k  → CandidateSet
Stage 3 Scoring:   Signal Set A (RapidFuzz) + [NEW] Signal Set C (fastText cosine)
                   + Signal Set B (B1–B6, +0.20 cap, logged) + category-pair dispatch  → ScoredMatch
```

### Implementation Notes (constraints for the build)

1. **No 7GB model. No C++ `fasttext` pip.** Use compressed/quantized pre-trained vectors with a pure-Python loader. Leading candidate: `compress-fasttext` loading a quantized `cc.en.300` (~25–50MB). Phase 1 engineer adversary confirms the exact library + pin and verifies Apple-Silicon CI safety before the build prompt is generated.
2. **Flat cosine, not ANN.** At <500 canonicals a full scan is sub-millisecond. No faiss/annoy/hnswlib.
3. **Embeddings built once per pipeline invocation** from `canonical_entities.canonical_name` + `entity_aliases.value`, mirroring TokenIndex/NgramIndex. No persistence, no `update()`.
4. **Tests must not require the full model download.** Unit tests monkeypatch `embed` or load a tiny vendored stub vector table; one optional integration test (skipped if model absent) exercises the real loader.
5. **Signal Set B cap is enforced after summation, not per-signal**, and the cap plus every individual boost is written to `signal_breakdown` for audit.
6. **Tenant scoping** on the EmbeddingIndex build matches the existing `tenant_id: Optional[str] = None` pattern in `indices.py`.

### V1 Hard Constraints (still binding)

- Connectors QB + RUDDR only. SQLite graph store. Shadow Ledger only.
- Pre-trained fastText IN SCOPE; fine-tuned fastText, XGBoost, GraphRAG, self-hosted LLM remain NOT-SCOPE.
- Graph-corroborated scoring uses deterministic SQL joins, not LLM calls.

### Relevant Spec Sections (v4)

- §5 Defensibility / Cold-Start (fastText as the 80→95 bridge)
- §9 Stage 2c, Stage 3 Signal Sets A/B/C, fastText three-layer architecture (Layers 1–2 in V1)
- §17 V1 Build Scope (pre-trained fastText IN, fine-tuned OUT)
- §7 Phase 1 gate 95%
