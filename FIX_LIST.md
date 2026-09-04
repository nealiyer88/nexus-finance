# Nexus Finance — Fix List

Compiled 2026-09-03, after features 1–16 + 8a/8b/10a/10b/10c/12a shipped and were
independently verified by running them, not by reading their tests.

Every item below was observed in the shipped tree or in a live run. Nothing here is
speculative. Items marked **VERIFIED** were reproduced directly this session.

---

## Tier 1 — Blocks knowing whether the product works

### 1. Nothing measures accuracy
**VERIFIED.** No precision, recall, F1, or confusion matrix exists anywhere in the tree.
The closest test asserts that 42 of 44 ground-truth pairs score above the bottom cutoff —
a floor, not a grade. The "76.7% auto-match" figure on the overview page counts decisions
made, not decisions that were correct.

Consequence: every other change to matching is unmeasurable. Raising the weights, adding
data, or training a model cannot be shown to help or hurt.

Fix: a scored evaluation over `tests/fixtures/canonical_ground_truth.json` reporting
precision, recall, F1 and a false-merge count, per category pair, runnable as one command
and asserted in CI against a floor.

### 2. Match reasoning is generated and thrown away
**VERIFIED.** `MatchResult.reasoning_trace` is built in `core/matching/engine.py` and passed
to Stage 6, but its only durable home is `approval_decisions.reasoning_trace`, which is
Postgres-only and written solely inside `if pg_is_available()`. On the default SQLite path
it is discarded. Auto-approved matches — the majority — therefore have no stored account
of why they merged.

Signal numbers survive for queued items only, inside `pending_decisions.disposition_json`.

Fix: persist the trace on the SQLite path for every disposition, including auto-approve
and no-match.

### 3. The match explanation shown to users is a debug dump
**VERIFIED.** `dashboard/pages/approval_queue.py::build_detail_view` renders
`dataclasses.asdict(signal_breakdown)` and `asdict(graph_evidence)` as raw dict text.
It is internal field names on a page, not an explanation a person can act on.

Fix: a readable "matched because…" panel — which signals fired, what each contributed,
what the graph corroboration was, and what would have changed the outcome.

---

## Tier 2 — The moat, in the order it has to be built

### 4. Training capture is LLM-gated, so most decisions leave no trace
**VERIFIED.** `core/matching/training_data.py::store_training_pair` writes to
`llm_training_data` only when the pair went through the Stage 5 LLM fallback — the row
requires an existing `source_call_id`. Auto-approves and no-matches produce nothing.

The v3 spec promised capture of every `(entity_pair, redacted_context, category_pair,
LLM_reasoning, human_decision)` as the corpus for the V2 model. What is captured is a
fraction of that.

Fix: capture every human decision and every automatic disposition, not only the
LLM-assisted ones.

### 5. The training table has zero readers
**VERIFIED.** Grep across `core/`, `api/`, `dashboard/`, `scripts/` returns only writers.
Nothing has ever read `llm_training_data` back. It is a write-only well.

### 6. Hard negatives are not collected
The spec calls for "model thought these were candidates but human chose X over Y" as
hard-negative training data. Rejections are recorded, but not the alternatives that were
ranked and passed over.

### 7. No learned pairwise model (spec: V2+)
The spec's Layer 2 is an XGBoost pairwise classifier with the embedding cosine as a feature
column. Shipped instead: a hand-tuned `Dict[Tuple[str, str], WeightConfig]` in
`core/matching/weights.py`. Every weight was typed by a person.

Note: `scikit-learn` is **not** in `requirements.txt` (verified). Any learned model needs
the dependency added deliberately.

### 8. No fine-tuned embeddings — the thing the spec calls the moat (spec: V2+)
The spec's Layer 3 is contextual embeddings fine-tuned on cross-category transaction
co-occurrence and approval pairs. Not built, never queued. The shipped model is
`models/cc.en.300-compress.bin`, pre-trained by a third party on Common Crawl and frozen.

Rules §11 is explicit that fine-tuning is V2+ and out of scope for V1, so this is deferred,
not broken. But it is the differentiator, and it is not on any plan.

### 9. Thresholds are hardcoded, never tuned from data
`AUTO_APPROVE_THRESHOLD = 0.90`, `SURFACE_THRESHOLD = 0.70`,
`LLM_FALLBACK_THRESHOLD = 0.50` are module constants in `core/matching/disposition.py`.
Nothing derives them from measured outcomes. Depends on item 1.

### 10. The premise that justified the fastText mandate was measured false
**VERIFIED in the build record.** v4 made pre-trained fastText V1-mandatory on the claim
that subword cosine was the only V1 signal that could bridge 80% → 95% on abbreviations.
Measured against the real model, the target pairs scored 0.2526 and 0.3679; token-level
`pacrim`↔`pacific` ≈ 0.0; and on one case the designated negative scored *higher* (0.46)
than the positive. Feature 8a's acceptance criterion was amended mid-build because the
original was mathematically unreachable. A deterministic token-prefix abbreviation
heuristic plus Stage 4 "abbreviation rescue" carries the lift instead.

Fix: decide honestly whether the embedding signal earns its 0.05/0.12 weight, once item 1
makes that answerable.

### 11. The embedding blocking channel can be silently evicted
`EmbeddingIndex.query` returns top_k=50 regardless of cosine quality, and `blocking.py`
truncates embed-only candidates first when the 50-candidate cap is hit. Under a full
candidate set the embedding channel can contribute nothing — while still appearing wired.

---

## Tier 3 — Nothing triggers the system

### 12. No entry point for ingestion
**VERIFIED.** Nothing in `api/`, `dashboard/` or `scripts/` calls `run_ingestion` or
`ingest_transactions`. Both are proven to work when called directly, and are called only
by tests. There is no button, no schedule, no CLI.

### 13. No live credentials, no real data has ever flowed
**VERIFIED.** Both connectors run in fixture mode. `.env` holds only `DATABASE_URL`.
The HTTP paths, OAuth refresh and rate limiting exist and have never executed against a
real QuickBooks or Ruddr tenant.

### 14. Seed order is load-bearing and undocumented
**VERIFIED by reproduction.** Running ground-truth seeding before `seed_from_history`
raises `ValueError: system_reference (quickbooks, QB-001) already bound to CAN-001` and
yields **0** pending decisions. History first, then reference data, then transactions is
the only order that fills both the graph and the approval queue. Nothing documents or
enforces this.

---

## Tier 4 — Test suite credibility

### 15. Tests prove callability, not behaviour
This pattern has now shipped three defective features that passed a fully green suite.
The approval queue shipped returning 500 on every real page load with 577 tests passing.

Specific instances found:
- Feature 14's page tests assert a Flask GET returns 200. Dash routes client-side, so the
  shell returns 200 regardless; no callback fires. The test named "with no database" does
  not test that claim.
- Feature 13's `test_seed_from_history_cluster_fields_present` loops
  `aliases_by_category.items()` with isinstance-only assertions and no non-emptiness
  precondition. In a real run that dict was `{}` for every cluster — the loop body executes
  zero times.
- Feature 15's tenant-predicate test is a substring check for `"tenant_id"`, satisfiable by
  merely selecting the column. The loophole is live in the detail SQL, which the test never
  exercises.
- `test_build_sidebar_is_pure` compares `repr()` of two calls; any function passes.
- No queue-full test for the audit worker.

### 16. Zero integration-marked tests added by four consecutive features
**VERIFIED.** 12a, 13, 14 and 15 each added 0. The integration tier has been flat at 35
executed since feature 16, while the suite grew from 543 to 639.

### 17. Features 1–9 shipped under a QA gate that always reported pass
Never audited since. Unknown what it missed.

### 18. Six items in `GATE_DEBT.md`
**VERIFIED** (6 entries). Unreviewed.

---

## Tier 5 — Product surface

### 19. The dashboard is unstyled
Default Dash components, no design system, no spacing or type scale. Functional and hard
to look at.

### 20. Audit log page has zero callbacks
**VERIFIED.** `dashboard/pages/audit_log.py` registers no `@callback`. The table is never
populated. The page exists; the feature does not.

### 21. Connectors page is hardcoded empty
Layout sets `connectors = []` with a "not wired up, out of scope" comment. Renders
"Coming Soon" cards only.

### 22. AR reconciliation mislabels clients with no activity
**VERIFIED in a live run.** 13 of 16 clients had zero labour and zero invoiced and were
labelled `MATCHED`. Zero versus zero is silence, not agreement. Needs a distinct
no-activity state.

### 23. No WSGI entry point
**VERIFIED.** `dashboard/app.py` has no module-level `server = app.server`. A production
deployment fails on the missing line.

---

## Tier 6 — Data realism

### 24. 91 records against a design point of tens of thousands
46 QuickBooks + 45 Ruddr entities; 6 QB transactions + 4 Ruddr time entries. Signal B3
(amount co-occurrence) is barely exercised.

### 25. Whole classes of real-world case are absent
No non-English or non-Latin names. No duplicates within a single source. No one-to-many or
many-to-many merges — every ground-truth entity is a clean 1:1 pair. No records with
missing or empty names. No entities that change over time. No multi-tenant collisions.

### 26. `tenants.slug` collides for same-named customers
UNIQUE constraint on a name-derived slug. Two customers with the same name cannot both
exist.

---

## Tier 7 — Operational leftovers

### 27. Test rows in Postgres
**VERIFIED:** `audit_log` holds 2,488 rows generated by test runs. Deletion is blocked by
the tooling's safety classifier and needs to be run by hand.

### 28. Feature 17 (signup/onboarding) is blocked on product decisions
Not a code problem. Needs decisions on identity provider, customer credential encryption
and storage, and whether there is billing. All are expensive to reverse after real
customers exist.

### 29. The reality-check gate has no memory between rounds
It re-derives the full criteria list each run and flags a different subset, rather than
tracking what it already cleared. Observed: it cleared an item explicitly in one round and
flagged the same unchanged text in the next, and once asserted something the code
contradicted. Cost several rounds of rework this session.

---

## Suggested order

1 → 2 → 3 (make the system measurable and explainable)
4 → 5 → 6 (start collecting the corpus properly; it compounds from the day it starts)
12 → 14 (make it runnable by a person)
20, 22, 23 (finish the half-built surfaces)
7 → 9 → 8 (the learned model, only once there is a scoreboard)
24, 25 (more data, once its effect can be measured)
19 (design, once the content is settled)
