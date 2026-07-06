# Engineer Adversary — Feasibility Assessment: 8b B3

## 1. Can this be built as specified? **Yes, partially — with two ambiguities the brief must resolve before build.**

The scaffolding is genuinely favorable:
- `_compute_b_boosts` (`core/matching/scoring.py:212-307`) is a clean append-a-raw-tuple structure. Adding B3 is one `raws.append(("B3", b3_raw))` block plus extending the `Literal["B1","B2","B4","B5","B6"]` on line 239 to include "B3". The proportional cap logic already handles the new signal at line 302-306.
- `NormalizedTransaction` (`connectors/base.py:67-85`) exists with source_id/source/category/amount/currency/txn_date/counterparty_source_id — a natural persistence shape. `read_transactions` is implemented on both QB (`connectors/quickbooks.py:267`) and RUDDR (`connectors/ruddr.py:198`) connectors, so ingestion wiring is closer to trivial than the brief suggests.
- Confirmed no `transactions` table in `db/schema.sql` or `db/schema_sqlite.sql` — only `last_transaction TEXT` on `entity_edges`. Brief's premise is correct.

## 2. Technical blockers

**None hard.** But two under-specified pieces will trip the builder:

- **"Same period" is undefined.** Month bucket? Quarter? A ±N-day window on `txn_date`? Signal Set B v4 §9 likely says; brief must quote it verbatim or the builder will guess and fixtures will silently drift.
- **`AMOUNT_TOLERANCE = min(TotalAmt * 0.02, $500)` — which side's TotalAmt?** Source txn amount, candidate txn amount, or `abs(a-b) ≤ min(max(a,b)*0.02, 500)`? Rules §5 defines the constant but not its base in a pairwise co-occurrence context.
- **Keying:** B3 fires at Stage 3 where `source_id` is often `None` (see the B4 comment at line 260-266). Transactions must be indexed by both `(canonical_id | NULL, source, external_source_id)` so unresolved candidates can be queried by source-system counterparty ID, not just canonical_id. Brief says "canonical/source entity reference" — good, but the sqlite lookup helper (analogous to `count_shared_person_neighbors`) needs to be written.

## 3. Effort estimate — **2–3 build sessions, not "M"**

- Schema in 2 dialects + `003_transactions.sql` migration + keep `test_schema_parity.py` green: **~0.5 session**
- B3 implementation + Literal type extension + `BoostEntry` propagation + `signal_breakdown` inclusion: **~0.5 session**
- New sqlite helper (`amount_cooccurrence_count` or similar) + fixture rows: **~0.5 session**
- Tests in `tests/test_scoring.py` covering: fires, doesn't fire outside band, respects tolerance edge, participates in +0.20 cap, tenant scoping: **~0.75 session**
- Debug + schema-parity churn + period-definition clarification: **~0.5 session buffer**

## 4. Implementation risks

- **False PASS on scoring test.** Builder will assert `"B3" in signal_breakdown` without asserting the applied value survives cap renormalization when combined with B1+B2+B5. Require an explicit cap-collision test.
- **RLS drift.** `tenant_id UUID` (Postgres) vs `TEXT` (sqlite) — schema-parity test will fail if types diverge from the pattern in `canonical_entities`. Follow the existing convention exactly.
- **Ambiguous zone gate.** B3 must be inside the `base_score in [0.70, 0.90)` guard (line 236). If the builder puts B3 above the guard "because it's data-based, not graph-based," it'll fire outside the band and inflate auto-approves.
- **Scope creep on ingestion.** Brief marks connector wiring out of scope but "beyond what tests require" is a squishy phrase. Recommend: fixture-only inserts via a test helper; no `read_transactions → persist` pipeline in 8b.

## 5. Recommended approach

Ship as brief describes, but pin these before build:
1. Freeze the "period" definition (recommend: `YYYY-MM` string bucket, computed from `txn_date` at insert).
2. Freeze the tolerance formula: `abs(a-b) ≤ min(max(a,b)*0.02, 500)`.
3. Add a `transactions` table keyed by `(tenant_id, source, external_source_id)` with an FK-nullable `canonical_id`, plus `(tenant_id, counterparty_source_id, period)` index for the B3 lookup.
4. Write the sqlite helper before touching `_compute_b_boosts` — mirrors the shape of `count_shared_person_neighbors`.
5. No connector-side ingestion in 8b. Fixtures only.

**Verdict:** Feasible. Real complexity is M-to-M+, closer to 2.5 sessions than 1.