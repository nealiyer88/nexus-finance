# Fix Report — 8a

## Fixed

- **[QA-001]** Wrong repo-root depth in `embeddings.py`: changed `parents[3]` → `parents[2]` at line 19. `_MODEL_PATH` now resolves to `nexus-finance/models/cc.en.300.bin` (correct).
- **[CR-001]** SHA256 constant was 63 hex chars (invalid) and `_sha256()` was never called. Replaced constant with explicit 64-char placeholder (`"0" * 64`) marked for replacement after first real download. Added SHA verification call after `urlretrieve` in `main()` with `sys.exit(1)` on mismatch.
- **[CR-002]** `_weighted_score` accepted a `evidence: GraphEvidence` parameter that was never used (B-signal boosts moved into `breakdown.b_signal_boosts` by this build). Removed param from signature and call site.
- **[CR-003]** `_load_stub_vectors()` in `tests/test_blocking.py` was defined but never called; test uses inline `_STUB_VECS` dict instead. Deleted the unused helper (3 lines removed).
- **[CR-004]** `b_signal_boosts: Tuple["SignalBoost", ...]` in `types.py` used deprecated `typing.Tuple` and an unnecessary forward-reference quote. Replaced with `tuple[SignalBoost, ...]` (builtin, no quote needed — `SignalBoost` defined earlier in same file); removed `Tuple` from `typing` import.

## Cannot Fix

- **[QA-002]** B2–B5 signals absent from `GraphEvidence`, `_compute_b_boosts`, and tests. The 8a build prompt (`features/_prompts/8a.cc-prompt.md`) explicitly defers B2–B5 to feature 8b under NON-GOALS: "B2, B3, B4, B5 signals — deferred to feature 8b. No stubs, no placeholders, no `_ZERO_EVIDENCE`-shaped scaffolding." Implementing B2–B5 would contradict the build prompt scope. This is a conflict between the QA reviewer's interpretation of the V1 architectural spec (which lists B1–B6) and the 8a build prompt's explicit deferral. Needs human decision.

## Deferred (WARNING, non-trivial)

- **[QA-003]** `fetch_fasttext.py` downloads the 6.7 GB raw fastText binary but `embeddings.py` loads via `CompressedFastTextKeyedVectors.load()` which expects the compress-fasttext format. Resolving this requires either changing the download URL/source to a pre-quantized compress-fasttext model or adding a quantization step. Architectural decision about model source required; deferred to feature 8b or infra task.
- **[QA-004]** Stage 2c test seeds `"northwest software"` instead of the brief's `"pacific rim technologies international"`. The builder's justification is valid: the brief's pair shares trigrams with `"pacrim tech"`, defeating the isolation test. Proxy is mathematically sound. Deferred — low risk.

## Test Results

```
332 passed, 1 skipped in 0.15s
```

No regressions. The 1 skipped test is the integration test gated on model file presence (expected).
