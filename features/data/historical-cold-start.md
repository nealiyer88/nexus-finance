# Feature Brief: Historical Data Pipeline + Cold Start Seeding

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

On Day 1, the graph is empty. Without historical seeding, the first sync cycle produces zero auto-approvals — every entity goes to human review. The spec requires "Day 2 is your cross-category entity registry" — which means historical data (2–5 years) must be ingested, clustered, and surfaced for guided onboarding within 48 hours. The cold start experience determines whether the customer sees value or churns.

---

## Forward Dependencies (contracts, not implementations)

These do **not** exist in the tree today. This brief consumes them by contract only; do not
guess at their internals, and do not treat any of them as present when writing code.

**From feature 12 (matcher orchestrator):**
- `core/matching/engine.py` — a single-entity entry point that runs Stage 1 → 2 → 3 → 4 →
  (Stage 5 when the band says so) → Stage 6 and returns a per-entity result carrying at
  minimum: resolved canonical id (or a "new entity" marker), final confidence, the match
  type, and the `Disposition` that produced it. Feature 12 names this result type; it is
  NOT defined anywhere in the tree yet, so refer to it by role, not by name.
- `core/ingestion/pipeline.py` — a batch driver that pulls from a `ConnectorInterface`,
  normalizes, matches, and returns a per-disposition summary.
- Feature 12's brief also references a registry abstraction as the second `match()`
  argument. No such type exists today; Stages 1–3 currently take a live
  `sqlite3.Connection` (see `core/graph/entity_store.py`, `core/matching/blocking.py`,
  `core/matching/scoring.py`, `core/matching/disposition.py`). **Feature 13 must consume
  whichever of the two feature 12 actually ships** — connection or registry — and must not
  hard-code the other. Confirm against feature 12 at build time.

**From feature 11 (approval queue):**
- A pending-review record that the queue reads. Feature 11's brief points at the
  `approval_decisions` table, which per the rules file exists in the **Postgres** schema
  only and is never created or queried at runtime; `db/schema_sqlite.sql` has no approvals
  table. Feature 11 must therefore define where a pending item lives. Feature 13 needs from
  it exactly one thing: **a way to hand a cluster to the queue as a pending item that
  carries its member pairs, aggregate confidence, and recommended action.** Everything else
  about the queue is feature 11's business.
- Feature 11 already surfaces `Disposition.abbreviation_rescue`; cold-start clusters must
  preserve that flag on their member pairs rather than re-deriving it.

**Already shipped and safe to depend on:** `core/ingestion/normalizer.py`
(`normalize_entity(raw) -> NormalizedEntity`), `core/matching/blocking.py`
(`generate_candidates`), `core/matching/scoring.py` (`score_pair`, `score_candidate_set`),
`core/matching/disposition.py` (`apply_thresholds`), `core/matching/llm_fallback.py`
(`llm_assess`), `core/matching/redaction.py`, `core/graph/resolution.py`
(`resolve_match` / `create_new_entity` / `reject_match`), `core/graph/entity_store.py`,
`connectors/quickbooks.py`, `connectors/ruddr.py`.

---

## Scope

### In Scope

- Create `core/ingestion/historical.py`:
  - `seed_from_history(qb_connector, ruddr_connector, tenant_id)` — pulls the full entity
    set from both connectors. Note the shipped `ConnectorInterface` contract: entities come
    from `read_entities(entity_type, filters)` (no date range), and historical *transactions*
    come from `read_transactions(date_range)` taking a `connectors.base.DateRange`. There is
    no "pull everything" method — the seeder iterates entity types itself.
  - Feeds each raw record through `normalize_entity()` (Stage 0) before matching.
  - Drives the feature 12 orchestrator over the normalized set, then groups the resulting
    dispositions into cross-category clusters for guided review.
  - Cold start surfaces more for human review than steady state. See the open threshold
    question below — the mechanism for "relaxed" is a decision, not an implementation detail.

- Create `core/ingestion/clustering.py`:
  - `cluster_entities(...)` — takes the scored/dispositioned output of the seeding run and
    groups it into candidate clusters ranked by aggregate confidence. The concrete input
    element is `core/matching/types.ScoredMatch` (or the `Disposition` that wraps a ranked
    tuple of them); do not invent a new pair type.
  - Each cluster: proposed canonical name, aliases per source category (`accounting` |
    `psa`, per `NormalizedEntity.category`), aggregate confidence, recommended action.
  - Cluster size is data-dependent, not fixed. The guided-onboarding session length is a
    product target, not an assertion.

- LLM-assisted clustering for non-obvious cross-category matches:
  - Reuses the shipped Stage 5 path (`llm_assess`), which already builds the redacted prompt
    via `redact_org` / `redact_person` and runs `leak_check` inbound and outbound. Cold start
    must NOT re-implement redaction.
  - `llm_assess` refuses any disposition whose `action` is not `LLM_FALLBACK`, and
    `apply_thresholds` returns `NO_MATCH` with `top_match=None` below
    `LLM_FALLBACK_THRESHOLD`. Widening the cold-start band therefore has no seam today —
    see Open Decisions.
  - `llm_fallback.MAX_LLM_CALLS_PER_RUN` is a hard cap that raises `LLMBudgetExceededError`,
    not a soft budget. A cold-start run that exceeds it fails rather than degrades. See
    Open Decisions.
  - Results populate the onboarding queue, never auto-approve. This is already enforced:
    `llm_assess` always returns `action="QUEUE_FOR_REVIEW"`.
  - Abbreviation-rescue pairs (PSA↔Accounting, heuristic fired, score in the LLM band) skip
    the LLM by design and route straight to human review. Cold start must honor this.

- Guided onboarding output: structured list of clusters for the approval queue, prioritized
  by confidence and business impact (higher transaction volume = higher priority; volume is
  readable from the shipped `transactions` table).

- **Test suite:** `tests/test_historical.py`
  - Seed an empty graph with the fixture set (`tests/fixtures/qb_entities.json`,
    `tests/fixtures/ruddr_entities.json`) via the historical pipeline. Derive the fixture
    entity count at test time by loading the fixtures — never write it into the test.
  - Assert: cluster count is > 0 and ≤ the number of seeded entities, and every seeded
    entity appears in at most one cluster.
  - Assert: every pair whose score ≥ `disposition.AUTO_APPROVE_THRESHOLD` lands in the same
    cluster as its counterpart (import the constant; do not restate its value).
  - Assert: LLM-assisted clustering is invoked for pairs in the fallback band, using an
    injected fake `LLMClient` (`llm_assess` accepts a `client` argument) — no live API call
    in tests.
  - Assert: no cluster produced by the LLM path carries an auto-approve recommendation.

### Out of Scope

- Retroactive transaction classification — Phase 3 of historical pipeline (spec Section 12)
- Onboarding wizard UI — separate feature
- Incremental sync (delta detection) — V1 uses full re-pull
- Cross-category schema drift detection — separate feature
- Changing any shipped threshold constant. If cold start needs a different band, that is a
  scope change to `core/matching/disposition.py`, not a side effect of this feature.

---

## Open Decisions (require a human, not a rewrite)

1. **Relaxed cold-start band.** The shipped Stage 4 has one band set, module-level and not
   parameterized: below `LLM_FALLBACK_THRESHOLD` there is no `top_match` at all. Widening
   the cold-start LLM band below it requires either (a) parameterizing `apply_thresholds`,
   (b) cold start calling `score_candidate_set` and doing its own banding, bypassing Stage 4
   — which forfeits cluster-conflict detection and abbreviation rescue, or (c) dropping the
   wider band and using the steady-state one. Pick one before build.
2. **LLM call cap.** `MAX_LLM_CALLS_PER_RUN` is enforced by exception. A full-history cold
   start over a real tenant will plausibly exceed it. Decide: raise the cap for cold start,
   make cold start batch across multiple runs with `reset_call_budget()`, or degrade
   gracefully to human review on cap. "Budget for N calls" is not a mechanism.
3. **Where a pending cluster is stored.** No runtime approvals table exists. Feature 11 must
   define it; if feature 11 ships without one, feature 13 has nowhere to write and is blocked.

---

## Success Criteria

- [ ] `core/ingestion/historical.py` exists with `seed_from_history()`
- [ ] `core/ingestion/clustering.py` exists with `cluster_entities()`
- [ ] Historical seeding processes every entity loaded from the fixture files (count derived
      at test time) with no unhandled exceptions
- [ ] Cluster count > 0, ≤ seeded entity count; no entity appears in two clusters
- [ ] Each cluster carries: proposed canonical name, aliases per source category, aggregate
      confidence, recommended action
- [ ] LLM-assisted clustering goes through `llm_assess` / `redaction.py`; no second
      redaction implementation exists in the new files
- [ ] No LLM-derived cluster carries an auto-approve recommendation
- [ ] Second pass after simulated approval of the seeded clusters produces strictly more
      auto-approvals than the first pass on the empty graph (monotonic improvement, measured
      in-test against the first pass — not against a fixed percentage)
- [ ] `.venv/bin/python -m pytest tests/test_historical.py -x --tb=short` passes AND reports
      a collected test count greater than zero (a bare exit code is not acceptable evidence)

---

## Dependencies

- [ ] Matcher orchestrator (feature 12) — NOT BUILT. Forward dependency; see contract above.
- [ ] Approval queue (feature 11) — NOT BUILT. Forward dependency; see contract above.
- [x] Both connectors (features 5, 6) — shipped, fixture-backed
- [x] Normalizer (feature 3), scoring (8/8a/8b), disposition + LLM fallback (9),
      resolution (10) — shipped

---

## Estimated Complexity

**Rating:** M

**Rationale:** Clustering logic is the new work — the rest reuses the shipped pipeline. The
real cost is not the clustering algorithm; it is the two unresolved seams above (band
widening and the LLM call cap), both of which touch shipped modules this feature is not
supposed to modify. Resolve those before estimating build time.

---

## PROJECT CONTEXT

### Cold Start Timeline (from spec Section 12)

- Day 1: Connection — QB + RUDDR OAuth
- Day 2: Cross-category entity clustering — RapidFuzz clusters obvious matches, Claude API
  catches non-obvious cross-category matches
- Day 2–3: Guided onboarding — customer reviews the proposed clusters in a single session
- Week 1: First automated cycle — with graph seeded, auto-match rate improves over the
  cold first run

### V1 Hard Constraints

- LLM calls use Claude API with mandatory redaction
- LLM results never auto-approve during cold start
- Historical data populates graph layer only — never modifies source system history
- Sequential processing; no async/parallel

### Relevant Spec Sections

- Section 12: Historical Data Pipeline & Cold Start (all 4 phases)
- Section 5: Cold Start Solution
