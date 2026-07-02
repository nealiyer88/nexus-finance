# Fix Report — 8a

## Fixed

- **[CR-001]** Reverted `rocket.sh` to pre-feature base SHA (`9eb36884`) via
  `git checkout 9eb36884 -- rocket.sh`. The Fable-5 fallback and heartbeat-loop
  infra commits (`d6565e3`, `8740c7e`) are no longer part of the feature file diff;
  they remain in history as separate infra commits but `rocket.sh` is now at its
  pre-build state so it does not appear in `git diff 9eb36884..HEAD -- rocket.sh`.

- **[QA-002]** Added a docstring note in
  `tests/test_blocking.py::test_embedding_stage_2c_surfaces_candidate` (lines
  378–382) cross-referencing the brief's named pair (`pacrim tech` / `pacific rim
  technologies international`), so a future reader can trace the test back to AC-9.

## Deferred (WARNING, non-trivial or unverifiable)

- **[QA-001]** Fetch script URL (`storage.yandexcloud.net`) returns HTTP 404. Correct
  working URL cannot be verified without live network access; introducing a second
  broken URL would make the integration tests worse. Deferred to the next human-review
  cycle to confirm a working CDN or Hugging Face URL.

- **[CR-003]** Double DB query for GraphEvidence. Fixing this requires restructuring
  `score_pair` in `scoring.py` beyond a trivial change — `_compute_graph_evidence`
  must either be eliminated or replaced with derivation from b_boost raw values, which
  also affects the out-of-ambiguous-band path. Non-trivial; deferred.

## Conflicting (fix broke tests, reverted)

- **[CR-002]** Weight-budget fix (making `fasttext_cosine` in-budget by proportionally
  scaling the other 6 weights) broke two existing tests:
  - `test_score_clamped_to_unit_interval` — expected `score == 1.0` (clamp hit); with
    reduced weights score dropped to 0.948, never reaching the clamp.
  - `test_person_inversion_pair_scores_at_least_0_95_via_string_metrics` — expected
    `score >= 0.95`; dropped to 0.88 after weight reduction.
  Reverted `weights.py` and `test_scoring.py` to pre-fix values. Resolving CR-002
  requires re-anchoring the two threshold-sensitive tests to the new weight values,
  which is a scoring-behavior change beyond the fixer's scope.

## Test Results

```
326 passed, 2 skipped in 0.18s
```
(2 skipped = model-gated integration tests, by design per Implementation Note 4)
