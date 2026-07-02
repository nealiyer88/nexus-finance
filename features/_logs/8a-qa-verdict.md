The critical evidence is in. Now writing the full QA verdict.

---

## QA Verdict — Feature 8a: fastText Integration + Signal Set B Reconciliation

### [QA-000] — Deliverable existence check

**Severity:** PASS

All brief-required paths verified:

| Path | Status |
|------|--------|
| `core/matching/embeddings.py` (NEW) | EXISTS — real implementation |
| `core/matching/indices.py` (MODIFY) | EXISTS — `EmbeddingIndex` class added |
| `core/matching/blocking.py` (MODIFY) | EXISTS — Stage 2c wired |
| `core/matching/scoring.py` (MODIFY) | EXISTS — Signal Set B + C integrated |
| `core/matching/weights.py` (MODIFY) | EXISTS — `fasttext_cosine` weight added |
| `core/matching/types.py` (MODIFY) | EXISTS — `fasttext_cosine`, `b_boosts` in `SignalBreakdown` |
| `core/graph/entity_store.py` (MODIFY) | EXISTS — `get_created_at`, `get_external_field` added |
| `models/.gitkeep` (NEW) | EXISTS |
| `scripts/fetch_fasttext.py` (NEW) | EXISTS — real implementation |
| `tests/test_embeddings.py` (NEW) | EXISTS — 6 tests |
| `tests/test_blocking.py` (MODIFY) | EXISTS — Stage 2c test added |
| `tests/test_scoring.py` (MODIFY) | EXISTS — Signal Set B/C tests added |

---

### [QA-001] — SC-1: embed() empty/whitespace returns None; cosine(None) returns 0.0

**Severity:** PASS (executed)

```
embed("") → None ✓
embed("   ") → None ✓
cosine(None, (1.0, 0.0)) → 0.0 ✓
cosine((1.0, 0.0), None) → 0.0 ✓
cosine(None, None) → 0.0 ✓
```

---

### [QA-002] — SC-2: Import works; pure-Python loader; no C++ compile

**Severity:** PASS (executed)

```
from core.matching.embeddings import embed, cosine  → OK
requirements.txt: compress-fasttext==0.1.5  (pure-Python package confirmed)
```

`compress-fasttext` imports cleanly with no C++ extension. No `faiss`, `annoy`, or `hnswlib` in `requirements.txt`.

---

### [QA-003] — SC-3: Stage 2c surfaces candidate when token+trigram blocking alone do not

**Severity:** PASS (executed)

`tests/test_blocking.py::test_embedding_stage_2c_surfaces_candidate` passes. Uses "lais corp" vs "luminos artificial intelligence systems" (zero token/trigram overlap). Monkeypatches `embed` to return unit vector for all inputs. Confirms:
- `CAN-EMB` absent from token/trigram results
- `CAN-EMB` present with `embed:*` signal when `EmbeddingIndex` passed

The brief names "pacrim tech"/"pacific rim technologies international" as the illustrative pair; the test uses an equivalent zero-overlap pair (same mechanism), which is valid for SC-3.

---

### [QA-004] — SC-4: `fasttext_cosine` in `signal_breakdown`; category-pair weight dispatch

**Severity:** PASS (executed)

```
test_signal_breakdown_carries_every_weighted_signal → PASS
breakdown.fasttext_cosine >= 0 ✓

PSA↔Accounting fasttext_cosine: 0.12
Accounting↔Accounting fasttext_cosine: 0.05
→ weights differ per category pair ✓
```

---

### [QA-005] — SC-5: Abbreviation lift — "pacrim tech" / "pacific rim technologies international" score ABOVE 0.70 with Signal Set C

**Severity:** BLOCKING

**Description:** The brief requires both abbreviation pairs to score ≥ 0.70 with Signal Set C enabled. "meridian cap" / "meridian capital group" meets this criterion. "pacrim tech" / "pacific rim technologies international" DOES NOT — and cannot with the current weights regardless of cosine value.

**Evidence (executed):**

```
# Measured without fasttext (null embed):
'pacrim tech' vs 'pacific rim technologies international' → 0.4340

# At maximum possible fasttext contribution (cosine = 1.0):
0.4340 + (PSA_ACCOUNTING_WEIGHTS.fasttext_cosine=0.12 × 1.0) = 0.5540

0.5540 < 0.70 (SURFACE threshold) — CRITERION FAILS
```

The brief states: `"pacrim tech" ↔ "pacific rim technologies international"` score **above SURFACE (0.70)** with Signal Set C enabled. The maximum achievable score is **0.5540** — firmly below the threshold. No bonus signal can close this gap:
- `abbreviation_bonus` (0.20) requires `SHORTCODE_MAX_LEN = 4`. "pacrim" is 6 chars; the full entity "pacrim tech" is 11 chars. **Does not fire.**
- `alias_boost` (0.15): no aliases registered for this pair in tests.
- Signal Set B boosts: require `base_score ∈ [0.70, 0.90)` — not satisfied at 0.5540.

The model-gated test `test_signal_c_lift_meridian_and_pacrim` is skipped (model absent), but mathematical analysis proves the assertion `result_with_ft.score >= 0.70` will fail for this pair even with the real model: `0.4340 + 0.12 × cosine ≤ 0.5540` for any `cosine ≤ 1.0`.

**Expected:** Either (a) increase `fasttext_cosine` weight for PSA↔Accounting to at least 0.27, (b) fix `_check_psa_abbreviation` to recognize "pacrim" as a ≤4-char-equivalent pattern (e.g. by tokenizing), or (c) ensure "PACRIM" alias is in the canonical so `alias_boost` fires.

---

### [QA-006] — SC-5 (secondary): SC-5 test has a patching defect

**Severity:** WARNING

**Description:** `test_signal_c_lift_meridian_and_pacrim` disables fasttext by patching `_wmod.get_weights` (module attribute). But `scoring.py` imports `get_weights` with `from core.matching.weights import get_weights`, creating a local binding. `mock.patch.object(_wmod, 'get_weights', ...)` replaces the module attribute but NOT scoring.py's local binding.

**Evidence (executed):**
```python
with m.patch.object(_wmod, 'get_weights', return_value='MOCKED'):
    print(scoring_mod.get_weights)  # → <original function>  # patch MISSED
```

The "no fasttext" branch in this test does NOT actually disable fasttext. When the real model is present, both `result_with_ft` and `result_no_ft` will use `PSA_ACCOUNTING_WEIGHTS.fasttext_cosine = 0.12`.

**Expected:** Patch `core.matching.scoring.get_weights` (the imported name) rather than `_wmod.get_weights`.

---

### [QA-007] — SC-6: Signal Set B — B1/B2/B4/B5/B6 present; B3 absent; +0.20 cap; boosts in `signal_breakdown`

**Severity:** PASS (executed)

```
test_b2_fires_on_overlapping_project_code_fragments → PASS
test_b2_does_not_fire_on_disjoint_fragments → PASS
test_b_boosts_total_applied_capped_at_max → PASS (4 signals, sum applied == 0.20 exactly)
test_b_boosts_do_not_fire_outside_ambiguous_band → PASS

grep B3/amount_cooccurrence/transaction in scoring.py → CLEAN
b_boosts field present in SignalBreakdown → ✓ (types.py:64)
Score_pair wires b_boosts into final_breakdown → ✓ (scoring.py:535)
```

---

### [QA-008] — SC-7: Non-match "brightpath machine learning" vs "luminos ai" scores < 0.50

**Severity:** PASS (executed)

```
test_ac25_brightpath_vs_luminos_scores_below_no_match → PASS
score = 0.27 (< 0.50 NO_MATCH threshold)
```

---

### [QA-009] — SC-8: Exactly one new pinned dependency; no C++ build required

**Severity:** PASS (executed)

`requirements.txt` adds exactly: `compress-fasttext==0.1.5`. No `faiss`, `annoy`, `hnswlib`, or `fasttext` (the C++ library). `compress-fasttext` is pure Python.

---

### [QA-010] — SC-9: `models/` gitignored; fetch script idempotent

**Severity:** PASS (executed)

```
.gitignore: models/*.bin, models/*.gz, models/*.bin.gz ✓
scripts/fetch_fasttext.py: MODEL_PATH.exists() check at line 42 → exits early if present ✓
```

Minor: Script prints the computed SHA after download but does not pin a known-good SHA to verify integrity. The brief says "Documents the source and SHA" — the source URL is documented in comments; the SHA is computed and printed at download time but not compared against a known value. No integrity failure gate. **WARNING only** — not blocking per brief text.

---

### [QA-011] — SC-10: Full test suite passes with no regression

**Severity:** PASS (executed)

```
pytest tests/ -x --tb=short
327 passed, 2 skipped in 0.15s
```

---

## Checks not run

| Check | Reason |
|-------|--------|
| `test_signal_c_lift_meridian_and_pacrim` (full model integration) | `core/matching/embeddings._model_present()` returns `False`; model not downloaded. Brief explicitly authorizes this skip (Implementation Notes §4). Independently verified by executing score_pair with monkeypatched embed. |
| `test_embed_returns_tuple_of_floats_for_normal_word` | Same — model absent. Pure unit test; not a blocking criterion. |

---

## Summary

| Criterion | Result |
|-----------|--------|
| SC-1: embed empty/None handling | PASS |
| SC-2: pure-Python import | PASS |
| SC-3: Stage 2c surfaces zero-overlap candidate | PASS |
| SC-4: fasttext_cosine in breakdown; category-pair weight dispatch | PASS |
| SC-5: abbreviation lift ≥ 0.70 — "meridian cap" | PASS |
| **SC-5: abbreviation lift ≥ 0.70 — "pacrim tech"** | **FAIL (BLOCKING)** |
| SC-5 test patching (disable-ft branch) | WARNING |
| SC-6: Signal Set B B1/B2/B4/B5/B6; B3 absent; +0.20 cap; audit trail | PASS |
| SC-7: brightpath/luminos non-match < 0.50 | PASS |
| SC-8: one new dependency; no C++ | PASS |
| SC-9: gitignore + idempotent fetch | PASS |
| SC-10: full suite, no regression | PASS |

VERDICT: FAIL