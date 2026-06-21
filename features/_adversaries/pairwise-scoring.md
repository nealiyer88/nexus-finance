# Hardened Design: Pairwise Scoring Engine (Pipeline Stage 3)

**Source brief:** `features/pipeline/pairwise-scoring.md`
**Adversary phase:** Rocket loop, Phase 1
**Date:** 2026-06-14

This document records every genuine disagreement between the three
adversary agents (design / skeptic / engineer), the side that won, and
the resulting scope adjustments. The brief is amended in-place by the
decisions below — the build prompt (Phase 2) and the implementation
(Phase 3) follow this document, not the original brief.

---

## 1. Disagreements and decisions

### 1.1 Person-name inversion direct-score (0.95)

- **Design**: keep the 0.95 direct-score for the residual case where one
  side bypassed Stage 0 normalization.
- **Skeptic**: the 0.95 override is dead code or actively harmful —
  Stage 0 already inverts "Last, First" → "First Last" before Stage 3
  sees the strings.
- **Engineer**: drop the Stage 3 inversion override entirely — Stage 0
  owns it. Keep one test proving inverted pairs score ≥0.95 via the
  normal string-metric path.

**Decision:** **Engineer wins.** Drop `name_inversion_score` from
`WeightConfig`. Stage 0 (`core/ingestion/normalizer.py` lines 184–189)
is the single owner of person-name inversion. Both sides arrive at Stage
3 normalized to `"michael chen"`, which scores 1.0 on `token_set_ratio`
natively. A 0.95 ceiling would actively destroy that 1.0 signal.

**Why the losing sides are wrong on this point:**
- Design's "residual case where one side bypassed Stage 0" cannot
  happen in V1 — every input to Stage 3 comes from the matcher
  orchestrator, which runs Stage 0 first. There is no API path that
  feeds Stage 3 a raw_record.

### 1.2 Shared transaction context signal

- **Design**: defend the signal as cross-category corroboration.
- **Skeptic**: blocks — no `transactions` table exists in
  `db/schema_sqlite.sql`.
- **Engineer**: cut entirely from V1 — requires Stage 6 ingestion that
  doesn't exist.

**Decision:** **Engineer wins.** "Shared transaction context" signal is
**removed from V1 scope.** Resurrect when transaction storage ships
(Stage 6 / feature 10). The brief's bonus band (+0.03–0.05) is deleted;
no code references this signal.

**Why the losing side is wrong on this point:**
- Design's "graph corroboration proves the V1 thesis" still holds via
  the shared-person-entities signal (1.3 below). The transaction
  variant is a Stage 6 dependency, not a Stage 3 requirement.

### 1.3 Graph signal set B — shared person entities & neighborhood overlap

- **Design**: implement fully.
- **Skeptic**: queries return 0 evidence on a fresh DB because
  `entity_edges` is empty until Stage 6 writes.
- **Engineer**: implement the SQL queries against `entity_edges` (which
  exists, just empty); returns 0 evidence on fresh DB; activates when
  edges populate.

**Decision:** **Engineer wins.** Implement `shared_person_entities` and
`graph_neighborhood_overlap` as real SQL functions against the existing
`entity_edges` table (`db/schema_sqlite.sql` lines 33–46). Both return 0
on the empty edge set V1 fixtures ship with. Tests seed edges manually
to prove the SQL fires and bonuses apply correctly.

**Why the losing sides are wrong on this point:**
- Skeptic is right that V1 ground truth has no seeded edges. Skeptic is
  wrong that this makes the signal worthless — it is the activation
  surface for Stage 6, and shipping it now lets Stage 6 land without
  rewriting Stage 3.

### 1.4 token_sort_ratio retention

- **Design**: keep — distinct failure mode from `token_set_ratio`.
- **Engineer**: drop — `token_set_ratio` covers it post-normalizer; the
  signal is 95% redundant.

**Decision:** **Design wins.** Keep `token_sort_ratio`. Reasons:
1. The brief explicitly lists all four RapidFuzz signals; build prompts
   honor the brief's enumerated signal set unless a *safety* issue
   requires cutting.
2. Success criterion (brief line 80) requires "Signal breakdown
   returned for every scored pair (all individual signal values
   visible)" — dropping a signal trades brief fidelity for ~5µs/pair
   savings.
3. The four RapidFuzz signals are individually cheap; cost of keeping
   token_sort is irrelevant at V1 scale.

**Why the losing side is wrong on this point:**
- Engineer's perf argument is sound at <500 entities but the savings
  are not worth diverging from the brief. The brief is the source of
  truth on signal enumeration.

### 1.5 Category-pair dispatch table size

- **Design**: 4 populated entries (acc↔acc, acc↔psa, psa↔acc, psa↔psa).
- **Engineer**: 1 populated entry (the cross-category cell) + default.

**Decision:** **Engineer wins.** Populate exactly two cells (which
share one config object): `("accounting","psa")` and `("psa","accounting")`
both point at the same `PSA_ACCOUNTING_WEIGHTS`. All other category pairs
fall through to `DEFAULT_WEIGHTS` via the `get_weights()` default. No
dead config branches.

**Why the losing side is wrong on this point:**
- Design's 4-entry table includes intra-system pairs that Stage 2d
  filters out (`core/matching/blocking.py` lines 70–75). Those entries
  are unreachable at V1.

### 1.6 WeightConfig structure (weights vs override scores)

- **Design**: single dataclass with both weights and override scores.
- **Engineer**: split — weights sum to 1.0, overrides separate.

**Decision:** **Engineer wins.** `WeightConfig` carries only signals
that participate in the weighted-sum formula:
- 5 multiplicative weights on the string metrics
- 1 additive `alias_boost` (applied post-sum)
- 1 additive `abbreviation_bonus` (applied post-sum, gated by heuristic)
- 1 `profile_id: str` for debuggability

`name_inversion_score` is **deleted** (see 1.1). No separate
`OVERRIDE_SCORES` constant is needed in V1 because the only override
the brief asked for is name-inversion, which is now Stage 0's
responsibility.

### 1.7 profile_id on ScoredMatch + SignalBreakdown as dataclass

- **Design**: add `weight_profile_id` to `ScoredMatch`; promote
  `signal_breakdown` to a frozen dataclass.
- **Skeptic / Engineer**: no objection.

**Decision:** **Design wins, no opposition.** Add `weight_profile_id`
to `ScoredMatch`. `SignalBreakdown` and `GraphEvidence` are frozen
dataclasses in `core/matching/types.py`. Matches the existing convention
(`CandidateSet`, `DeterministicMatch` are frozen dataclasses, not dicts).

### 1.8 Score clamping

- **All three** flag the brief as silent on clamping.

**Decision:** Score is **clamped to [0.0, 1.0]** in `score_pair`'s
return. The exact formula (in the docstring and asserted by tests) is:

```
weighted_sum = (
    weights.token_sort_ratio  * signals.token_sort_ratio  / 100 +
    weights.token_set_ratio   * signals.token_set_ratio   / 100 +
    weights.partial_ratio     * signals.partial_ratio     / 100 +
    weights.jaro_winkler      * signals.jaro_winkler      / 100 +
    weights.ngram_jaccard     * signals.ngram_jaccard
)
score_raw = (
    weighted_sum
    + (weights.alias_boost        if alias_boost_fires        else 0.0)
    + (weights.abbreviation_bonus if abbreviation_bonus_fires else 0.0)
    + graph_evidence.shared_person_bonus
    + graph_evidence.neighborhood_overlap_bonus
)
score = min(1.0, max(0.0, score_raw))
```

(RapidFuzz returns 0–100; n-gram Jaccard returns 0–1. The division by
100 normalizes the RapidFuzz outputs into the same [0,1] scale before
multiplying by weights.)

### 1.9 Graph neighborhood overlap cap

- **Skeptic** flags missing cap value in brief.

**Decision:** Per-shared-node bonus = **0.025**. Cap on total
neighborhood overlap bonus = **0.10** (so ≥4 shared nodes saturates).
The shared-person bonus is separately capped at **0.10** (per shared
person: +0.05; max two persons count). Total graph evidence is therefore
bounded at 0.20.

### 1.10 Email +0.10 soft signal

- **Design**: defend the signal.
- **Skeptic**: structurally unreachable — Stage 1 already resolves
  email matches at confidence 0.99.

**Decision:** **Skeptic wins.** Drop the +0.10 email soft signal from
Stage 3. By construction, Stage 3 only runs on pairs where Stage 1
returned `None`. Two person entities with the same email would have
been resolved at Stage 1b. Coding an unreachable bonus inflates test
fictions without exercising any production input.

**Why the losing side is wrong on this point:**
- Design's defense relies on the signal firing for "email present on
  one side but not on the canonical store" — but that case is
  Stage 1's empty-result path, which falls through to Stage 2 with
  email never compared.

### 1.11 Alias boost spec (which metric, threshold, stacking)

- **Skeptic** flags spec gap.

**Decision:** Alias boost specification, lifted from skeptic's
question:
- Compute `max(token_set_ratio, jaro_winkler)` of incoming
  `entity.normalized_name` against each of the candidate's aliases
  (excluding the candidate's `canonical_name`, which is already in the
  weighted sum).
- If the max is >85.0 (RapidFuzz scale), the boost fires.
- Boost is **single-application**: max one boost per pair, regardless
  of how many aliases qualify.

### 1.12 PSA shortcode / abbreviation heuristic algorithm

- **Skeptic** flags spec gap (CFSB vs CEN is not initials).

**Decision:** Algorithm:
- Fires only when the category pair is `(accounting, psa)` or
  `(psa, accounting)`.
- Identify the "shortcode side" = whichever of the pair has a
  `normalized_name` of length ≤4 characters AND that side's source is
  `ruddr` (i.e. PSA). If both sides are >4 chars, the heuristic does
  not fire.
- Identify the "long side" = the other side's `normalized_name`.
- The heuristic fires if EITHER:
  - the shortcode is a prefix of any whitespace-token of the long side
    (e.g. `"cen"` prefixes `"cenlar"`), OR
  - the shortcode equals the consonant-skeleton initials of the long
    side (first letter of each token, lowercased) — e.g. `"mcg"` for
    `"meridian consulting group"`.
- On fire → add `weights.abbreviation_bonus` (default 0.20).

CAN-019 ("Stratos Cloud" / "CloudNine Infrastructure") is safe because
neither normalized side is ≤4 chars.

### 1.13 Tenant scoping

- **Skeptic** flags missing from brief.

**Decision:** All Stage 3 entity_store reads (alias fetch, edge fetch)
take `tenant_id: Optional[str] = None`, following the existing
`core/graph/entity_store.py` convention. The new functions in
`entity_store.py` for Stage 3:
- `get_aliases(conn, canonical_id, tenant_id=None) -> list[str]`
- `get_canonical_name_and_category(conn, canonical_id, tenant_id=None) -> Optional[tuple[str, str, str]]` — returns `(canonical_name, entity_category, entity_type)`; needed because `CandidateEntity` carries only `canonical_id`
- `count_shared_person_neighbors(conn, source_canonical_id, candidate_canonical_id, tenant_id=None) -> int`
- `count_shared_graph_neighbors(conn, source_canonical_id, candidate_canonical_id, tenant_id=None) -> int`

`source_canonical_id` for Stage 3 is `None` for unresolved entities (the
query side), so the shared-neighbors functions accept `None` and return
0 in that case.

### 1.14 Tie-breaking

- **Skeptic** flags missing.

**Decision:** The orchestrator wrapper (not implemented in this
feature) will iterate Stage 2's `CandidateSet` and call `score_pair`
per candidate. The brief's deliverable is `score_pair`, not
`score_candidate_set`, so tie-breaking lives in the (later)
orchestrator. **In scope for this feature only:** a thin convenience
function `score_candidate_set(entity, candidate_set, conn, tenant_id=None)
-> tuple[ScoredMatch, ...]` returning matches sorted by `(-score,
canonical_id)` for deterministic ordering. This unblocks Stage 4's
ordering needs without adding scope.

### 1.15 "44 canonical match pairs" interpretation

- **Skeptic** flags off-by-one risk.

**Decision:** Interpretation: 44 canonical entities, each with a
QB↔RUDDR pair. The fixture stats block (`tests/fixtures/canonical_ground_truth.json`)
confirms `total_canonical: 44` with `org_pairs: 19, person_pairs: 25`.
44 pairs total. The test iterates over `canonical_entities` and
constructs one pair per entity (the QB side as `entity`, the RUDDR
side as `candidate`).

### 1.16 Non-match test data synthesis

- **Skeptic** flags missing.

**Decision:** Test fixture extension: synthesize **10 non-match pairs**
by cross-matching QB entity X with RUDDR entity Y where X≠Y AND the
two entities share no pattern token (i.e., different ground-truth
canonicals). Random seed pinned at 42 for reproducibility. Assert all
10 score <0.50.

### 1.17 score_pair signature

- **Design** proposes; **Engineer** agrees.

**Decision:** Adopt Design's signature, with the engineer's renaming
hygiene:

```python
def score_pair(
    entity: NormalizedEntity,
    candidate_id: str,
    candidate_name: str,
    candidate_aliases: tuple[str, ...],
    candidate_category: str,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> ScoredMatch: ...
```

(Renamed `candidate: CandidateEntity` to `candidate_id: str` so the
function signature does not couple to `CandidateEntity`'s shape; the
convenience wrapper `score_candidate_set` unpacks the
`CandidateEntity` and passes the strings down.)

### 1.18 Empty-string guard

- **Engineer** proposes.

**Decision:** `score_pair` returns a `ScoredMatch` with `score=0.0` and
a zero-valued `SignalBreakdown` if either `entity.normalized_name` or
`candidate_name` is empty / whitespace-only. No exception.

### 1.19 Weights-sum hygiene test

- **Skeptic** flags missing.

**Decision:** Add `test_weights_sum_to_one` asserting the multiplicative
weights + `alias_boost` (which is the "tier-1 evidence" portion of the
score) sum to 1.0 exactly. Concretely:
`token_sort + token_set + partial + jaro_winkler + ngram_jaccard +
alias_boost == 1.0` to within `1e-9`. The two bonuses
(`abbreviation_bonus`, graph evidence) are additive and excluded from
this sum (they are upside, not budget).

---

## 2. Scope adjustments (final)

### Removed from brief

- Person-name inversion direct-score (Stage 0 owns it).
- Shared transaction context signal (no transactions table in V1).
- Email +0.10 soft signal (Stage 1 already resolves email matches).

### Added to brief

- Score clamping to [0.0, 1.0] — explicit, documented, tested.
- Numeric caps on graph evidence: per-shared-person 0.05 (max 2 → 0.10);
  per-shared-neighbor 0.025 (max 4 → 0.10); total graph bonus ≤ 0.20.
- `profile_id: str` on `WeightConfig`; `weight_profile_id: str` on
  `ScoredMatch`.
- `SignalBreakdown` and `GraphEvidence` as frozen dataclasses in
  `core/matching/types.py`.
- 4 new read-only functions in `core/graph/entity_store.py` for
  alias / canonical-name / edge lookups, tenant-scoped.
- `score_candidate_set(entity, candidate_set, conn, tenant_id=None) ->
  tuple[ScoredMatch, ...]` convenience wrapper with deterministic
  ordering.
- Empty-string guard.
- Synthesized 10 non-match pairs (random seed 42).
- Hygiene test: `test_no_xgboost_no_fasttext_no_llm_in_scoring` —
  greps `core/matching/scoring.py` for forbidden imports.
- Weights-sum hygiene test.

### Kept as-written

- All four RapidFuzz signals (`token_sort_ratio`, `token_set_ratio`,
  `partial_ratio`, `jaro_winkler`).
- N-gram Jaccard (trigrams, sentinel-padded matching `NgramIndex` in
  `core/matching/indices.py`).
- Category-pair dispatch via `Dict[Tuple[str,str], WeightConfig]`.
- PSA shortcode heuristic (algorithm pinned in 1.12 above).
- Per-signal `WeightConfig` weights at brief's defaults
  (0.25 / 0.25 / 0.15 / 0.10 / 0.10 / 0.15 / 0.20).
- All success criteria from brief (lines 78–87) except #6 (the email
  test, removed; non-match "score <0.50" lives in synthesized fixture).

---

## 3. Implementation constraints (from engineer)

- **`scoring.py` may import `rapidfuzz`.** The hygiene test at
  `tests/test_blocking.py` line 371 enumerates the no-rapidfuzz
  modules; `scoring.py` is NOT on that list.
- **No new external dependencies.** RapidFuzz already pinned at
  `rapidfuzz==3.9.0` in `requirements.txt`.
- **No XGBoost / fastText / LLM imports** in `scoring.py` (enforced by
  new hygiene test).
- **Tenant scoping** on every `entity_store` read function added in
  this feature.
- **Stage 0 lowercases and strips diacritics** — Stage 3 does NOT
  re-normalize; it trusts `NormalizedEntity.normalized_name`.
- **Brief's "44 pairs" is 44 canonical entities × QB↔RUDDR**, not 44 ×
  2. Test iterates ground-truth `canonical_entities`.

---

## 4. Real risks the skeptic raised that survive

- **Graph signal set B silently returns 0 in V1.** This is acceptable
  (the signal becomes load-bearing in Stage 6) but must be visible in
  the breakdown so debug output shows `shared_person_count=0, bonus=0.0`
  rather than the signal being absent.
- **PSA abbreviation heuristic on Stratos Cloud / CloudNine.** Pinned
  algorithm (1.12) explicitly guards: neither side is ≤4 chars →
  heuristic does not fire. Test the negative case.
- **Alias boost double-counting.** Mitigated by:
  (a) excluding the candidate's `canonical_name` from the alias scan
      (it's already in the weighted sum), and
  (b) capping alias boost to a single application per pair.
- **Tenant leakage in entity_store SQL.** Mitigated by following the
  existing `Optional[str]` tenant_id pattern from Stages 1–2.

---

## 5. Risks the skeptic raised that are phantom (dismissed)

- "Rebrand pair scores ~0 trivially" — that IS the correct V1 outcome
  (NO_MATCH band, surfaces to alias review). The scorer's job is not
  to bridge rebrands. Acceptance test #8 stays.
- "Performance on N×M SQLite calls" — at 500 canonicals × 50 cap × 4
  queries/pair = 100K SQLite calls per pipeline run, all indexed, all
  read-only. Sub-3-second wall time at V1 scale. Re-evaluate at >50K
  nodes per rules §11.
- "Tie-breaking lives in Stage 3" — Stage 4 owns disposition; Stage 3
  only needs to return a deterministic list. Addressed via
  `score_candidate_set` wrapper (1.14).

---

## 6. Test cases added beyond the brief's success criteria

Mandatory test additions (will appear in `tests/test_scoring.py`):

1. `test_weights_sum_to_one` — weights + alias_boost == 1.0
2. `test_score_clamped_to_unit_interval` — synthesize a pair forcing
   raw sum > 1.0; assert returned score == 1.0
3. `test_empty_normalized_name_returns_zero_score` — empty entity name
4. `test_empty_candidate_name_returns_zero_score` — empty candidate
5. `test_psa_abbreviation_heuristic_does_not_fire_on_long_names` —
   Stratos Cloud / CloudNine assertion
6. `test_psa_abbreviation_heuristic_fires_on_prefix_match` — CEN /
   Cenlar
7. `test_psa_abbreviation_heuristic_fires_on_initialism_match` —
   MCG / Meridian Consulting Group
8. `test_alias_boost_single_application` — multiple aliases ≥0.85 →
   one boost only
9. `test_shared_person_bonus_capped` — seed 3+ shared persons; assert
   bonus capped at 0.10
10. `test_neighborhood_overlap_bonus_capped` — seed 5+ shared
    neighbors; assert bonus capped at 0.10
11. `test_graph_evidence_zero_on_empty_edge_table` — fresh DB; assert
    `shared_person_count=0` and bonuses=0
12. `test_dispatch_returns_default_for_unconfigured_pair` —
    `("crm", "payments")` → `DEFAULT_WEIGHTS`
13. `test_dispatch_returns_psa_accounting_for_cross_pair` — both
    direction tuples
14. `test_score_candidate_set_orders_by_descending_score` — three
    candidates with distinct scores
15. `test_score_candidate_set_breaks_ties_by_canonical_id` — two
    candidates with identical scores
16. `test_no_xgboost_no_fasttext_no_llm_in_scoring` — hygiene grep
17. `test_signal_breakdown_carries_every_weighted_signal` — all 5
    weighted signals present even when zero
18. `test_44_ground_truth_pairs_score_above_no_match` — iterate
    `canonical_ground_truth.json`; assert all 44 pairs score > 0.50
19. `test_10_synthesized_non_match_pairs_score_below_no_match` —
    seed 42; assert all 10 score < 0.50
20. `test_person_inversion_pair_scores_at_least_0_95_via_string_metrics`
    — Chen, Michael / Michael Chen; both normalized to "michael chen";
    assert score ≥ 0.95 via the natural weighted sum (no inversion
    override)

These supplement the brief's listed acceptance criteria; they do not
replace them.

---

## 7. Final file list (build deliverable)

- `core/matching/scoring.py` — new — `score_pair`, `score_candidate_set`,
  signal functions, abbreviation heuristic.
- `core/matching/weights.py` — new — `WeightConfig`, `DEFAULT_WEIGHTS`,
  `PSA_ACCOUNTING_WEIGHTS`, `get_weights`, dispatch table.
- `core/matching/types.py` — modified — append `SignalBreakdown`,
  `GraphEvidence`, `ScoredMatch` frozen dataclasses.
- `core/graph/entity_store.py` — modified — append `get_aliases`,
  `get_canonical_name_and_category`, `count_shared_person_neighbors`,
  `count_shared_graph_neighbors`.
- `tests/test_scoring.py` — new — covers all brief success criteria
  and the 20 hardened-design test additions.
- `requirements.txt` — unchanged.

No other files touched.
