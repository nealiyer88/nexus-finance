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

## Scope

### In Scope

- Create `core/ingestion/historical.py`:
  - `seed_from_history(qb_connector, ruddr_connector, tenant_id)` — pulls full historical data from both connectors
  - Runs all historical entities through normalizer
  - Runs matcher pipeline with relaxed thresholds (surface more candidates for human review during onboarding)
  - Groups proposed matches into cross-category clusters for guided review

- Create `core/ingestion/clustering.py`:
  - `cluster_entities(scored_pairs)` → groups of candidate matches ranked by aggregate confidence
  - Produces 20–50 clusters for the guided onboarding session
  - Each cluster: proposed canonical name, aliases from each category, aggregate confidence, recommended action

- LLM-assisted clustering for non-obvious cross-category matches:
  - Claude API call for entity pairs scoring 0.40–0.70 during cold start (wider band than normal operation)
  - Mandatory redaction (same protocol as Stage 5)
  - Results populate the onboarding queue, never auto-approve

- Guided onboarding output: structured list of clusters for the approval queue, prioritized by confidence and business impact (higher transaction volume = higher priority)

- **Test suite:** `tests/test_historical.py`
  - Seed empty graph with all fixture data via historical pipeline
  - Assert: produces ≥30 proposed clusters from 91 entities
  - Assert: high-confidence pairs (>0.85) grouped correctly
  - Assert: LLM-assisted clustering invoked for ambiguous pairs

### Out of Scope

- Retroactive transaction classification — Phase 3 of historical pipeline (spec Section 12)
- Onboarding wizard UI — separate feature
- Incremental sync (delta detection) — V1 uses full re-pull
- Cross-category schema drift detection (>20% change) — separate feature

---

## Success Criteria

- [ ] `core/ingestion/historical.py` exists with `seed_from_history()` function
- [ ] `core/ingestion/clustering.py` exists with `cluster_entities()` function
- [ ] Historical seeding processes 91 fixture entities and produces ≥30 clusters
- [ ] Each cluster contains: proposed canonical name, aliases per category, confidence, recommended action
- [ ] LLM-assisted clustering uses mandatory redaction protocol
- [ ] LLM-assisted results never auto-approve
- [ ] After guided onboarding (simulated approvals of top clusters), re-run pipeline achieves ≥70% auto-match rate
- [ ] `pytest tests/test_historical.py` passes

---

## Dependencies

- [ ] Matcher orchestrator (feature 12) — pipeline must be end-to-end functional
- [ ] Both connectors (features 5, 6) — need historical data from both categories
- [ ] Approval queue (feature 11) — clusters feed into the queue for guided review

---

## Estimated Complexity

**Rating:** M

**Rationale:** Clustering logic is the new work — the rest reuses existing pipeline. The LLM-assisted cold start clustering operates at a wider confidence band (0.40–0.70) than normal operation (0.50–0.70), which means more LLM calls during onboarding. Budget for ~50 Claude API calls per cold start.

---

## PROJECT CONTEXT

### Cold Start Timeline (from spec Section 12)

- Day 1: Connection — QB + RUDDR OAuth
- Day 2: Cross-category entity clustering — RapidFuzz clusters obvious matches, Claude API catches non-obvious cross-category matches
- Day 2–3: Guided onboarding — customer reviews 20–50 proposed clusters in 30–60 minutes
- Week 1: First automated cycle — with graph seeded, auto-match rate targets 70–80%

### V1 Hard Constraints

- LLM calls use Claude API with mandatory redaction
- LLM results never auto-approve during cold start
- Historical data populates graph layer only — never modifies source system history

### Relevant Spec Sections

- Section 12: Historical Data Pipeline & Cold Start (all 4 phases)
- Section 5: Cold Start Solution
