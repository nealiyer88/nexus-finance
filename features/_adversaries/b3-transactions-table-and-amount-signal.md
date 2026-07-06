# Hardened Design: 8b — B3 Transactions Table + Amount Co-occurrence Signal

**Date:** 2026-07-05 (Rocket run 8b→10→11)
**Inputs:** adversary-design, adversary-skeptic, adversary-engineer (full outputs below the fold; this document is the reconciliation)

---

## Disagreements and decisions

### D1 — Build now as one feature vs. split into 8b-1 (table+measurement) / 8b-2 (signal)
**Winner: Design Advocate / Engineer (build as one feature).** The skeptic's split assumes a measurement-first path exists — it does not: there is no real transaction data anywhere (QB fixture-mode `read_transactions` returns `[]`, RUDDR fixtures are entities-only), so 8b-1's "measure co-occurrence rates on real pairs" would measure builder-invented fixtures, exactly what the skeptic objects to. The split buys a second rocket cycle, not evidence. The skeptic's REAL point — that the stub brief's undefined terms invite a June-21-style brief/prompt divergence — is won a different way: **this document pins every open semantic before Phase 2**, so the brief is no longer a stub by the time the builder sees a prompt. The reconcile happens here, not in Phase 3.

### D2 — Source-side join key (the "dark signal" trap)
**Winner: Engineer, wholesale.** On the normal Stage-3 path the source entity is UNRESOLVED (`source_canonical_id=None`); any design keying B3 on the source's canonical id is permanently dark in production (the B4 bug class, fixed 2026-07-05). Decision:
- Source side joins on **`(source, counterparty_source_id) = (entity.source, entity.source_id)`**.
- Candidate side joins on a **nullable `canonical_id` column** referencing `canonical_entities`.
- Following the B1/B6 pattern: **`score_pair` precomputes `amount_cooccurrence_periods` (gated on `in_b_band`, like `candidate_ext`) and passes the count into `_compute_b_boosts`** — no signature explosion, no per-signal queries inside the dispatch.
This kills the skeptic's "signal that never fires on unresolved entities" objection: the join requires no resolution.

### D3 — "Same period" semantics
**Winner: Engineer (calendar month), with the Skeptic's B5-trap guard adopted as a hard rule.** `period TEXT` in `'YYYY-MM'` form, **derived from `txn_date` at insert** — NEVER from insert/load time (the B5 `created_at` failure mode, CC-LEARNINGS 2026-07-05). A CHECK-style test asserts period == txn_date[:7] for seeded rows. The skeptic's billing-lag concern (QB invoices lag RUDDR work by weeks → same-month equality misses true pairs) is REAL but acceptable for V1: B3 is corroborative upside inside a +0.20 cap, so false negatives degrade gracefully; false positives are the danger and month-granularity + tolerance + same-currency guards against those. Logged as a known limitation for 8b follow-up tuning (consider period+1 window when real ingestion data exists).

### D4 — Which amounts, and aggregation
**Winner: Engineer (row-level co-occurrence), Skeptic's aggregation deferred.** V1 semantics: a co-occurrence exists in period P when a source-side row and a candidate-side row from **different source systems, same currency**, satisfy `ABS(ABS(a.amount) - ABS(b.amount)) <= MIN(MAX(ABS(a.amount), ABS(b.amount)) * 0.02, 500)`. `ABS()` on amounts handles credit memos. The skeptic is right that QB invoice `TotalAmt` vs RUDDR per-entry amounts rarely match row-level in the wild — but aggregation (sum RUDDR per project per period) is a materially larger feature requiring real ingestion, which the brief explicitly defers. Row-level is what "just enough to evaluate amount co-occurrence" means.

### D5 — Tolerance symmetry
**Winner: Skeptic (the objection), resolved by the Engineer's formula.** Anchoring `TotalAmt * 0.02` on **max(|a|,|b|)** makes B3(a,b) == B3(b,a) by construction. Dedicated symmetry test required (we shipped the asymmetric-B5 bug class once already).

### D6 — Boost tiering
**Adopted (mirrors B2):** `+0.10` for exactly one distinct co-occurring period, `+0.15` for ≥2 distinct periods. Participates in the existing +0.20 proportional cap. Band-gated [0.70, 0.90) like all B signals.

## Scope adjustments
- ADDED (vs stub): concrete table shape, join keys, period semantics, tolerance formula, tiering — pinned above.
- ADDED: `tests/test_schema_parity.py` `SHARED_TABLES` list must gain `transactions` (hardcoded list; parity only checks the two base schema files, so the table lands in BOTH `db/schema.sql` and `db/schema_sqlite.sql`, plus migration 003 pair).
- REMOVED / CONFIRMED OUT: connector ingestion wiring; currency conversion (same-currency required; $500 cap documented as USD V1); aggregation semantics; any change to B1/B2/B4/B5/B6.
- CONSTRAINT: the shipped 8a test `test_b_boosts_total_applied_capped_at_max` (pins B1+B2+B4+B5 = 4 signals) must NOT be modified; a NEW test covers the cap with B3 included (5 signals).

## Table shape (both dialects, engineer's spec)
```sql
transactions(
  txn_id            INTEGER/SERIAL PRIMARY KEY,
  tenant_id         TEXT NULL,
  source            TEXT NOT NULL,           -- 'quickbooks' | 'ruddr'
  category          TEXT NOT NULL,           -- 'accounting' | 'psa'
  source_txn_id     TEXT NOT NULL,
  txn_type          TEXT NOT NULL,
  amount            REAL/NUMERIC NOT NULL,
  currency          TEXT NOT NULL DEFAULT 'USD',
  txn_date          TEXT NOT NULL,           -- ISO date
  period            TEXT NOT NULL,           -- 'YYYY-MM', derived from txn_date
  counterparty_source_id TEXT NULL,          -- source-system entity id the txn belongs to
  canonical_id      TEXT NULL REFERENCES canonical_entities(canonical_id),
  created_at        (dialect default),
  UNIQUE(tenant_id, source, source_txn_id)
)
-- indexes: (source, counterparty_source_id, period), (canonical_id, period)
```
"RLS-scoped" per repo convention = `tenant_id` column + query predicates (schema.sql has no CREATE POLICY; do not invent PG RLS).

## Implementation constraints (engineer)
- `BoostEntry.signal_id` Literal gains `"B3"` in BOTH annotations (dataclass + `raws` local).
- New `entity_store` helper `count_amount_cooccurrence_periods(conn, source, source_entity_id, candidate_canonical_id, tenant_id) -> int` — single query, tenant-scoped, different-source + same-currency + ABS-tolerance + distinct-period semantics.
- `score_pair` queries it once, only when `in_b_band`, passes count into `_compute_b_boosts` (default `None` → 0 / skip for direct-call test compat).
- Migration 003: PG + SQLite mirror, safe `CREATE TABLE IF NOT EXISTS` in SQLite dialect (llm_training_data NIT: do not copy the PG `DROP TABLE CASCADE` pattern — use `CREATE TABLE IF NOT EXISTS` in both).

## Real risks (skeptic, sustained)
- Stub brief → builder invention: neutralized by this document; the prompt derives from HERE, not the brief.
- Period from load-time instead of txn_date: hard rule + test (D3).
- Fixture-invented validation: acknowledged — tests seed raw SQL rows shaped by the D2/D4 semantics; ground-truth suite intentionally has B3 dark (no transaction fixtures), which is regression-safe. Real-data validation deferred to ingestion follow-up. This is recorded, not hidden.

## Phantom risks (dismissed)
- "B3 dark on unresolved path" — false under D2 join keys.
- "95% gate needs B3 therefore invalid" (design overclaim) — the gate measurement (feature 12) is more honest with B3 present, but B3's absence was never the gate's only gap; not load-bearing for this build either way.

## Test cases the brief missed (from debate)
1. Symmetry: B3(entity→candidate) == B3(candidate→entity) at a boundary amount.
2. Credit memo: amounts -1000.00 vs 1000.00 co-occur (ABS).
3. Tolerance boundary: delta exactly == min(max*0.02, 500) fires; +0.01 over does not.
4. Tiering: 1 distinct period → +0.10; 2 → +0.15; same period counted once even with 3 row-pairs in it.
5. Cross-currency rows never co-occur (USD vs EUR, identical amounts).
6. Same-source rows never co-occur (two QB rows).
7. Band gate: B3 returns nothing at base 0.65 / 0.92 even with perfect co-occurrence.
8. Cap: B1+B2+B3+B4+B5 raw > 0.20 → proportional scale, sum(applied) == 0.20 exactly (NEW test; 8a's 4-signal test untouched).
9. Period derivation: seeded row with txn_date '2026-03-15' must carry period '2026-03'; helper returns 0 if period column disagrees with txn_date month (guard test).
10. Tenant scoping: rows under other tenant_id invisible.
11. Empty table: count 0, no B3 entry, zero score delta (dark-by-default).
12. Schema parity: `transactions` present in both dialects with matching columns (SHARED_TABLES extended).
