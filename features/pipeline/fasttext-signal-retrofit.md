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
- **Stage 3 Signal Set B reconciliation** (`core/matching/scoring.py`): implement five of six signals using existing V1 schema. Each signal gated to the 0.70–0.90 ambiguous zone, with v4 boost ranges enforced and the +0.20 hard cap applied AFTER summation:
  - B1 shared person entity (+0.05–0.10) — reads `entity_edges` SAME_AS/MEMBER_OF
  - B2 project-code fragment in QB ref/class/memo (+0.08–0.12) — reads `system_references.external_fields` JSON for `class`/`memo` fields, extracts segments via the existing project-code shape utility, matches against PSA-side project codes via `_check_psa_abbreviation`'s shortcode logic
  - B4 shared email domain (+0.05–0.08) — reads `system_references.external_fields.email`, splits on `@`, compares domain (case-insensitive)
  - B5 temporal co-occurrence, same 30-day first-seen window (+0.03–0.05) — reads `canonical_entities.created_at` on both canonicals, fires when `abs(delta_days) <= 30`
  - B6 graph neighborhood overlap (+0.02–0.05 per shared node, capped +0.10) — already shipped, reconcile to spec-mandated cap

  **Every applied boost logged in `signal_breakdown`** with signal id, raw value, applied value (per spec v4 §9 audit). Total Signal Set B boost hard-capped at +0.20.
- **Defensive guard against deferred-signal accidental population:** B3 (amount co-occurrence) is OUT OF SCOPE for this feature — it requires a transactions table not in V1 schema. `_compute_b_boosts` MUST NOT include any field, parameter, or branch related to B3. Reviewers MUST grep the diff for `amount_cooccurrence`, `transaction`, and similar; their presence in any code or test is a BLOCKING violation. (This guard exists because the prior attempt at this feature shipped no-op B3 scaffolds that violated NON-GOALS — see CC-LEARNINGS 2026-06-21.)
- **Model fetch script** (`scripts/fetch_fasttext.py`, NEW): downloads/places the compressed model into `models/`. Idempotent; skips if present. Documents the source and SHA.
- **Tests:** extend `tests/test_blocking.py`, `tests/test_scoring.py`; add `tests/test_embeddings.py`. Use a tiny vendored/stub vector set or monkeypatched `embed` for unit tests so the suite does not depend on the full model download.

### Out of Scope

- B3 amount co-occurrence — deferred to **feature 8b** (`features/pipeline/b3-transactions-table-and-amount-signal.md`, to be authored). 8b will add a minimal transactions table to `db/schema.sql` and `db/schema_sqlite.sql` plus the B3 signal implementation. 8b is the prerequisite for feature 12 (matcher-orchestrator) to measure against the 95% gate over real cycles.
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
- [ ] Abbreviation lift (**AMENDED 2026-07-05**, approved by Neal): both pairs **reach the human review queue** — `"meridian cap"` ↔ `"meridian capital group"` via composite ≥ 0.70, `"pacrim tech"` ↔ `"pacific rim technologies international"` via the token-level abbreviation heuristic + Stage 4 rescue (`QUEUE_FOR_REVIEW`). Asserted in `tests/test_scoring.py` (stub-vector, no-model, and real-model variants). *Why amended:* measured on the real pre-trained model (SHA pinned in `scripts/fetch_fasttext.py`), the pair cosines are 0.2526 (pacrim) / 0.3679 (meridian), token-level `pacrim`↔`pacific` ≈ 0.0 — with the sum-to-1.0 renormalized budget, no fastText weight can lift a pair whose cosine is below 0.70, so the original "composite >0.70 via Signal C" criterion was mathematically unreachable. The original premise ("fastText subword cosine is the only V1 signal that surfaces them", §Problem) is empirically false for pre-trained vectors on invented/OOV company names; the deterministic heuristic carries the lift, fastText remains the Stage 2c blocking surfacer and a corroborating Stage 3 signal.
- [ ] Signal Set B: B1, B2, B4, B5, B6 present and reachable; B3 explicitly absent from `_compute_b_boosts`; a synthetic pair that trips ≥4 B-signals receives total B boost **exactly capped at +0.20**, with each boost itemized in `signal_breakdown`.
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
- Feature 8b (B3 + transactions table) will follow but does NOT gate this feature.

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
