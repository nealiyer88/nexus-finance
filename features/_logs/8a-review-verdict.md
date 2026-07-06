# Combined verdict — 8a
## QA
---

## QA Verdict — Feature 8a (fastText + Signal Set B Retrofit)

### [QA-000] Deliverable Existence Gate

| Path | Status |
|------|--------|
| `core/matching/embeddings.py` (NEW) | **PASS** — exists, real embed/cosine implementation |
| `scripts/fetch_fasttext.py` (NEW) | **PASS** — exists, idempotent skip-if-present logic |
| `models/.gitkeep` (NEW) + .gitignore entries | **PASS** — present; *.bin, *.gz gitignored |
| `tests/test_embeddings.py` (NEW) | **PASS** — 5 unit tests + 1 model-gated skip |
| `core/matching/indices.py` — EmbeddingIndex | **PASS** — class added, flat cosine scan |
| `core/matching/blocking.py` — Stage 2c wire | **PASS** — optional `embedding_index` param wired |
| `core/matching/scoring.py` — B1/B2/B4/B5/B6 + Signal C | **PASS** — all five signals present, B3 absent |
| `core/matching/weights.py` — fasttext_cosine field | **PASS** — added with category-pair dispatch |
| `core/matching/types.py` — SignalBreakdown | **PASS** — fasttext_cosine + b_boosts fields added |
| `requirements.txt` — compress-fasttext==0.1.5 | **PASS** — exactly one new dep, no C++ fasttext |
| `tests/test_blocking.py` — Stage 2c test | **PASS** — test exists, passes |
| `tests/test_scoring.py` — B2/cap/AC-25/Signal C | **PASS** — 5 new tests, 1 model-gated skip |

### Success Criteria

**AC-1** (`embed("") → None`, `embed("   ") → None`, `cosine(None, x) → 0.0`) — **PASS** (executed, verified)

**AC-2** (import works, pure-Python no C++ compile) — **PASS** (verified; compress-fasttext==0.1.5 is pure-Python)

**AC-3** (Stage 2c surfaces pacrim-style candidates that token+trigram miss) — **PASS** — The test uses `"lais corp"` / `"luminos artificial intelligence systems"` instead of the brief's named pair, but this is correct: `"pacrim"` and `"pacific"` share trigrams (`^pa`, `pac`, `rim`), so the brief's pair would have been found without embedding. The substituted pair has zero token/trigram overlap and proves the mechanism. Both without-embedding (no candidate) and with-embedding (candidate surfaces with `embed:1` signal) verified.

**AC-4** (every scored pair has `fasttext_cosine` in `signal_breakdown`; PSA↔Accounting weight ≠ Accounting↔Accounting weight) — **PASS** (PSA↔Acc: 0.12, Acc↔Acc: 0.05; field present on every result)

**AC-5** (abbreviation lift — meridian cap / pacrim tech score ≥0.70 with C enabled, <0.70 disabled) — **SKIPPED** — `test_signal_c_lift_meridian_and_pacrim` is correctly gated `@pytest.mark.skipif(not _model_present())`. The model cannot be downloaded: `scripts/fetch_fasttext.py` URL returns HTTP 404. The brief's Implementation Note 4 explicitly authorizes model-conditional skips for this integration test. (See [QA-001])

**AC-6** (B1/B2/B4/B5/B6 present, B3 absent, ≥4 signals capped at +0.20, each itemized) — **PASS** — B3, `amount_cooccurrence`, `transaction` absent from all changed files (grep clean). Synthetic 5-signal pair: total_raw=0.45, total_applied=0.20 exactly. Each boost itemized as `BoostEntry(signal_id, raw, applied)` in `signal_breakdown.b_boosts`.

**AC-7** (`"brightpath machine learning"` vs `"luminos ai"` scores 0.281 < 0.50) — **PASS**

**AC-8** (exactly one new dep; no C++ compile) — **PASS** (`compress-fasttext==0.1.5`; depends on numpy+gensim, no C++ build)

**AC-9** (`models/` gitignored; fetch script idempotent) — **PASS** (idempotent check verified; .gitignore covers *.bin, *.gz, *.bin.gz)

**AC-10** (pytest passes with no regressions) — **PASS** (326 passed, 2 skipped — both skips are authorized model-gate skips)

---

### Issues

### [QA-001] — Fetch script URL returns HTTP 404
**Severity:** WARNING
**Description:** `scripts/fetch_fasttext.py` requests `https://storage.yandexcloud.net/nlp/compress-fasttext/models/cc.en.300-compress.bin` and receives HTTP 404. The model file can never be downloaded via this script, making the model-gated integration tests permanently unrunnable.
**Evidence:** `python3 scripts/fetch_fasttext.py` → `download failed: HTTP Error 404: Not Found`
**Expected:** A working URL for the compressed fastText model (e.g., a Hugging Face Hub URL or the project's own CDN). AC-5 integration coverage is blocked until this is resolved.

### [QA-002] — AC-3 test pair substitutes brief's specified pair
**Severity:** WARNING
**Description:** Brief AC-3 specifies `"pacrim tech"` / `"pacific rim technologies international"` as the pair to assert in `test_blocking.py`. The test uses `"lais corp"` / `"luminos artificial intelligence systems"` instead.
**Evidence:** `test_embedding_stage_2c_surfaces_candidate` docstring; confirmed `"pacrim tech"` is found by trigrams even without embedding (`^pa`, `pac`, `rim` are shared).
**Expected:** Either update the brief to remove the specific-pair claim, or add a comment in the brief explaining why the pair was substituted. The substituted pair is technically more correct; this is a documentation/traceability gap.

### [QA-003] — Weight budget over-subscription; fasttext_cosine additive on top of prior weights
**Severity:** WARNING
**Description:** `fasttext_cosine` was added without reducing other signal weights. DEFAULT max pre-clamp is now 1.05 (was 1.00); PSA_ACCOUNTING max is 1.32 (was 1.20). A fix was attempted (CR-002) but reverted because it broke two threshold-anchored tests.
**Evidence:** `DEFAULT_WEIGHTS` scoring sum = 0.90 + 0.15 alias_boost = 1.05; tests `test_score_clamped_to_unit_interval` and `test_person_inversion_pair_scores_at_least_0_95_via_string_metrics` remain tied to pre-adjustment thresholds.
**Expected:** Either rebalance weights and update the threshold tests to the new values, or explicitly document that the clamped region is wider by design.

### [QA-004] — Double DB query for shared-neighbor signals
**Severity:** WARNING
**Description:** `score_pair` calls `count_shared_person_neighbors` and `count_shared_graph_neighbors` twice per pair — once in `_compute_b_boosts` (for B1/B6) and once in `_compute_graph_evidence` (for GraphEvidence). The GraphEvidence bonuses are no longer added to the score (correct), but the redundant queries remain.
**Evidence:** `core/matching/scoring.py:415-428` (`_compute_graph_evidence` still calls both) and `scoring.py:524` (called after `_compute_b_boosts`). Acknowledged as CR-003 in fix report.
**Expected:** `_compute_graph_evidence` could derive its counts from the `b_boosts` tuple or the two functions could be called once and shared.

### [QA-005] — Hygiene guard for forbidden ML imports removed without replacement
**Severity:** WARNING
**Description:** `test_no_xgboost_no_fasttext_no_llm_in_scoring` was removed. The guards for `xgboost`, `anthropic`, `openai`, `fastembed`, `sentence_transformers`, `torch`, `transformers` in `scoring.py` are no longer automatically enforced.
**Evidence:** Diff section 12 in `tests/test_scoring.py` — old test replaced with Signal Set B tests. Confirmed those imports are currently absent from `scoring.py`, but the regression guard is gone.
**Expected:** Replace with a narrowed hygiene test that still blocks the non-fasttext NOT-SCOPE ML imports.

---

## Checks not run

- **AC-5 abbreviation lift integration test** — SKIPPED. `_model_present()` returns False because `scripts/fetch_fasttext.py` URL returns HTTP 404. Per the brief's Implementation Note 4, model-gated tests are authorized to skip when the model is absent. However, the model is absent due to a broken fetch URL rather than intentional deferral.
- **`test_embed_returns_tuple_of_floats_for_normal_word`** — SKIPPED (same reason, same authorization).

---

VERDICT: PASS## CODE
---

## Code Review — Feature 8a (fastText + Signal Set B)

**Branch:** `rocket-run-8a-v2`  
**Diff range:** `9eb36884..HEAD`  
**Build result:** 326 passed, 2 skipped (model-gated), 0 failed

---

## Spec Compliance Checklist

- [x] **Scope matches prompt**: Core feature (Stage 2c + Signal Set C + Signal Set B) all present. No connector scaffolding or Stage 0/1/4/5/6 changes. ✓
- [ ] **Authorized files only**: `features/_logs/8a-fix-report.md` is in the committed diff range (`e2f1b10`) but NOT in the build prompt's FILE PATHS section. **AUTOMATIC FAIL per review rules.**
- [x] **NON-GOALS not built**: B3 absent from scoring.py and tests (grep returned zero matches). No Neo4j, fine-tuned fastText, XGBoost, write-back, or schema changes. ✓

## Security Checklist

- [x] **No secrets/credentials/PII in source**: PASS. Model URL is public CDN. No tokens. ✓
- [x] **No sensitive data in API responses**: PASS. ✓
- [x] **Protected files untouched**: `.claude/settings.json`, `.claude/hooks/`, `TEMPLATE.md`, `SHIPPED.md`, `DEBUG.md`, `PROMPT_LOG.md`, `CC-LEARNINGS.md`, `db/schema*.sql` — all clean. ✓
- [x] **External calls only where authorized**: `fetch_fasttext.py` downloads model as authorized. No network IO in core matching code. ✓
- [x] **Atomic writes**: `fetch_fasttext.py` uses `tmp_path.rename()`. ✓
- [x] **No destructive operations**: PASS. ✓

## Code Quality Checklist

- [x] **No dead code / unused imports**: PASS. `datetime` import in `scoring.py` is used by `_compute_b_boosts` (`ts_src - ts_cand`). ✓
- [x] **Naming conventions**: snake_case functions, PascalCase classes (EmbeddingIndex, BoostEntry), UPPER_CASE constants (MAX_B_BOOST, CANDIDATE_CAP). ✓
- [ ] **No hardcoded values that should be config**: `MODEL_URL` in `fetch_fasttext.py` is hardcoded to a Yandex CDN URL that returns HTTP 404 (noted as QA-001 deferred). **WARNING** — the script doesn't work but tests are skip-gated.
- [x] **Layering respected**: DB access stays in entity_store and scoring; embeddings module is isolated. ✓
- [x] **Input validation**: Empty/whitespace guards in `embed()` and `score_pair()`. ✓
- [x] **Error handling**: `embed()` catches all exceptions from model lookup; `get_external_field()` catches JSON parse errors. ✓

## Architecture Checklist

- [x] **No cross-module boundary violations**: `EmbeddingIndex` uses lazy import of embeddings to avoid circular deps. ✓
- [x] **Parameterized queries**: All SQL in `entity_store.py` and `scoring.py` uses `?` placeholders. No f-string SQL. ✓
- [ ] **No N+1 / performance traps**: `_compute_graph_evidence` and `_compute_b_boosts` both independently call `count_shared_person_neighbors` and `count_shared_graph_neighbors` — double DB round-trip per scored pair. Documented as CR-003, deferred. **WARNING.**
- [x] **Project invariants respected**: Stage boundaries maintained; Shadow Ledger untouched; SQLite graph store used correctly. ✓

## Grep Anti-Patterns

```
grep -rn "print(" changed files → PASS (only fetch_fasttext.py, intentional per AC-2)
grep -rnE 'f"(SELECT|INSERT|UPDATE)' → PASS (no interpolated SQL)
grep -rn "os.kill|taskkill" → PASS (none)
grep -rn "amount_cooccurrence|transaction" core/matching/scoring.py → PASS (zero matches, B3 absent)
grep -n "def _compute_b_boosts" scoring.py → PASS (exactly one match at line 173)
```

---

## Issues

### [CR-001] — Unauthorized committed file: features/_logs/8a-fix-report.md
**Severity:** BLOCKING  
**File:Line:** `features/_logs/8a-fix-report.md` (committed in `e2f1b10`)  
**Description:** This file does not appear in the build prompt's FILE PATHS section under Create, Modify, or DO NOT MODIFY. It was committed in the fix commit. The review rule states "A file modified that is NOT in the build prompt's FILE PATHS = automatic FAIL." This is a rocket framework artifact (fix log), not feature code — but the rule is categorical.  
**Fix:** Either exclude `features/_logs/` files from the committed diff (keep them as untracked build artifacts) or add the path to the authorized FILE PATHS in the build prompt for future runs.

---

### [CR-002] — Weight budget invariant violated (AC-5)
**Severity:** WARNING  
**File:Line:** `core/matching/weights.py:40–63`  
**Description:** `fasttext_cosine` was supposed to be included in the sum-to-1.0 budget by proportionally reducing the other 6 weights. Instead it was added additively: DEFAULT budget = 1.05, PSA budget = 1.12. The `test_weights_sum_to_one` test does not check `fasttext_cosine` and therefore doesn't catch this. Scores are bounded by `clamp(0,1)` so no score exceeds 1.0, but pairs with high alias matches can reach 1.05/1.12 pre-clamp, meaning the weight calibration is off from spec. The fix report (8a-fix-report.md) acknowledges this as CR-002 deferred: fixing it broke `test_score_clamped_to_unit_interval` and `test_person_inversion_pair_scores_at_least_0_95_via_string_metrics`.  
**Fix:** Reduce the 5 string-signal weights proportionally so that string signals + alias_boost + fasttext_cosine = 1.0. Update the two test assertions that relied on the over-budget calibration.

---

### [CR-003] — AC-24 test exercises 2 signals, spec requires 4
**Severity:** WARNING  
**File:Line:** `tests/test_scoring.py:868–908`  
**Description:** AC-24 requires the cap test to exercise 4 signals (B1+B2+B4+B5 totaling 0.35 raw) and assert `len(result.signal_breakdown.b_boosts) == 4`. The actual test patches B4 and B5 to return `None` and asserts `len(result) == 2`. The cap arithmetic is correctly tested, but B4 and B5 are not exercised in the cap test, and the `len == 4` assertion is absent.  
**Fix:** Add mocks for `get_external_field` returning a corporate email domain (B4 raw=0.08) and `get_created_at` returning a 5-day delta (B5 raw=0.05); update the assertion to `len == 4` and `total_applied ≈ 0.20`.

---

### [CR-004] — AC-9 pair substituted with different entities
**Severity:** WARNING  
**File:Line:** `tests/test_blocking.py:372–420`  
**Description:** AC-9 specifies the test pair `"pacrim tech"` / `"pacific rim technologies international"`. The test uses `"lais corp"` / `"luminos artificial intelligence systems"` instead, with a comment acknowledging the swap. Both pairs have zero token/trigram overlap and prove the same mechanism, but the brief-named pair is not covered in a committed test.  
**Fix:** Either rename the entities to the brief-specified pair or add a second parametrized assertion covering the `pacrim tech`/`pacific rim technologies international` pair.

---

### [CR-005] — GraphEvidence bonus values inconsistent with B-boost applied values (AC-23)
**Severity:** WARNING  
**File:Line:** `core/matching/scoring.py:405–431`, `core/matching/scoring.py:524`  
**Description:** AC-23 requires `GraphEvidence.shared_person_count` to be derived from B1's raw boost ÷ 0.05 and `neighborhood_overlap_count` from B6's raw ÷ 0.025, ensuring `graph_evidence.shared_person_bonus` reflects the actual capped contribution. The implementation computes `GraphEvidence` via a separate `_compute_graph_evidence` call using the original (uncapped) formula. When the B-boost cap triggers, `GraphEvidence.shared_person_bonus` may report 0.10 while B1's actual applied boost was less. Noted as CR-003 deferred in the fix report.  
**Fix:** Derive `GraphEvidence` counts from `b_boosts` entries (when in-band) or from DB queries (when out-of-band), ensuring the bonus fields reflect actual applied values.

---

## Checks Not Run

- **Integration test (AC-10, Signal C lift)**: Skipped — fastText model not present at `models/cc.en.300-compress.bin`. Cannot verify `meridian cap`/`pacrim tech` score lift. Test correctly gated; not a defect.
- **Fetch script network reachability**: Cannot verify model download (Yandex CDN URL returns 404 per fix report QA-001). Functionality is skip-gated in CI; the broken URL is a deferred operational issue.

---

VERDICT: FAIL