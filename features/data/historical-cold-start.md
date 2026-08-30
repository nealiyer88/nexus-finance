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

## Upstream contracts (SHIPPED)

Features 11 and 12 are shipped and present in the tree. Feature 13 builds **on top of**
them and must not reimplement the matching or ingestion pipeline inside the new modules.

**From feature 12 (matcher orchestrator):**
- `core/matching/engine.py` — `engine.match(incoming, ctx) -> MatchResult`, the single-entity
  entry point that runs Stage 1 → 2 → 3 → 4 → (Stage 5 when the band says so) → Stage 6 and
  returns a per-entity result carrying: resolved canonical id (or a "new entity" marker),
  final confidence, the match type, and the `Disposition` that produced it.
- `core/ingestion/pipeline.py` — `run_ingestion(connector, conn, tenant_id, llm_client=None)
  -> IngestionSummary`, the batch driver that pulls from a `ConnectorInterface`, matches, and
  returns a per-disposition summary.
- `MatchContext` is the second `match()` argument and settles the registry-vs-connection
  question in favor of the connection: it carries the live `sqlite3.Connection` alongside the
  token, n-gram and embedding indexes, `tenant_id`, and the LLM client. No registry type
  exists; do not hard-code one.

**From feature 11 (approval queue):**
- A pending-review record that the queue reads. Feature 11's brief points at the
  `approval_decisions` table, which per the rules file exists in the **Postgres** schema
  only and is never created or queried at runtime; `db/schema_sqlite.sql` has no approvals
  table. Feature 11 shipped the runtime home instead: the pending-decision store, its SQLite
  migration, and the approvals router. Feature 13 needs from it exactly one thing: **a way to
  hand cluster members to the queue as pending items carrying their pairs, confidence, and
  recommended action.** Everything else about the queue is feature 11's business.
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
  - `seed_from_history(qb_connector, ruddr_connector, conn, tenant_id, llm_client=None)` —
    pulls the full entity set from both connectors. Note the shipped `ConnectorInterface`
    contract: entities come from `read_entities(entity_type, filters)` (no date range), and
    historical *transactions* come from `read_transactions(date_range)` taking a
    `connectors.base.DateRange`. There is no "pull everything" method — the seeder iterates
    entity types itself.
  - It calls `run_ingestion` once per connector against the same `conn` — one connector per
    call — pulling entity types via the pipeline's existing per-category entity-type mapping,
    and must not request both `"customer"` and `"client"` from a single connector.
  - The connectors already return normalized entities, so there is no Stage 0
    `normalize_entity()` call in this feature.
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
    not a soft budget. See Open Decisions.
  - Results populate the onboarding queue, never auto-approve. This is already enforced:
    `llm_assess` always returns `action="QUEUE_FOR_REVIEW"`.
  - Abbreviation-rescue pairs (PSA↔Accounting, heuristic fired, score in the LLM band) skip
    the LLM by design and route straight to human review. Cold start must honor this.

- Guided onboarding output: structured list of clusters for the approval queue, ranked by
  aggregate confidence and then cluster size.

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
- Transaction-volume prioritization — deferred until a feature owns transaction ingestion,
  because the fixture connectors return no transactions today.
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
2. **LLM call cap — DECIDED: degrade, do not fail.** The shipped orchestrator already catches
   the budget-exceeded failure inside `match()` and rewrites the disposition to
   queue-for-review, and the ingestion run resets the call budget once per run. A cold start
   that exhausts the cap therefore sends the remainder to human review rather than raising.
   Cold start must go through `match()` so it inherits this behavior; the cap is not raised
   and no new budget mechanism is built.
3. **Where a pending cluster is stored — DECIDED: no cluster-level storage.** A cluster is an
   in-memory return value from `cluster_entities()` plus one enqueued pending row per member
   entity, written through the shipped pending-decision store. **No cluster-level table and no
   schema change is in scope.** `approval_decisions` remains Postgres-only and is neither
   created nor queried at runtime.

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

- [x] Matcher orchestrator (feature 12) — SHIPPED; see upstream contracts above.
- [x] Approval queue (feature 11) — SHIPPED; see upstream contracts above.
- [x] Both connectors (features 5, 6) — shipped, fixture-backed
- [x] Normalizer (feature 3), scoring (8/8a/8b), disposition + LLM fallback (9),
      resolution (10) — shipped

---

## Estimated Complexity

**Rating:** M

**Rationale:** Clustering logic is the new work — the rest reuses the shipped pipeline. The
real cost is not the clustering algorithm; it is the one unresolved seam above (band
widening), which touches a shipped module this feature is not supposed to modify. Resolve
that before estimating build time.

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
