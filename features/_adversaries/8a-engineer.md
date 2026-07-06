Write blocked pending permission. Outputting the assessment inline so you can read it now; approve the write if you want it saved next to `8a-design.md`.

---

# Engineer: Feasibility of feature 8a (fastText + Signal Set B reconciliation)

## 1. Can this be built as specified? — Partially. Approach is sound, but the brief describes a shipped-code shape that does not exist and will re-trigger the 2026-06-21 deadlock unless clarified.

**What's real and works:** `entity_edges` (SAME_AS/MEMBER_OF), `system_references.external_fields` (TEXT/JSON), `canonical_entities.created_at`, `TokenIndex`/`NgramIndex`/`_iter_seed_strings`, `CandidateEntity.blocking_signals` — all present in `db/schema_sqlite.sql` and `core/matching/indices.py`. `compress_fasttext` + `gensim` import cleanly on this host, so the pure-Python loader path is viable. `_check_psa_abbreviation` at `core/matching/scoring.py:133` is real. `count_shared_person_neighbors` (B1) and `count_shared_graph_neighbors` (B6) are real at `core/graph/entity_store.py:353,395`.

**What the brief describes but doesn't exist:**

- **`_compute_b_boosts` is not in the codebase.** The shipped B-adjacent logic lives inside `_compute_signal_breakdown` (`scoring.py:222`, string signals + `alias_boost_fired` + `abbreviation_bonus_fired`) and `_compute_graph_evidence` (`scoring.py:257`). Reviewers "MUST grep the diff for `amount_cooccurrence`" against a function name the builder has to invent. This is precisely the brief/prompt disagreement that caused the 3-iteration deadlock on `feature/8a-blocked` (`CC-LEARNINGS.md:67`).
- **Only 2 of 5 targeted B-signals map to shipped code.** B1 (shared_person, +0.05×n cap +0.10) and B6 (neighborhood_overlap, +0.025×n cap +0.10) already exist under different names. B2/B4/B5 have **zero** infrastructure — no accessor reads `external_fields` JSON, no `created_at` getter for score-time use, no email-domain util.
- **`alias_boost` and `abbreviation_bonus` are not in the v4 B1–B6 taxonomy.** The brief is silent on their fate. Leave as Set A? Reclassify? Delete? Any answer requires a call the brief doesn't make.
- **Global +0.20 cap does not exist.** `_weighted_score` (`scoring.py:286`) sums per-signal-capped bonuses with no post-summation guard. Adding B2 (+0.12) + B4 (+0.08) + B5 (+0.05) on top of maxed B1+B6 currently could deliver +0.45. The brief says "cap AFTER summation" — needs new logic, not just wiring.

## 2. Technical blockers

- **`SignalBreakdown` is a frozen dataclass with fixed fields** (`types.py:42`). Adding `fasttext_cosine` and per-B-signal itemized logging changes the type shape. The brief doesn't specify the new shape (dict? `tuple[BoostEntry, ...]`?). Downstream code that pattern-matches this shape (Stage 4 disposition, Stage 5 prompt building) needs review.
- **B2 reuse claim is optimistic.** `_check_psa_abbreviation` returns bool from name/alias string inputs. B2 needs to parse QB `external_fields.class`/`memo` JSON and match segments against PSA `project_codes`. Different input shapes; likely a new utility, not reuse.
- **New DB reads unspecified.** B2/B4/B5 each need at least one `system_references` or `canonical_entities` read per pair. No helper exists in `entity_store.py`. Rules §12 puts DB helpers there — inline SQL in `scoring.py` violates the layering rule.
- **`compress-fasttext` transitively pulls `gensim` (+numpy/scipy).** "Exactly one new pinned dependency" is technically true but understates the wheel footprint on Apple Silicon CI.

## 3. Effort estimate — 5–6 build sessions, not 1

`embeddings.py` + fetch script (1) · Stage 2c wire-in with `embed:<rank>` signals (0.5) · Stage 3 Signal C weight-dispatched (0.5) · Signal Set B reconciliation — 3 new signals + rename/refactor of shipped B1/B6 + total-cap + itemized logging + `SignalBreakdown` extension (1.5) · tests across 3 surfaces with monkeypatched embed (1) · debug + reconciliation against shipped scoring layout (1). Complexity rating **L** is correct; single-session build is not.

## 4. Implementation risks

- **Deadlock replay.** Naming ambiguity around `_compute_b_boosts` will re-trigger the QA/CodeReview disagreement pattern from 2026-06-21.
- **Silent B-cap violation.** Without a post-sum cap test that exercises ≥4 signals simultaneously, the +0.20 invariant can silently break.
- **`SignalBreakdown` shape ripple.** `tests/test_scoring.py` is 847 lines with ~20+ `score_pair` assertions; type changes cascade.
- **Abbreviation-lift assertion is model-dependent.** "meridian cap ↔ meridian capital group scores >0.70 with C, <0.70 without" cannot be met by stub vectors — subword semantics require the real quantized model. The brief simultaneously says tests must not require the download. These constraints conflict; needs one `skipif(not model_present)` integration test.

## 5. Recommended approach

Before building, tighten the brief: (a) name the target function explicitly — `_compute_b_boosts` — and require it to be **authored** (not "modified") so the grep guard is meaningful; (b) specify what happens to `alias_boost` / `abbreviation_bonus` (recommend: leave as Set A, additive to B1–B6); (c) specify the `SignalBreakdown` extension shape (recommend: add `fasttext_cosine: float` and `b_boosts: tuple[BoostEntry, ...]` where `BoostEntry` names id/raw/applied); (d) add new `entity_store.py` getters (`get_created_at`, `get_external_field(canonical_id, field)`) so DB access stays out of `scoring.py`; (e) split the abbreviation-lift assertion into a unit test (monkeypatched, checks weight application) and one integration test gated on model presence.

Sequence the build: land Stage 2c + Signal C end-to-end first (the 80→95 bridge). Then reconcile Set B in a second commit on the same branch. The brief bundles the bridge signal with the auditability retrofit; sequencing them protects against another deadlock without changing scope.