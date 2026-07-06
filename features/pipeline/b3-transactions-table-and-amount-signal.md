# Feature Brief: B3 Transactions Table + Amount Co-occurrence Signal

**Author:** Neal Iyer
**Date:** 2026-07-01
**Status:** Stub (to be expanded before build)
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 8b (follow-up to 8a; prerequisite for feature 12's 95% gate measurement)

---

## Why this feature exists

Signal B3 (amount co-occurrence within `AMOUNT_TOLERANCE` in the same period, +0.10–0.15 boost per v4 §9) is the only Signal Set B signal that cannot be built on the existing V1 schema — it requires a transactions table, which does not exist. Feature 8a shipped B1, B2, B4, B5, B6 against existing schema and explicitly deferred B3 here (see CC-LEARNINGS 2026-06-21: the prior attempt to scaffold B3 without its data dependency deadlocked the rocket loop).

## What 8b ships

- **Minimal transactions table** added to `db/schema.sql` and `db/schema_sqlite.sql`: transaction rows carrying source system, category, canonical/source entity reference, amount, and period — just enough to evaluate amount co-occurrence. RLS-scoped by `tenant_id` like every other table (rules §10).
- **B3 signal implementation** in `core/matching/scoring.py` `_compute_b_boosts`: amount co-occurrence within `AMOUNT_TOLERANCE = min(TotalAmt * 0.02, $500)` in the same period, +0.10–0.15 boost, gated to the 0.70–0.90 ambiguous zone, itemized in `signal_breakdown`, participating in the existing +0.20 Signal Set B hard cap.
- **Tests:** extend `tests/test_scoring.py`; schema migration tests.

## Out of Scope

- Transaction ingestion from connectors (QB/RUDDR `read_transactions` wiring) beyond what tests require — a follow-up if not trivially includable.
- Any change to the other five B-signals shipped in 8a.

## Dependencies

- Feature 8a SHIPPED.

## Estimated Complexity

**Rating:** M — one new table (two schema dialects), one signal in an existing dispatch structure, tests.
