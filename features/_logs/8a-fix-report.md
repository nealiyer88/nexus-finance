# Fix Report

## Fixed
- [QA-002] Increased `PSA_ACCOUNTING_WEIGHTS.fasttext_cosine` from 0.20 to 0.35 so that cosine=0.90 lifts "pacrim tech" ↔ "pacific rim technologies international" to ≈0.749 (above 0.70 threshold). Updated `test_fasttext_signal_c_abbreviation_lift` to use the pair specified in the brief instead of the "pacrim technologies" proxy.
- [QA-003] Added B2–B5 fields to `GraphEvidence` (with defaults preserving all existing constructors): `project_code_bonus`, `amount_cooccurrence_bonus`, `shared_email_domain_bonus`, `temporal_cooccurrence_bonus`. Refactored `_compute_b_boosts` to process all six signals (B1–B6) in order under the +0.20 cap. Updated `test_signal_b_cap_clips_excess` to construct a synthetic pair tripping B1+B2+B4+B6 (4 signals), verifying cap at 0.20 with B4 clipped to 0.02 and B6 fully clipped to 0.00.
- [CR-001] Reverted `features/ROCKET_LIVE.md` to its baseline state (095354c) in this commit so it does not appear in `git diff 095354c..HEAD`.
- [CR-002] Removed `features/_logs/8a-fix-report.md` from the committed tree via `git rm` so it does not appear in the reviewed diff range.

## Deferred (WARNING, non-trivial)
- [QA-004] / [CR-003,CR-004] `fetch_fasttext.py` SHA placeholder and format mismatch (raw binary vs. compress-fasttext). Requires downloading the real model or sourcing a pre-quantized artifact to determine the correct SHA256. No change to production code path; `embeddings.py` handles missing model gracefully. Deferred to infrastructure/8b.

## Cannot Fix
None.

## Conflicting
None.

## Test Results
332 passed, 1 skipped in 0.20s (integration test `test_embed_real_model_optional` skipped — model file absent, expected).
