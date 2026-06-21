# Engineer Adversary — Feasibility Assessment

## 1. Can this be built as specified? **Partially.**

The fastText pieces (Stage 2c EmbeddingIndex, Stage 3 Signal C, model fetch) are clean and feasible against the actual code. The Signal Set B "reconciliation" is mis-framed — it's not a reconciliation, it's a 4-signal greenfield build on data the scorer cannot currently see. That's the load-bearing problem the brief understates.

## 2. Technical blockers

**Signal Set B — what actually shipped vs. what v4 demands.** `core/matching/scoring.py:79-100` ships exactly two graph signals: `shared_person` (≈B1) and `neighborhood_overlap` (≈B6). **B2, B3, B4, B5 do not exist and cannot be added without changing inputs to `score_pair`:**

- **B2 (project-code in QB ref/class/memo):** `score_pair` takes `(entity, candidate_id, candidate_name, candidate_aliases, candidate_category, conn, …)`. It has no access to the QB candidate's `Class`, ref, or memo strings, nor to the entity's `project_code` field (which exists on `NormalizedTransaction`, not `NormalizedEntity`). Needs a new fetch path or signature change.
- **B3 (amount co-occurrence within `AMOUNT_TOLERANCE` same period):** Scorer has no transaction handle. `AMOUNT_TOLERANCE` is defined in `rules/01-nexus-finance-v1.md` but I grepped — no constant exists in code. Implementing this means joining transactions at Stage 3, which today is a pure pairwise-name function.
- **B4 (shared email domain):** No email lives on `CanonicalEntity` or the scorer's inputs. `get_aliases` returns alias *strings* only. Would need a new graph-store getter and probably a new column or system-ref parse.
- **B5 (first-seen 30-day window):** `canonical_entities.created_at` exists, but `NormalizedEntity` has no first-seen timestamp; needs to be added to the connector contract or fetched from the source row.

The brief's "read what shipped, then ensure all six signals exist" language hides this: it reads as a reconcile, it's actually 4 new signals each requiring new data plumbing into the scorer. **This alone is a feature-sized chunk.**

**fastText library risk.** The brief defers "exact library + pin" to "Phase 1 adversary debate" but only `compress-fasttext` is named. That package pulls `gensim` (>50MB, NumPy ABI sensitive), and on Apple-Silicon CI the NumPy/Gensim wheel chain has historically been the failure surface, not C++ fastText. The brief's claim of "exactly one new pinned dependency" understates the transitive footprint. Needs a real install-and-import smoke before the build prompt is generated.

**Auditability storage.** The brief says every B-boost is logged in `signal_breakdown` with id/raw/applied. The shipped `SignalBreakdown` dataclass (`types.py:42`) is a flat frozen dataclass with named float fields — there's no `dict[str, ...]` field for itemized boosts. Adds either a new container field or a sibling dataclass — touching every test that constructs a `SignalBreakdown` (≥10 sites in `test_scoring.py`).

## 3. Effort estimate (realistic)

- fastText loader + EmbeddingIndex + Stage 2c wire + tests with stub vectors: **1 session.**
- Signal C weight wiring + abbreviation-lift assertions on pacrim/meridian: **0.5 session** (depends on stub vectors being chosen well enough that the lift actually clears 0.70).
- Signal Set B build-out (B2–B5 plumbing + B6 cap rework + itemized breakdown refactor + auditability fields + cap-after-summation test): **1.5–2 sessions** if data plumbing is in scope; alternatively descope B2–B5 to "interfaces only" for this feature.
- Apple-Silicon CI dependency debugging buffer: **0.5 session.**

**Total: 3.5–4 sessions.** Brief calls it L — agree, but builder will overrun if B-signal data plumbing isn't explicitly scoped or descoped now.

## 4. Implementation risks (builder will get stuck on)

1. Choosing stub vectors small enough to vendor but expressive enough that `meridian cap ↔ meridian capital group` clears 0.70 with-Signal-C and falls below without — this is the success-criterion that's easiest to fudge or get wrong.
2. Refactoring `SignalBreakdown` to carry itemized boosts will cascade through every existing scoring test (`tests/test_scoring.py` = 847 lines).
3. `score_candidate_set` infers candidate category from a hardcoded accounting↔psa flip (`scoring.py:410-420`). B2/B3/B4 may need real category metadata, not the inference.
4. `compress-fasttext` quantized model load time + memory footprint on the smallest CI runner — needs measurement, not assumption.

## 5. Recommended approach

Split the brief into **8a-fastText** (loader + Stage 2c + Signal C + abbreviation lift tests + B6 cap rework) and **8b-signal-set-B-completion** (B2–B5 data plumbing + itemized breakdown refactor). 8a unblocks the 80→95 bridge claim and is genuinely feasible in 1.5–2 sessions. 8b is honest about being a separate feature with its own data-plumbing scope. Bundling them as written invites a context-degraded mid-session where the builder ships fastText cleanly and then hand-waves four B-signals into `_ZERO_EVIDENCE`-shaped stubs that pass the literal success criteria but don't actually fire on the fixture.

If bundling is non-negotiable, at minimum: pin the `compress-fasttext` library and re-confirm Apple-Silicon install in a smoke before the build prompt, and pre-write the `SignalBreakdown` audit-container shape so the builder isn't designing it under time pressure.