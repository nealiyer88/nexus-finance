All evidence gathered. Producing the verdict now.

---

## Code Review — Feature 8a: fastText Signal C + Signal Set B Cap

**Diff range:** `095354c..HEAD`
**Test result:** 332 passed, 1 skipped — green

---

## Spec Compliance

- **Authorized files only:** PASS — all 13 changed files match the authorized FILE PATHS list. No protected files touched.
- **Scope expansion:** FAIL — see CR-001 below.
- **NON-GOALS not built:** FAIL — see CR-001 below.

---

## Security Checklist

- **No secrets / credentials in source:** PASS — no tokens, credentials, or PII found across all changed files.
- **No sensitive data in API/CLI responses:** PASS
- **Protected files untouched:** PASS — `.claude/settings.json`, hooks, agents, `TEMPLATE.md`, log files, Stage 0/1/4/5/6 modules, connectors all unmodified.
- **External calls only where authorized:** PASS — `fetch_fasttext.py` is not imported by any production module; embedding calls are internal.
- **Atomic writes for shared state:** PASS — no shared mutable state introduced.
- **No destructive operations outside scope:** PASS

---

## Code Quality Checklist

- **No dead code or unused imports:** PASS — all imports used; `Callable` added to `indices.py` is consumed by `EmbeddingIndex`.
- **Naming conventions (snake_case / PascalCase / UPPER_CASE):** PASS
- **No hardcoded values that should be config:** PASS — `B_SIGNAL_CAP`, constants all module-level.
- **Layering respected:** PASS
- **Input validation:** PASS — `embed()` guards blank/whitespace; `cosine()` guards None inputs.
- **Error handling meaningful:** PASS — `_load_model` uses structured `logging.warning`, never raises. Bare `except Exception` in `embed()` is intentional silent guard.
- **`print()` calls:** PASS — only in `scripts/fetch_fasttext.py` (CLI utility, expected).

---

## Architecture Checklist

- **Parameterized queries:** PASS — all SQL in `_iter_seed_strings` uses `?` placeholders.
- **No N+1 / performance traps:** PASS — `EmbeddingIndex.build` does a single pass; `top_k` is a flat scan (acceptable at <500 entities per spec).
- **Project invariants:** Partial — see CR-001, CR-002.
- **`_embed_fn` private access in blocking.py:** NOTE — `blocking.py:40` accesses `embedding_index._embed_fn`. This is documented as intentional in the build manifest (avoids a second `embed` import that fails when model absent). Acceptable given the documented rationale.

---

## Issues

---

### [CR-001] — B2–B5 signals scaffolded contrary to NON-GOALS
**Severity:** BLOCKING
**File:Line:**
- `core/matching/types.py:83–90` — 8 new fields added to `GraphEvidence` (`project_code_fragment_count`, `project_code_bonus`, `amount_cooccurrence_count`, `amount_cooccurrence_bonus`, `shared_email_domain_count`, `shared_email_domain_bonus`, `temporal_cooccurrence_count`, `temporal_cooccurrence_bonus`)
- `core/matching/scoring.py:307–310` — `_compute_b_boosts` reads `evidence.project_code_bonus` (B2), `evidence.amount_cooccurrence_bonus` (B3), `evidence.shared_email_domain_bonus` (B4), `evidence.temporal_cooccurrence_bonus` (B5)
- `tests/test_scoring.py:948–954` — `test_signal_b_cap_clips_excess` constructs evidence with `project_code_bonus=0.08` and `shared_email_domain_bonus=0.05`

**Description:** The build prompt NON-GOALS state explicitly: *"B2, B3, B4, B5 signals — deferred to feature 8b. No stubs, no placeholders, no `_ZERO_EVIDENCE`-shaped scaffolding."* The original `GraphEvidence` had exactly 4 fields (B1 + B6 counts and bonuses, confirmed via `git show 095354c:core/matching/types.py`). The build added 8 schema fields and wired them into `_compute_b_boosts`, exactly matching the prohibited pattern. These fields are always 0.0 in production (since `_compute_graph_evidence` doesn't populate them), making them pure scaffolding stubs.

The build prompt criteria §7 specifies `_compute_b_boosts` with **only B1 and B6**. The builder extended to all six without authorization.

**Fix:** Remove the 8 B2–B5 fields from `GraphEvidence` in `types.py`. Reduce `_compute_b_boosts` to only B1 and B6 as specified in criteria §7. Update `test_signal_b_cap_clips_excess` to use only `shared_person_bonus` and `neighborhood_overlap_bonus` in the evidence, matching the criteria §11 fixture (`shared_person_bonus=0.15, neighborhood_overlap_bonus=0.10`, asserting `b6.applied=0.05`).

---

### [CR-002] — `PSA_ACCOUNTING_WEIGHTS.fasttext_cosine` is 0.35, spec says 0.20
**Severity:** WARNING
**File:Line:** `core/matching/weights.py:61`
**Description:** Criteria §6 explicitly states `PSA_ACCOUNTING_WEIGHTS.fasttext_cosine = 0.20`. The build sets it to 0.35. The builder's reasoning (manifest "Assumptions") is that the spec's own math check shows `0.43 (base) + 0.18 (ft@0.20) = 0.61 < 0.70`, which would fail the abbreviation-lift acceptance criterion. The builder chose to raise the weight to make the required test pairs pass rather than use intermediate test pairs. This resolves an internal spec contradiction (criteria §6 vs. criteria §10 are incompatible for "pacrim tech"↔"pacific rim technologies international") but deviates from a named constant value. The correct resolution per the spec's own hint was to use "pacrim technologies" as the test pair, not change the weight.
**Fix:** Revert `fasttext_cosine` to `0.20` in `PSA_ACCOUNTING_WEIGHTS`. Replace the "pacrim tech" ↔ "pacific rim technologies international" pair in the abbreviation-lift test with "pacrim tech" ↔ "pacrim technologies" (cosine=0.9, base≈0.658, total≈0.838 > 0.70 with w=0.20).

---

### [CR-003] — `fetch_fasttext.py` SHA256 placeholder will always fail on real download
**Severity:** WARNING
**File:Line:** `scripts/fetch_fasttext.py:429`
**Description:** `_CC_EN_300_GZ_SHA256 = "0" * 64` is an all-zeros placeholder. The script verifies SHA after download and calls `gz_path.unlink()` + `sys.exit(1)` on mismatch. Any real invocation will download, delete, and exit 1. The docstring documents this as a placeholder ("replace with verified value after first download"), but the script as shipped is non-functional. Additionally, the download URL (`cc.en.300.bin.gz`) provides the raw fastText binary format, not the `compress-fasttext` quantized format; `CompressedFastTextKeyedVectors.load()` expects the latter.
**Fix:** Document the format mismatch in the script header. The correct path to generate a `compress-fasttext`-compatible model is: download → `compress_fasttext.prune_ft_freq` (or equivalent compress step). Leave SHA as placeholder with a clear `# TODO: fill after generating compressed model` comment.

---

### [CR-004] — `test_blocking.py` deviates from criteria §5 spec
**Severity:** WARNING
**File:Line:** `tests/test_blocking.py:508–560`
**Description:** Criteria §5 requires: (a) `_load_stub_vectors()` helper reading `tests/fixtures/stub_vectors.json`; (b) canonical seeded as "pacific rim technologies international". The test defines inline vectors and seeds "northwest software" as the canonical. The test validates the correct behavioral property (embedding surfaces a candidate that token+trigram cannot), but the spec-mandated helper is absent and the canonical name differs from the specification. The test also doesn't call `_load_stub_vectors()` — criteria §5 item 4 requires this explicitly.
**Fix:** Add `_load_stub_vectors()` helper matching `tests/test_scoring.py`'s implementation. Optionally align the seeded canonical name with criteria §5 ("pacific rim technologies international").

---

### [CR-005] — `test_signal_b_cap_clips_excess` evidence deviates from criteria §11
**Severity:** WARNING
**File:Line:** `tests/test_scoring.py:948–965`
**Description:** Criteria §11 specifies `GraphEvidence(shared_person_count=3, shared_person_bonus=0.15, neighborhood_overlap_count=4, neighborhood_overlap_bonus=0.10)` and asserts `b6.applied == 0.05`. The test uses `shared_person_bonus=0.10`, adds `project_code_bonus=0.08` and `shared_email_domain_bonus=0.05` (B2/B4 stubs), and asserts `b6.applied == 0.00`. The test still validates the cap invariant but uses a four-signal scenario that requires the prohibited B2–B5 schema (tied to CR-001).
**Fix:** Align with criteria §11 exactly: `shared_person_bonus=0.15, neighborhood_overlap_bonus=0.10`; assert `b6.applied ≈ 0.05` (clipped to remaining 0.05 after B1 consumed 0.15).

---

## Checks not run

None — all checklist items executed against the build diff.

---

## Summary

One BLOCKING issue (CR-001): B2–B5 signal slots were added to `GraphEvidence` and wired into `_compute_b_boosts` in direct violation of the build prompt's NON-GOALS prohibition on stubs and scaffolding for these deferred signals. The remaining findings are warnings. Tests are green, security properties are satisfied, and all other acceptance criteria pass.

VERDICT: FAIL