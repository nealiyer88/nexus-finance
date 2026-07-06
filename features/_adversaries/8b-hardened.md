# Hardened Design: 8b — B3 Transactions Table + Amount Co-occurrence Signal

## Resolved Disagreements

**1. Is scope "M" honest without connector ingestion?**
Winner: **Skeptic (partial)** + **Engineer**. The brief's "M" is optimistic; Engineer confirmed 2.5 sessions realistic. Resolution: keep ingestion out of scope but re-label complexity **M+** and require fixture-only inserts via a test helper. Skeptic's "empty in production" critique is acknowledged in the Risk Register but does not block 8b — feature 12's gate measurement runs against fixtures + a follow-up ingestion feature, not 8b alone.

**2. Defer the table entirely and compute B3 transiently?**
Winner: **Design**. Decisive argument: Engineer confirmed `_compute_b_boosts` (`core/matching/scoring.py:212-307`) already assumes a six-signal vocabulary and `NormalizedTransaction` (`connectors/base.py:67-85`) is the natural persistence shape. A transient signal would leave the +0.20 cap untestable at ceiling and re-litigate schema when ingestion lands.

**3. Is "period" a column or an algorithm?**
Winner: **Skeptic**. Engineer flagged the same ambiguity. Resolution baked into brief below: `period = YYYY-MM` string bucket computed from `txn_date` at insert.

**4. Which side's TotalAmt drives tolerance?**
Winner: **Skeptic**. Under-specified in original. Resolution: `abs(a-b) ≤ min(max(a,b) * 0.02, 500)`.

**5. Dual schema dialects — trivial or non-trivial?**
Winner: **Design**. Engineer confirmed `test_schema_parity.py` is the enforcement mechanism and the pattern is established. Two dialects stays in scope.

## Engineer Flags

- **Period definition undefined** → Resolved: `YYYY-MM` string bucket from `txn_date`, computed at insert time.
- **Tolerance basis ambiguous** → Resolved: `abs(a-b) ≤ min(max(a,b) * 0.02, 500)`.
- **Keying at Stage 3 where `source_id` is often None** → Resolved: table indexed by `(tenant_id, source, external_source_id)` with nullable `canonical_id`, plus `(tenant_id, counterparty_source_id, period)` index for B3 lookup.
- **RLS type drift (UUID vs TEXT)** → Resolved: mirror the exact `tenant_id` typing pattern from `canonical_entities` in each dialect.
- **False PASS on cap-collision** → Resolved: mandatory explicit test that B3 + B1 + B2 + B5 firing together respect the +0.20 cap after renormalization.
- **Ambiguous-zone gate placement** → Resolved: B3 sits inside the existing `base_score in [0.70, 0.90)` guard at `scoring.py:236`.
- **Connector ingestion scope creep** → Resolved: fixture-only inserts via test helper. No `read_transactions → persist` wiring in 8b.

## Hardened Brief

**Ship in 8b:**

1. **New table `transactions`** in both `db/schema.sql` (Postgres) and `db/schema_sqlite.sql`, plus migration `db/migrations/003_transactions.sql`. Columns:
   - `tenant_id` (type matching existing `canonical_entities` pattern per dialect)
   - `source` TEXT (e.g. "quickbooks", "ruddr")
   - `category` TEXT (e.g. "accounting", "psa")
   - `external_source_id` TEXT (source-system txn id)
   - `canonical_id` TEXT NULL (FK-nullable to `canonical_entities`)
   - `counterparty_source_id` TEXT NULL (source-system entity ref)
   - `amount` NUMERIC/REAL
   - `currency` TEXT
   - `txn_date` DATE/TEXT
   - `period` TEXT (YYYY-MM, computed from `txn_date` at insert)
   - Indexes: `(tenant_id, source, external_source_id)` unique; `(tenant_id, counterparty_source_id, period)` lookup.
   - RLS scoped by `tenant_id` per rules §10.

2. **SQLite helper** (new function alongside `count_shared_person_neighbors`): `amount_cooccurrence_count(tenant_id, source_ref, candidate_ref, period, amount) -> int` returning count of counterparty transactions within tolerance in the same period.

3. **B3 in `core/matching/scoring.py::_compute_b_boosts`**:
   - Extend `Literal["B1","B2","B4","B5","B6"]` at line 239 to include `"B3"`.
   - Add `raws.append(("B3", b3_raw))` block **inside** the `base_score in [0.70, 0.90)` guard.
   - `b3_raw` fires when ≥1 counterparty transaction exists in same `period` with `abs(a-b) ≤ min(max(a,b) * 0.02, 500)`. Raw boost 0.10–0.15 per v4 §9.
   - Participates in existing +0.20 proportional cap (lines 302-306) — no cap logic changes.
   - Itemized in `signal_breakdown`.

4. **Tests in `tests/test_scoring.py`**:
   - B3 fires on matching amount + period.
   - B3 does not fire outside 0.70–0.90 band.
   - B3 respects tolerance at edge (`max(a,b)*0.02` and `$500` boundary).
   - B3 + B1 + B2 + B5 firing together respect +0.20 cap after renormalization (cap-collision).
   - Tenant scoping: cross-tenant transactions do not fire B3.
   - Schema parity via existing `test_schema_parity.py`.

**Non-goals (removed/preserved):**
- No connector `read_transactions → persist` pipeline. Fixtures only via test helper.
- No changes to B1/B2/B4/B5/B6.
- No Stage 6 graph-update wiring.
- No re-tuning of category-pair weight dispatch.

**Success criteria:**
- All new + existing tests green.
- `test_schema_parity.py` green across both dialects.
- `signal_breakdown` structurally includes B3 when it fires.
- Cap-collision test proves +0.20 ceiling holds.

**Complexity:** M+ (2.5 build sessions per Engineer).

**Dependencies:** 8a SHIPPED.

## Risk Register

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| B3 fires only in tests until ingestion ships — no production signal contribution to feature 12's 95% gate | High | Explicit non-goal + follow-up feature to wire `read_transactions` persistence; feature 12 measurement plan must account for this. |
| Cap saturation clips B3 in common cases, making it invisible in production even after ingestion lands | Medium | Cap-collision test proves the mechanic; measurement in feature 12 tracks per-signal contribution to detect chronic clipping. |
| Schema-parity drift between Postgres and SQLite (`tenant_id` type, index definitions) | Medium | Mirror `canonical_entities` pattern exactly; `test_schema_parity.py` enforces at CI. |
| Period bucketing (`YYYY-MM`) misses cross-month invoice/time-entry alignment (Skeptic's Feb-work-billed-March case) | Medium | Accepted for V1; documented as follow-up tuning once ingestion ships and real drift is measurable. |