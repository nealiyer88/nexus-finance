# Hardened Design: Feature 8a — fastText Integration + Signal Set B Cap Rework

## Resolved Disagreements

**Bundle 8a (embed) + 8b (Signal B reconciliation), or split?**
**Winner: Skeptic.** Engineer confirmed B2–B5 are greenfield builds requiring new data plumbing into `score_pair` (project_code, email domain, first_seen timestamp, transaction join, candidate metadata) — not a reconciliation. Tiebreaker rule applies: Skeptic + Engineer infeasibility-of-bundle → split.

**"Reconcile, not greenfield" framing for Signal Set B.**
**Winner: Skeptic.** Engineer's grep of `scoring.py:79-100` shows only B1 (`shared_person`) and B6 (`neighborhood_overlap`) shipped. B2/B3/B4/B5 do not exist. The brief's language is factually wrong — there is nothing to reconcile for four of six signals.

**Headline success criterion (`pacrim tech` ↔ `pacific rim technologies international` >0.70) with monkeypatched `embed` in tests.**
**Winner: Design, with constraint.** Engineer says the lift is feasible if stub vectors are chosen carefully. The hardened brief now mandates a vendored stub vector table (not pure monkeypatch) sized so the abbreviation pair clears 0.70 with-Signal-C and falls below without — proves the signal is doing work without requiring CI to download the full model.

**`compress-fasttext` library pin deferred to "Phase 1 adversary debate."**
**Winner: Skeptic.** Engineer flagged transitive `gensim`/NumPy footprint as the real Apple-Silicon risk surface. Decision must be made before the build prompt is generated, not punted.

**`SignalBreakdown` dataclass refactor for itemized boosts.**
**Winner: Engineer scope reality.** Refactor is required (B1 + B6 + Signal C still need itemization). In scope for 8a; cost acknowledged.

## Engineer Flags

- **B2–B5 data plumbing not in `score_pair` signature** → **Scope reduction.** Deferred to feature 8b. Removed from 8a.
- **`AMOUNT_TOLERANCE` not defined in code** → Deferred to 8b with B3.
- **`compress-fasttext` transitive deps + Apple-Silicon CI risk** → **Workaround.** Pre-build smoke test required: install + import + load quantized model on Apple-Silicon runner before build prompt is generated. Pin library and version inline in the hardened brief.
- **`SignalBreakdown` is frozen dataclass; itemized boosts require refactor cascading through ~10 test sites** → **Accepted.** In scope.
- **`score_candidate_set` hardcoded accounting↔psa category flip** → Deferred to 8b (only matters for B2–B5).
- **Quantized model load time/memory on smallest CI runner** → Measure during smoke test; if >500MB resident or >5s load, switch to a smaller pre-quantized variant before build.

## Hardened Brief

**Feature:** 8a-fastText — pre-trained fastText integration (Stages 2c + 3 Signal C) + Signal Set B6 cap rework + `SignalBreakdown` itemized-boost refactor.

**Build:**

1. **`core/matching/embeddings.py` (NEW):** `embed(normalized_name: str) -> tuple[float, ...] | None` and `cosine(a, b) -> float`. `embed("")` and `embed("   ")` return `None`; `cosine` returns 0.0 if either arg is `None`. Pure-Python load via `compress-fasttext` (pin confirmed by pre-build smoke). Model file under `models/`, gitignored.
2. **`scripts/fetch_fasttext.py` (NEW):** Idempotent download of quantized `cc.en.300` into `models/`. Documents source + SHA.
3. **`core/matching/indices.py` (EXTEND):** Add `EmbeddingIndex` (canonical_id → vector, with alias-vector → owner-id reverse map). Built once per pipeline invocation from `canonical_entities.canonical_name` + `entity_aliases.value`. Tenant-scoped via existing `tenant_id: Optional[str] = None` pattern.
4. **`core/matching/blocking.py` (EXTEND):** Stage 2c wires `EmbeddingIndex` top-k flat-cosine scan; union with token + trigram candidates; record `embed:<rank>` blocking signal.
5. **`core/matching/scoring.py` (EXTEND):** Add `fasttext_cosine` (Signal C) to `score_pair`; surface raw cosine in `signal_breakdown`. Rework existing B6 (`neighborhood_overlap`) to enforce the +0.20 Signal-B cap after summation (B1 + B6 only for now). Itemize every applied boost with `{signal_id, raw, applied}`.
6. **`core/matching/weights.py` (EXTEND):** Add per-category-pair `fasttext_cosine` weight (PSA↔Accounting ≠ Accounting↔Accounting).
7. **`core/matching/types.py` (EXTEND):** Refactor `SignalBreakdown` to carry itemized boost list. Cascade through `tests/test_scoring.py` constructors.
8. **Tests:** `tests/test_embeddings.py` (NEW), extend `tests/test_blocking.py`, `tests/test_scoring.py`. Use vendored stub vector table sized to satisfy the abbreviation-lift criterion. One optional integration test gated on model presence.

**Success criteria:**
- `pacrim tech` ↔ `pacific rim technologies international` and `meridian cap` ↔ `meridian capital group` score **>0.70 with Signal C enabled, <0.70 with it disabled** (use `pytest.approx` for cap assertions, not `==`).
- Stage 2c surfaces the abbreviation candidate when token + trigram do not.
- `signal_breakdown` contains `fasttext_cosine` per scored pair and itemized B1/B6 boosts with cap applied after summation.
- Negative control `brightpath machine learning` vs `luminos ai` stays <0.50.
- `pytest tests/ -x --tb=short` passes with no regression.
- `requirements.txt` adds exactly one pinned dep (transitive footprint documented in commit message).

**Non-goals (descoped from original 8a):**
- B2 project-code fragment, B3 amount co-occurrence, B4 shared email domain, B5 30-day temporal window — deferred to **feature 8b** (separate data-plumbing scope into `score_pair`).
- `AMOUNT_TOLERANCE` constant — ships with 8b.
- True ANN, fine-tuned fastText, Layer-3 contextual embeddings, disposition-cutoff changes — unchanged NOT-SCOPE.

**Implementation notes that changed:**
- Library pin (`compress-fasttext` + version) decided pre-build via smoke, inlined into brief — not deferred.
- Pre-flight reads `core/matching/scoring.py` and `types.py` HEAD, inlines current B-signal + `SignalBreakdown` surface so the builder reconciles against actual code shape.
- Stub vectors are vendored, not generated by monkeypatch, so abbreviation-lift assertions test real cosine math.

## Risk Register

- **Stub vector tuning is hand-fitted** (med): vectors chosen to make the test pass may not reflect real `cc.en.300` behavior. *Mitigation:* one optional integration test against real model in nightly, gated on `models/` presence.
- **`compress-fasttext` + `gensim` NumPy ABI breaks Apple-Silicon CI** (med): transitive footprint understated. *Mitigation:* pre-build smoke is a hard gate before prompt generation.
- **`SignalBreakdown` refactor cascade overruns the session** (med): ~10 test-site constructors plus call sites. *Mitigation:* refactor in a single pass at the start of the build, not last.
- **Builder treats deferred B2–B5 as "interface stubs" and ships `_ZERO_EVIDENCE`-shaped placeholders** (low-med): bundling temptation. *Mitigation:* hardened brief explicitly lists B2–B5 as non-goals; reviewer rejects any B2–B5 code in 8a.