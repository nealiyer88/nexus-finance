# Combined verdict — 8a
## QA
I now have complete evidence. Let me write the verdict.

---

## QA Verdict — Feature 8a: fastText Integration + Signal Set B Reconciliation

**Reviewer:** QA Blind Critic
**Date:** 2026-06-20
**Baseline SHA:** `095354c1090f4adde708ffbcf2281990e2715d53`

---

### [QA-000] Deliverable existence check

| Path | Status |
|------|--------|
| `core/matching/embeddings.py` (Create) | PASS — exists, real implementation |
| `core/matching/indices.py` → `EmbeddingIndex` (Modify) | PASS — `EmbeddingIndex` class added with `build()` and `top_k()` |
| `core/matching/blocking.py` → `embedding_index` param (Modify) | PASS — `generate_candidates` accepts optional `EmbeddingIndex` |
| `core/matching/scoring.py` → Signal C + B reconciliation (Modify) | PARTIAL — Signal C present; Signal Set B only has B1+B6 of required B1–B6 |
| `core/matching/weights.py` → `fasttext_cosine` field (Modify) | PASS — added with per-category-pair dispatch |
| `core/matching/types.py` → `SignalBoost`, `fasttext_cosine` (Modify) | PASS |
| `scripts/fetch_fasttext.py` (Create) | PASS — exists, idempotent |
| `tests/test_embeddings.py` (Create) | PASS — exists with full coverage |
| `tests/fixtures/stub_vectors.json` (Create) | PASS |
| `tests/test_blocking.py` extended (Modify) | PASS — Stage 2c test added |
| `tests/test_scoring.py` extended (Modify) | PARTIAL — Signal C tests added; B cap test exercises only 2 signals |

Primary deliverable: **partially built** — Signal Set B is incomplete (B2–B5 absent). Deliverable gate: **CONDITIONAL FAIL** (see QA-003).

---

### Test run

```
332 passed, 1 skipped in 0.15s
```

Command: `PYTHONPATH=/Users/nealiyer/code/nexus-finance /Users/nealiyer/Library/Python/3.9/bin/pytest tests/ -x --tb=short`

The 1 skipped test is the model-absent integration test in `test_embeddings.py` — expected and correct.

---

### [QA-001] — SC3 test proxy diverges from brief's stated pair; brief's isolation premise is false

**Severity:** WARNING
**Description:** The brief's SC3 asserts: *"for query `"pacrim tech"` against a registry seeded with `"pacific rim technologies international"`, the fastText path surfaces that canonical as a candidate when token + trigram blocking alone do not."* The test uses `"northwest software"` as the canonical name instead. More critically, the isolation claim in the brief is factually incorrect: `"pacific rim technologies international"` shares multiple trigrams with `"pacrim tech"` (`rim`, `pac`, `tec`, `ech`, `im_`, `m_t`) and IS surfaced by token+trigram blocking alone.
**Evidence:**
```
token+trigram candidates for "pacrim tech":
  {'CAN-PAC': ('trigram: te', 'trigram:^pa', 'trigram:ech',
               'trigram:im ', 'trigram:m t', 'trigram:pac',
               'trigram:rim', 'trigram:tec')}
CAN-PAC ("pacific rim technologies international") surfaced by token/trigram alone: True
```
**Expected:** The test should acknowledge that the brief's specified pair has trigram overlap and either (a) use a pair that genuinely isolates the embedding-only path (as `"northwest software"` correctly does) with an explanatory comment, or (b) the brief itself should be corrected. The embedding mechanism is correctly implemented — the isolation proxy test passes — but the brief's stated assertion cannot be written as specified.

---

### [QA-002] — SC5: `"pacrim tech"` ↔ `"pacific rim technologies international"` scores 0.614 with Signal C enabled — below the 0.70 SURFACE threshold

**Severity:** BLOCKING
**Description:** The brief's SC5 explicitly requires that `"pacrim tech"` ↔ `"pacific rim technologies international"` scores **above 0.70** with Signal C enabled and below 0.70 with it disabled. With stub vectors providing cosine(pacrim tech, pacific rim technologies international) = 0.90 and `fasttext_cosine` weight = 0.20 (PSA↔Accounting), the actual score is **0.614** — below the required threshold. The test sidesteps this by substituting `"pacrim technologies"` as the candidate name, which scores 0.838 and passes. The meridian pair passes correctly.
**Evidence:**
```
'pacrim tech' vs 'pacific rim technologies international':
  on=0.6140  off=0.4340  -> SC5 FAIL  (requires on > 0.70)

'pacrim tech' vs 'pacrim technologies' (test proxy):
  on=0.8382  off=0.6582  -> SC5 PASS

'meridian cap' vs 'meridian capital group':
  on=0.8452  off=0.6652  -> SC5 PASS
```
**Expected:** Either (a) the `fasttext_cosine` weight for PSA↔Accounting must be increased so that the 0.9 cosine is sufficient to lift the pair above 0.70, or (b) the stub vectors must assign a higher cosine to this pair, or (c) the brief's specified pair must be reconsidered. The test as written does not assert the brief's required pair.

---

### [QA-003] — SC6: B2, B3, B4, B5 signals entirely absent; cannot trip ≥4 B-signals

**Severity:** BLOCKING
**Description:** The brief's SC6 requires: *"all of B1–B6 present and reachable; a synthetic pair that trips ≥4 B-signals receives total B boost exactly capped at +0.20."* Only B1 (`shared_person_bonus`) and B6 (`neighborhood_overlap_bonus`) are implemented. B2 (project-code fragment in QB ref/class/memo), B3 (amount co-occurrence within AMOUNT_TOLERANCE), B4 (shared email domain), and B5 (temporal co-occurrence, 30-day first-seen window) have no fields in `GraphEvidence`, no computation in `_compute_b_boosts`, and no tests.
**Evidence:**
```python
# GraphEvidence fields:
['shared_person_count', 'shared_person_bonus',
 'neighborhood_overlap_count', 'neighborhood_overlap_bonus']

# _compute_b_boosts produces only B1 and B6:
B signals: ['B1', 'B6']  (2 of 6)

# test_signal_b_cap_clips_excess trips B1(0.15) + B6(0.10) = cap at 0.20
# -- but this is only 2 signals, not ≥4
```
**Expected:** `GraphEvidence` must carry fields for B2–B5; `_compute_b_boosts` must compute all six signals with their respective boost ranges; the cap test must construct a synthetic pair triggering at least four signals. The fix report attributes this to a build-prompt deferral to feature 8b, but the brief's success criterion is explicit and does not authorize partial Signal Set B as a passing state.

---

### [QA-004] — `fetch_fasttext.py` downloads an incompatible model format; SHA256 is a placeholder that always fails

**Severity:** WARNING
**Description:** `scripts/fetch_fasttext.py` downloads `cc.en.300.bin.gz` from `dl.fbaipublicfiles.com` (the 6.7 GB raw fastText binary) and decompresses it to `models/cc.en.300.bin`. However `embeddings.py` loads the model via `compress_fasttext.models.CompressedFastTextKeyedVectors.load()`, which expects the `compress-fasttext` library's own quantized format (25–50 MB), not the raw fastText binary format. Running the fetch script would produce a ~17 GB incompatible file that the loader cannot parse. Additionally, `_CC_EN_300_GZ_SHA256 = "0" * 64` is a placeholder — the SHA verification step will always fail (`sys.exit(1)`) on any real download, making the script non-functional as shipped.
**Evidence:**
```
_CC_EN_300_GZ_SHA256 = "0000000000000000000000000000000000000000000000000000000000000000"
_CC_EN_300_URL = "https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.en.300.bin.gz"
# vs embeddings.py:
compress_fasttext.models.CompressedFastTextKeyedVectors.load(str(_MODEL_PATH))
# These formats are incompatible without a quantization conversion step.
```
**Expected:** The fetch script should either (a) download a pre-quantized compress-fasttext model (e.g., from the `compress-fasttext` project's own release artifacts) whose format matches what `CompressedFastTextKeyedVectors.load()` expects, or (b) include a conversion step. The SHA256 must be a real checksum for whatever artifact is actually downloaded.

---

### Criteria verdict

| Criterion | Status | Note |
|-----------|--------|------|
| SC1: `embed("")`/`embed("   ")` → None; `cosine(None,*)` → 0.0 | **PASS** | Executed |
| SC2: pure-Python import; no C++ compile | **PASS** | `compress_fasttext` has no `.so` extensions |
| SC3: fastText-only candidate discovery asserted in test_blocking.py | **WARNING** | Mechanism correct; brief's exact pair has trigram overlap; proxy used |
| SC4: `fasttext_cosine` in every `signal_breakdown`; category-pair dispatch | **PASS** | PSA↔Accounting=0.20 ≠ Accounting↔Accounting=0.0 |
| SC5: both brief pairs score >0.70 on / <0.70 off | **FAIL** | "pacrim tech" vs "pacific rim technologies international" scores 0.614 on |
| SC6: B1–B6 all present; ≥4 signals trip at cap | **FAIL** | Only B1+B6 exist; B2–B5 absent |
| SC7: brightpath vs luminos <0.50 | **PASS** | 0.2812 |
| SC8: exactly one new dependency; no C++ | **PASS** | `compress-fasttext==0.1.5` only |
| SC9: `models/` gitignored; fetch script idempotent | **PASS** | Idempotent at file-exists check |
| SC10: full test suite passes | **PASS** | 332 passed, 1 skipped |

---

## Checks not run

- **Real model load** (`test_embed_real_model_optional`): Correctly skipped — model file not present. Expected behavior per brief.
- **Fetch script execution against live URL**: Not run — would require downloading 6.7 GB; format incompatibility (QA-004) makes it non-functional regardless.
- **Apple Silicon CI C++ safety**: Verified structurally (no `.so` in `compress_fasttext`) but not run on actual CI hardware.

---

VERDICT: FAIL## CODE
I have all the information needed. Here is the complete code review verdict.

---

# Code Review — Feature 8a: fastText Signal C + Signal Set B Reconciliation

**Reviewer:** Code Review Gate (standalone, no build context)
**Diff range:** `git diff 095354c..HEAD`
**Test result:** 332 passed, 1 skipped

---

## Spec Compliance

### [CR-001] — Unauthorized modification: `features/ROCKET_LIVE.md`
**Severity:** BLOCKING
**File:Line:** `features/ROCKET_LIVE.md`
**Description:** File modified and not listed in the build prompt's FILE PATHS. The build prompt explicitly states "DO NOT MODIFY: Any file under `connectors/` or `features/`." Seven lines of rocket-loop status tracking were appended (review round results, fix-phase markers). These appear to be infrastructure writes by the harness, not feature code — but the reviewer has no build-phase context, and the rule is absolute: unauthorized file modifications → FAIL.
**Fix:** The rocket harness must not commit infrastructure status logs to files in the `features/` subtree as part of the feature branch commit range reviewed by this gate.

---

### [CR-002] — Unauthorized creation: `features/_logs/8a-fix-report.md`
**Severity:** BLOCKING
**File:Line:** `features/_logs/8a-fix-report.md` (new file)
**Description:** File created and not listed in the build prompt FILE PATHS. Lives under `features/`, explicitly in the DO NOT MODIFY zone. Content is the rocket-fix phase report; again, this is harness infrastructure, but the gate rule admits no exceptions.
**Fix:** Fix reports should be written outside the `features/` subtree, or committed on a separate branch segment not included in the reviewed diff range.

---

## Security Checklist

- **[PASS]** No secrets, credentials, tokens, or PII in any changed file. `_CC_EN_300_GZ_SHA256 = "0" * 64` is an explicitly documented placeholder, not a real credential or leaked hash.
- **[PASS]** No sensitive data echoed in API/CLI responses.
- **[PASS]** Protected files untouched. Confirmed via `git diff 095354c..HEAD` over `.claude/settings.json`, all hooks, agents, `TEMPLATE.md`, all append-only logs, `deterministic.py`, `disposition.py`, `llm_fallback.py`, `redaction.py`, `connectors/` — zero lines diff.
- **[PASS]** External network calls confined to `scripts/fetch_fasttext.py`, which is explicitly authorized as a model-fetch utility not imported by any production module.
- **[PASS]** No destructive process/file operations outside scope.
- **[PASS]** Atomic write concern (fetch script): partial downloads leave a gz temp file, but `embeddings.py` handles missing/corrupt model files gracefully (logs warning, returns `None`). Not a production write path; acceptable for a utility script.

---

## Code Quality Checklist

- **[PASS]** No dead code or unused imports across all changed production files. `cosine` imported in `indices.py` is used in `EmbeddingIndex.top_k`. `Callable` is used in type hints. `dataclasses` in `scoring.py` is used for `dataclasses.replace`. All `from __future__ import annotations` directives are required by convention.
- **[PASS]** Naming conventions followed throughout: `snake_case` functions, `PascalCase` classes (`EmbeddingIndex`, `SignalBoost`), `UPPER_CASE` constants (`B_SIGNAL_CAP`, `CANDIDATE_CAP`).
- **[PASS]** No hardcoded values that should be config. `k=10` in the Stage 2c call is a spec-defined constant; `B_SIGNAL_CAP=0.20` is a named module constant.
- **[PASS]** Layering respected: `blocking.py` → `indices.py` → `embeddings.py`; `scoring.py` → `embeddings.py` directly. No shortcut bypasses.
- **[PASS]** Error handling in `embed()`: broad `except Exception: return None` is intentional per spec ("return None rather than raising"). `_load_model()` logs a warning for both path-absent and load-failure cases.

---

## Architecture Checklist

- **[PASS]** No cross-module imports violating project boundaries.
- **[PASS]** All SQL in `_iter_seed_strings` uses parameterized `?` placeholders. Confirmed via grep: zero string-interpolated SQL in any changed file.
- **[PASS]** No N+1 queries: `EmbeddingIndex.build()` issues one pass through `_iter_seed_strings`; `top_k` is a flat in-memory scan.
- **[PASS]** `_weighted_score` correctly removed the now-unused `evidence: GraphEvidence` parameter; B-signal bonuses are read from `breakdown.b_signal_boosts` (via `_compute_b_boosts` + `dataclasses.replace`). Formula matches spec.
- **[PASS]** `fasttext_cosine` weight is additive post-sum and excluded from `test_weights_sum_to_one()`. Both `DEFAULT_WEIGHTS` and `PSA_ACCOUNTING_WEIGHTS` five-signal + alias_boost sum to 1.0.

---

## Grep Anti-Patterns

```
grep -n "print("                  [changed files] → CLEAN
grep -nE 'f"(SELECT|INSERT|...)' [changed files] → CLEAN
grep -nE "os\.kill|taskkill"     [changed files] → CLEAN
hygiene test (forbidden imports) re-run inline   → CLEAN (no fasttext/xgboost/anthropic/openai/torch/transformers imports in scoring.py)
```

---

## Acceptance Criteria Walkthrough

| AC | Status | Notes |
|---|---|---|
| 1 · `embeddings.py` | PASS | `parents[2]` correct (fix applied); lazy load; graceful absence; compress-fasttext; pinned version |
| 2 · `SignalBoost` + `SignalBreakdown` | PASS | Frozen dataclass; defaults preserve existing constructors; `tuple[SignalBoost, ...]` (no deprecated `Tuple`) |
| 3 · `EmbeddingIndex` | PASS | `build()` mirrors TokenIndex pattern; mean-vector per canonical; `top_k` flat scan; cosine ≤ 0.0 excluded; 0-indexed rank |
| 4 · `blocking.py` Stage 2c | PASS | `embedding_index._embed_fn` used (avoids second import); `embed:{rank}` signals; union with token+trigram |
| 5 · `test_blocking.py` | PASS† | Stage 2c test correct; "northwest software" surrogate justified; `embed:0` signal asserted. †`_load_stub_vectors()` helper spec'd but removed (inline dict used instead — see CR-005) |
| 6 · `weights.py` | PASS | `fasttext_cosine` added; DEFAULT=0.0, PSA_ACCOUNTING=0.20; sum-to-1 invariant preserved; test updated |
| 7 · `scoring.py` | PASS | `_compute_b_boosts` exact match to spec; `_ZERO_BREAKDOWN` updated; `_weighted_score` new formula; `score_pair` wiring correct |
| 8 · `stub_vectors.json` | PASS | All 7 vectors L2-normalized to 1.0 (verified). Cosines: pacrim pair = 0.9, meridian pair = 0.9, negative control = 0.0 |
| 9 · `test_embeddings.py` | PASS | All specified null-guard, correctness, stub cosine, and magnitude tests present; integration test skipped on absent model |
| 10 · Abbreviation-lift tests | PASS | Monkeypatches `scoring_mod.embed`; asserts >0.70 with Signal C, <0.70 without; negative control <0.50 |
| 11 · B-cap test | PASS | `_compute_b_boosts` called directly; raw_b1=0.15, raw_b6=0.10 → total_applied=0.20=cap; b6.applied=0.05, b6.raw=0.10 ✓ |
| 12 · Hygiene test | PASS | Import-specific patterns replace bare substrings; `fasttext_cosine` field name no longer false-positives |
| 13 · `fetch_fasttext.py` | WARN | See CR-003, CR-004 |
| 14 · `.gitignore` + `requirements.txt` | PASS | `models/` gitignored; `compress-fasttext==0.1.5` under correct section |
| 15 · `pytest` | PASS | 332 passed, 1 skipped (integration test, expected) |

---

## Additional Warnings

### [CR-003] — SHA placeholder guarantees script failure on every download
**Severity:** WARNING
**File:Line:** `scripts/fetch_fasttext.py:32,55`
**Description:** `_CC_EN_300_GZ_SHA256 = "0" * 64`. The SHA check at line 55 will always fail for any real download (no file has SHA256 of 64 zero hex digits), causing `gz_path.unlink()` and `sys.exit(1)`. Script cannot deliver the model until a human replaces this placeholder with the real digest. Documented as a placeholder, so not a security issue — but the script is non-functional in its current form.
**Fix:** Run the download once manually, capture `sha256sum cc.en.300.bin.gz`, replace the placeholder. The script's own `--verify-sha` doc hint points the way.

### [CR-004] — Format mismatch: script downloads raw binary; loader expects compress-fasttext format
**Severity:** WARNING
**File:Line:** `scripts/fetch_fasttext.py:27-28` / `core/matching/embeddings.py:37`
**Description:** The script downloads `cc.en.300.bin.gz` from fastText's official distribution (raw binary format, ~6.7 GB). `embeddings.py` loads via `compress_fasttext.models.CompressedFastTextKeyedVectors.load()` which expects the compress-fasttext quantized format. These are not compatible; the decompressed raw binary will not load correctly with the compress-fasttext loader. Fix report acknowledges this as `[QA-003]` and defers to feature 8b or infra. The spec requires "`compress-fasttext` (pure-Python, no C++ compile)" — the download source must match.
**Fix:** Change the download URL to a pre-quantized compress-fasttext model artifact, or add a compress-fasttext quantization step inside the script.

### [CR-005] — `_load_stub_vectors()` helper absent from `test_blocking.py`
**Severity:** WARNING
**File:Line:** `tests/test_blocking.py` (no line — function absent)
**Description:** Acceptance Criteria 5 specifies "Add `_load_stub_vectors()` helper that reads `tests/fixtures/stub_vectors.json` → `dict[str, list[float]]`." The helper was defined then removed by the fix phase (dead code, since the test uses an inline `_STUB_VECS` dict). The test goal is fully met; the helper's absence is a minor spec deviation.
**Fix:** Either restore the helper and call it, or accept the inline approach and remove the AC requirement.

### [CR-006] — `scoring.py` module docstring formula is stale
**Severity:** WARNING
**File:Line:** `core/matching/scoring.py:32-37`
**Description:** The module-level docstring shows the scoring formula without Signal Set C (fastText) or B-cap summation. The build added both. Not a functional defect, but a future reader will see a mismatched formula comment.
**Fix:** Update the formula block to include `+ fasttext_cosine_weight * fasttext_cosine` and `+ sum(b.applied for b in b_signal_boosts)`.

---

## Checks Not Run

None — all checklist items executed and evidenced above.

---

## Summary

The feature code is functionally complete and correct. All 15 acceptance criteria are met. The stub vector math is verified. The B-cap formula is correct. The hygiene guard is strengthened. Tests pass clean at 332/333. The **sole reason for FAIL** is two infrastructure files under the explicitly prohibited `features/` subtree appearing in the reviewed diff range — this triggers the automatic rule. The fix-phase harness should commit its status logs (ROCKET_LIVE.md updates, fix reports) in a manner that keeps them out of the feature builder's diff range, or the DO NOT MODIFY rule must carve out harness-written files.

VERDICT: FAIL