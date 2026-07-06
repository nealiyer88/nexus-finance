# Fix Report — 8a (Round 2)

## Fixed

- [CR-001] Removed `features/_logs/8a-fix-report.md` from git tracking (`git rm --cached`). File was committed in `e2f1b10` but is not in the build prompt's authorized FILE PATHS. BLOCKING issue resolved.
- [QA-005] Restored narrowed hygiene guard `test_no_xgboost_no_llm_no_deep_learning_in_scoring` in `tests/test_scoring.py`. Checks for `xgboost`, `anthropic`, `openai`, `fastembed`, `sentence_transformers`, `torch`, `transformers` — but NOT `fasttext` (compress-fasttext is IN SCOPE per v4 §1). Replaces the removed `test_no_xgboost_no_fasttext_no_llm_in_scoring`.
- [CR-003] Fixed AC-24 cap test to exercise 4 signals (B1+B2+B4+B5) as spec requires. Mocked `get_external_field` → `"alice@acmecorp.com"` (corporate domain, B4 raw=0.08) and `get_created_at` → side_effect with 5-day delta (B5 raw=0.05). Total raw=0.35 capped to 0.20. Asserts `len==4` and `sum(applied)≈0.20`.

## Deferred (WARNING, non-trivial)

- [QA-001] Fetch script Yandex CDN URL returns HTTP 404. Cannot replace without live network verification; substituting a wrong URL would make integration tests permanently fail. Operational issue.
- [QA-002 / CR-004] AC-3/AC-9 brief pair substitution — prior round added cross-reference comment. Renaming to `pacrim tech`/`pacific rim technologies international` requires trigram bypass scaffolding (the pair shares trigrams). Beyond 5-line threshold.
- [QA-003 / CR-002] Weight budget over-subscription. Proportional reduction broke `test_score_clamped_to_unit_interval` and `test_person_inversion_pair_scores_at_least_0_95_via_string_metrics`. Requires scoring-behavior decisions beyond fixer scope.
- [QA-004 / CR-005] Double DB query / GraphEvidence inconsistency — restructuring `_compute_graph_evidence` to derive from `b_boosts` is non-trivial. Deferred.

## Test Results

327 passed, 2 skipped (model-gated, authorized per build prompt Implementation Note 4)
