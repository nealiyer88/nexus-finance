# Feature Brief: B3 Transactions Table + Amount Co-occurrence Signal

**Author:** Neal Iyer
**Date:** 2026-08-22 (promoted from stub; folds in the 8b hardened design of 2026-07-05)
**Status:** Approved
**Complexity:** M+
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 8b (follow-up to 8a; prerequisite for feature 12's 95% gate measurement)

---

## Problem Statement

Signal B3 — amount co-occurrence within an amount tolerance in the same period, +0.10–0.15 per spec v4 §9 — is the only Signal Set B signal that could not be built on the existing V1 schema. Feature 8a shipped B1, B2, B4, B5 and B6 against `canonical_entities` / `entity_aliases` / `entity_edges` / `system_references`, and explicitly deferred B3 here because it needs transaction rows, and no transactions table exists. (See CC-LEARNINGS 2026-06-21: a prior attempt to scaffold B3 without its data dependency shipped no-op stubs and deadlocked the rocket loop; 8a therefore carried a hard "B3 must be absent" reviewer guard.)

Two things make B3 worth building now rather than at ingestion time. First, `_compute_b_boosts` already assumes a six-signal vocabulary and a +0.20 renormalized budget; with only five signals implementable, the budget can never be exercised at its ceiling by the full signal set, and the cap mechanic stays partly untested. Second, `NormalizedTransaction` (`connectors/base.py:67-85`) is already the connector contract's transaction shape, so the persistence shape is decided — deferring the table means re-litigating schema later when ingestion lands.

This feature adds the minimal `transactions` table in both SQL dialects, one tenant-scoped query helper, and the B3 branch inside the existing band-gated Signal Set B dispatch. No connector ingestion, no pipeline-shape change.

An explicitly accepted consequence: until a follow-up feature wires `read_transactions` → persistence, B3 is **dark outside tests** (QB fixture-mode `read_transactions` returns `[]`; RUDDR fixtures are entities-only). That is recorded in the Risk Register, not hidden. It is regression-safe — an empty table yields count 0, no `BoostEntry`, and zero score delta.

---

## Scope

### In Scope

- **New `transactions` table** in `db/schema.sql` (Postgres, aspirational store) and `db/schema_sqlite.sql` (the only real engine), plus the migration pair `db/migrations/003_transactions.sql` and `db/migrations/003_transactions_sqlite.sql`. Pinned shape (dialect types split as noted):

  ```sql
  transactions(
    txn_id                 INTEGER PRIMARY KEY AUTOINCREMENT | BIGSERIAL PRIMARY KEY,
    tenant_id              TEXT | UUID NOT NULL REFERENCES tenants(id),
    source                 TEXT NOT NULL,          -- 'quickbooks' | 'ruddr'
    category               TEXT NOT NULL,          -- 'accounting' | 'psa'
    external_source_id     TEXT NOT NULL,          -- source-system transaction id
    txn_type               TEXT NOT NULL,          -- 'invoice' | 'payment' | 'bill' | 'time_entry' | ...
    amount                 REAL | NUMERIC(18,2) NOT NULL,
    currency               TEXT NOT NULL DEFAULT 'USD',
    txn_date               TEXT | DATE NOT NULL,   -- ISO-8601
    period                 TEXT NOT NULL,          -- 'YYYY-MM', derived from txn_date at insert
    counterparty_source_id TEXT,                   -- source-system entity id the txn belongs to
    canonical_id           TEXT REFERENCES canonical_entities(canonical_id),
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP | TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source, external_source_id)
  );
  CREATE INDEX IF NOT EXISTS transactions_counterparty ON transactions (tenant_id, source, counterparty_source_id, period);
  CREATE INDEX IF NOT EXISTS transactions_canonical    ON transactions (tenant_id, canonical_id, period);
  ```

  Tenant scoping follows the repo's actual convention: a `tenant_id` column plus explicit query predicates, exactly as `canonical_entities` and `system_references` do. **No `CREATE POLICY`, no Postgres row-level security** — none exists anywhere in this tree and none is to be invented (rules §0, §10).

- **Join keys (load-bearing, D2 of the hardened design).** On the normal Stage 3 path the source entity is UNRESOLVED (`source_canonical_id is None`), so any design keying B3 on the source's canonical id would be permanently dark in production — the same bug class fixed in B4 on 2026-07-05. Therefore:
  - Source side joins on `(source, counterparty_source_id) = (entity.source, entity.source_id)`.
  - Candidate side joins on the nullable `canonical_id` column.

- **Period semantics.** `period` is a `'YYYY-MM'` string bucket **derived from `txn_date` at insert time — never from insert/load time** (the B5 `created_at` failure mode). A guard test asserts `period == txn_date[:7]` for every seeded row, and the helper contributes nothing for a row whose `period` disagrees with its `txn_date` month.

- **Amount tolerance — symmetric by construction.** A co-occurrence exists in period P when a source-side row and a candidate-side row from **different source systems, same currency**, satisfy:

  ```
  ABS(ABS(a.amount) - ABS(b.amount)) <= MIN(MAX(ABS(a.amount), ABS(b.amount)) * 0.02, 500)
  ```

  Anchoring on `MAX(|a|,|b|)` makes `B3(a,b) == B3(b,a)` by construction (we shipped an asymmetric-B5 bug once already). `ABS()` on the amounts handles credit memos. **`AMOUNT_TOLERANCE` is not currently a Python constant anywhere in the tree** — it exists only as prose in rules §5. This feature DEFINES it: module-level `AMOUNT_TOLERANCE_PCT: float = 0.02` and `AMOUNT_TOLERANCE_CAP: float = 500.0` (USD, V1) in `core/graph/entity_store.py`, next to the query that applies them.

- **New query helper** in `core/graph/entity_store.py`, following that module's existing conventions (module-level function, parameterized SQL only, `tenant_id: Optional[str] = None` last):

  ```python
  count_amount_cooccurrence_periods(conn, source, source_entity_id, candidate_canonical_id, tenant_id=None) -> int
  ```

  Returns the number of DISTINCT periods containing at least one co-occurring row pair under the semantics above. One query, no per-row Python filtering.

- **B3 in `core/matching/scoring.py::_compute_b_boosts`:**
  - Add `"B3"` to BOTH `Literal` annotations (the `BoostEntry.signal_id` field and the local `raws` list annotation at `scoring.py:239`).
  - New optional parameter `amount_cooccurrence_periods: Optional[int] = None`; `None` means "skip B3" so existing direct-call tests keep working unchanged.
  - The B3 block sits **inside** the existing band gate (`B_BOOST_BAND_LOW <= base_score < B_BOOST_BAND_HIGH`, `scoring.py:236`), like every other B signal.
  - Tiering (mirrors B2): `+0.10` for exactly one distinct co-occurring period, `+0.15` for two or more.
  - Itemized in `signal_breakdown` with signal id, raw and applied values.

- **`score_pair` wiring:** query the helper **once**, only when `in_b_band` (`scoring.py:688`), mirroring the precomputed-count pattern already used for B1/B6 and the `candidate_ext` gating pattern. Entity side keyed on `(entity.source, entity.source_id)` — never on `source_canonical_id`.

- **Signal Set B budget:** B3 participates in the existing +0.20 Signal Set B budget, which is implemented as **proportional renormalization, not a hard clamp** (`scoring.py:303`: when `sum(raw) > MAX_B_BOOST`, every entry's `applied` is scaled by `MAX_B_BOOST / total_raw`, so the applied values sum to exactly 0.20 and their relative proportions are preserved). Do not add a per-signal clamp and do not change the cap logic.

- **Schema parity:** `tests/test_schema_parity.py` needs TWO edits, not one — add `"transactions"` to the hardcoded `SHARED_TABLES` list AND add a matching `"transactions": {"tenant_id"}` entry to the `EXCLUDE` dict (`tests/test_schema_parity.py:46-51`). `test_column_parity` subscripts `EXCLUDE[table]` directly (`:121-122`, no `.get()`, no default), so a `SHARED_TABLES` entry without its `EXCLUDE` key raises `KeyError`. That list is the enforcement mechanism, and it compares column names across the two base schema files only.

- **Tests:** new section appended to `tests/test_scoring.py`, plus direct helper tests seeding raw SQL rows. Twelve named cases enumerated under Success Criteria.

### Out of Scope

- **Connector ingestion.** No QB/RUDDR `read_transactions` → persist wiring. Rows enter only via a test helper seeding raw SQL. A follow-up feature owns ingestion.
- **Currency conversion.** Same-currency is required; cross-currency rows simply never co-occur. The $500 cap is USD, V1.
- **Amount aggregation.** Row-level co-occurrence only. QB invoice `TotalAmt` vs RUDDR per-entry amounts frequently will not match row-level in the wild; summing RUDDR entries per project per period is a materially larger feature that needs real ingestion data to validate, and is deferred.
- **Period windows beyond exact-month equality.** Billing lag (Feb work billed in March) will cause false negatives; accepted for V1 because B3 is corroborative upside inside a renormalized budget, so false negatives degrade gracefully while month-granularity plus tolerance plus same-currency guard the dangerous direction. Revisit when real ingestion data makes the drift measurable.
- **Any change to B1, B2, B4, B5, B6**, to Signal Set C, to `weights.py`, or to `disposition.py`.
- **Any change to shipped 8a tests** — in particular `tests/test_scoring.py:886 test_b_boosts_total_applied_capped_at_max` (which pins the 4-signal case) must remain byte-unmodified; the 5-signal cap case is a NEW test.
- **Stage 6 graph-update wiring.** No new pip dependencies.
- **Raising the disposition cutoffs.** 0.90 / 0.70 / 0.50 in `core/matching/disposition.py:54-56` are unchanged.

---

## Success Criteria

Every criterion is asserted by `.venv/bin/python -m pytest tests/ -x --tb=short` from the repo root.

- [ ] `transactions` exists in BOTH `db/schema.sql` and `db/schema_sqlite.sql` with the pinned columns, `UNIQUE (tenant_id, source, external_source_id)`, and the two indexes; **`tests/test_schema_parity.py` receives BOTH required edits — `"transactions"` appended to `SHARED_TABLES` AND `"transactions": {"tenant_id"}` added to the `EXCLUDE` dict (`tests/test_schema_parity.py:46-51`)** — and `test_column_parity` passes for all five tables. Both edits are mandatory: `test_column_parity` subscripts `EXCLUDE[table]` at `tests/test_schema_parity.py:121-122` with no `.get()` and no default, so adding a table to `SHARED_TABLES` without its `EXCLUDE` key raises `KeyError` and the test errors on the new parametrization the moment the criterion is followed. The value is `{"tenant_id"}`, not `set()`: `transactions` declares `tenant_id` in both dialects but with a deliberate dialect split (SQLite nullable `TEXT`, Postgres `UUID NOT NULL REFERENCES tenants(id)`), which is exactly the `canonical_entities` case — and `canonical_entities` is entered as `{"tenant_id"}` (`:47`), as is `system_references` (`:50`). Match that convention verbatim; only `entity_aliases` and `entity_edges`, which have no `tenant_id` column in either dialect, use `set()`.
- [ ] `db/migrations/003_transactions.sql` and `db/migrations/003_transactions_sqlite.sql` both exist and use `CREATE TABLE IF NOT EXISTS` (do NOT copy migration 002's Postgres `DROP TABLE ... CASCADE` pattern); the SQLite migration applies cleanly and idempotently on top of `db/schema_sqlite.sql`.
- [ ] **(1) Symmetry** — `B3(entity → candidate) == B3(candidate → entity)` at a boundary amount, asserted via two mirrored `score_pair` calls.
- [ ] **(2) Credit memo** — amounts `-1000.00` and `1000.00` co-occur (ABS semantics).
- [ ] **(3) Tolerance boundary** — a delta exactly equal to `MIN(MAX(|a|,|b|)*0.02, 500)` fires; one cent over does not. Covered on both sides of the `MIN` (percentage-bound and $500-bound).
- [ ] **(4) Tiering** — 1 distinct period → raw `+0.10`; 2 distinct periods → raw `+0.15`; a single period containing three qualifying row pairs still counts once.
- [ ] **(5) Cross-currency** — USD and EUR rows with identical amounts never co-occur.
- [ ] **(6) Same-source** — two `quickbooks` rows never co-occur with each other.
- [ ] **(7) Band gate** — B3 produces no entry at `base_score` 0.65 or 0.92 even with perfect co-occurrence.
- [ ] **(8) Cap collision (NEW test)** — B1+B2+B3+B4+B5 with raw sum > 0.20 renormalizes proportionally: `sum(e.applied for e in entries) == 0.20` exactly, each `applied == raw * (0.20 / total_raw)`, and all five entries present in `signal_breakdown`. The shipped 4-signal cap test is untouched.
- [ ] **(9) Period derivation** — a seeded row with `txn_date = '2026-03-15'` carries `period = '2026-03'`; the helper contributes 0 for a row whose `period` disagrees with its `txn_date` month.
- [ ] **(10) Tenant scoping** — rows under a different `tenant_id` are invisible to the helper when `tenant_id` is set.
- [ ] **(11) Dark by default — proven in-process, no baseline artifact** — with an empty `transactions` table: (a) `count_amount_cooccurrence_periods` returns `0`; (b) no `BoostEntry` with `signal_id == "B3"` appears in `signal_breakdown.b_boosts`; (c) the score is proven unchanged **within a single test, computing both sides in-process — no committed baseline fixture, no comparison against any recorded pre-8b number**. For one pair and one `conn`, call `_compute_b_boosts(...)` twice with identical arguments except the new parameter: once with `amount_cooccurrence_periods=None` (B3 disabled — the pre-8b code path) and once with `amount_cooccurrence_periods=0` (B3 enabled, no evidence). Assert the two returned `tuple[BoostEntry, ...]` are equal element-for-element on `signal_id`, `raw` and `applied`, then assert `_weighted_score(base_score, boosts_disabled) == _weighted_score(base_score, boosts_enabled)` using exact `==`, not `pytest.approx`. The tuple equality is the load-bearing half, not the final float: the +0.20 budget is proportional renormalization (`scoring.py:303`), so a spurious zero-valued B3 entry appended to `raws` would raise `total_raw` and rescale every other signal's `applied` even though B3 itself contributes nothing. The correct implementation appends nothing to `raws` when the period count is 0, leaving `total_raw`, `scale` and every `applied` bit-identical on both paths.
- [ ] **(12) Unresolved-source path** — B3 fires for a pair whose source entity has `source_canonical_id is None`, proving the join keys do not require resolution.
- [ ] `_compute_b_boosts(..., amount_cooccurrence_periods=None)` skips B3 entirely; all pre-existing direct-call tests pass unmodified.
- [ ] `score_pair` invokes `count_amount_cooccurrence_periods` at most once per pair and only when `in_b_band`.
- [ ] Full suite green with no regression to the 8a baseline, run twice: once as-is, and once with `models/cc.en.300-compress.bin` temporarily moved aside (both must pass).

---

## Dependencies

- [ ] Feature 8a (fastText + Signal Set B reconciliation) SHIPPED — this extends `core/matching/scoring.py` and `core/graph/entity_store.py` in place. **CC must read the shipped code and reconcile, not greenfield.**
- [ ] `.claude/rules/01-nexus-finance-v1.md` §1 amended to state B3 lands with 8b (already applied).
- [ ] Rules §0 read and honoured: anything not marked `[BUILT]` is to be treated as not existing. Relevant here — SQLite is the only real engine `[BUILT]`; `db/schema.sql` and `db/migrations/` are `[PLANNED]`/aspirational and read only as text by `tests/test_schema_parity.py`; there is no RLS anywhere `[PLANNED]`.
- Connector transaction ingestion is NOT a dependency of this feature, and this feature does not gate it.

---

## Estimated Complexity

**Rating:** M+

**Rationale:** Upgraded from the stub's "M". The schema work spans two dialects plus a migration pair plus the parity list, and the signal work touches two shipped modules whose conventions (band gate, precomputed-count pattern, proportional renormalization, `Literal` annotations in two places) must be matched rather than reinvented. The real cost is the test surface: twelve named cases, several of them boundary/symmetry cases that exist specifically because this repo has already shipped an asymmetric-signal bug and a dark-signal bug. Engineer estimate: ~2.5 build sessions. The load-bearing risk is not difficulty but discipline — the June-21 failure here was invention in the gap between an underspecified brief and the prompt, which is why every semantic above is pinned rather than described.

---

## PROJECT CONTEXT

### Pipeline position (unchanged shape)

```
Stage 3 Scoring:  Signal Set A (RapidFuzz) + Signal Set C (fastText cosine)
                  + Signal Set B (B1, B2, [NEW] B3, B4, B5, B6 — band-gated [0.70, 0.90),
                    +0.20 proportionally renormalized, every boost logged)
                  + category-pair weight dispatch                          → ScoredMatch

New data surface: transactions (tenant-scoped table; joined by
                  (source, counterparty_source_id) on the source side and
                  canonical_id on the candidate side)
```

### Implementation Notes (constraints for the build)

1. **Column naming — decision.** Where the two hardened-design documents disagreed (`external_source_id` vs `source_txn_id`), **`external_source_id` wins**, applied consistently throughout this brief. Reason: the existing tables already use the `external_*` family for source-system identifiers — `system_references(source, external_id)` with `UNIQUE (tenant_id, source, external_id)` in `db/schema.sql:81` — so `UNIQUE (tenant_id, source, external_source_id)` is a direct parallel, whereas `source_txn_id` would introduce a `*_txn_id` naming family that appears nowhere in the schema. Index naming likewise follows the existing `<table>_<suffix>` convention (`canonical_entities_type`, `system_references_canonical`), not an `idx_` prefix.
2. **Index set — decision.** Same conflict resolved toward the tenant-leading composites listed in Scope: one unique constraint `(tenant_id, source, external_source_id)` and two lookup indexes, `(tenant_id, source, counterparty_source_id, period)` for the source side and `(tenant_id, canonical_id, period)` for the candidate side — one per join key, both tenant-leading because every query filters tenant first.
3. **No PG row-level security.** "Tenant-scoped" in this repo means a `tenant_id` column plus WHERE predicates. Do not write `CREATE POLICY`; none exists in the tree.
4. **Mirror the `tenant_id` typing pattern per dialect** exactly as `canonical_entities` does it (TEXT in SQLite, UUID FK in Postgres) to avoid parity drift.
5. **The +0.20 budget is proportional renormalization, not a clamp.** `scoring.py:303` computes `scale = MAX_B_BOOST / total_raw` when the raw sum exceeds the cap and multiplies every entry. B3 adds a raw value to the list and nothing else; cap logic is untouched.
6. **`"B3"` must be added in two places** — the `BoostEntry.signal_id` `Literal` and the `raws` local annotation. Missing either is a type-level regression.
7. **B-signal constants live module-level in `scoring.py`** near the other B constants (`MAX_B_BOOST`, `B_BOOST_BAND_LOW/HIGH`); the tolerance constants live in `entity_store.py` where the SQL predicate that uses them lives.
8. **Test data is seeded, and that is acknowledged.** There are no transaction fixtures, so the ground-truth suite intentionally leaves B3 dark. Tests seed raw SQL rows shaped by the semantics above. Real-data validation is deferred to the ingestion follow-up — recorded here, not hidden.
9. **Test command is `.venv/bin/python -m pytest tests/ -x --tb=short`.** Bare `pytest` is not on PATH, and the console script would not put the repo root on `sys.path`. Run it before the first change to record a baseline, and after each step.

### V1 Hard Constraints (still binding)

- Connectors QB + RUDDR only. SQLite is the only real engine. Shadow Ledger only.
- Graph-corroborated scoring uses deterministic SQL joins, not LLM calls. B3 adds a SQL join, no LLM surface.
- Neo4j, fine-tuned fastText, XGBoost, GraphRAG, self-hosted LLM, write-back all remain NOT-SCOPE.
- Confidence thresholds unchanged at `core/matching/disposition.py:54-56` (0.90 / 0.70 / 0.50).

### Risk Register

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| B3 fires only in tests until ingestion ships — no production contribution to feature 12's 95% gate | High | Explicit non-goal plus a follow-up ingestion feature; feature 12's measurement plan must account for B3 being dark. Empty table ⇒ zero score delta, so it is regression-safe. |
| Cap saturation renormalizes B3 down to near-invisibility in common cases even after ingestion lands | Medium | The 5-signal cap-collision test proves the mechanic; feature 12 tracks per-signal applied contribution to detect chronic clipping. |
| Schema-parity drift between dialects (`tenant_id` typing, index definitions) | Medium | Mirror the `canonical_entities` pattern exactly; `tests/test_schema_parity.py` `SHARED_TABLES` enforces column-name parity at CI. |
| `period` derived from load time instead of `txn_date` (the B5 `created_at` failure mode) | Medium | Hard rule plus the period-derivation guard test (criterion 9). |
| Month-bucket equality misses cross-month invoice/time-entry alignment (billing lag) | Medium | Accepted for V1 — false negatives degrade gracefully inside a corroborative budget. Documented as follow-up tuning (consider a period±1 window) once ingestion makes real drift measurable. |
| Underspecified brief invites builder invention (the 2026-06-21 failure mode) | Low, now | Every semantic — table shape, join keys, period derivation, tolerance formula, tiering, cap behaviour — is pinned in this brief. The build prompt derives from here. |

### Relevant Spec Sections (v4)

- §9 Stage 3 Signal Set B — B1–B6 enumeration, B3 amount co-occurrence +0.10–0.15, +0.20 total, audit logging
- §7 Phase 1 auto-match gate 95% (measured by feature 12, not by this feature)
- §17 V1 Build Scope
- Rules §0 (BUILT vs PLANNED), §5 (thresholds and `AMOUNT_TOLERANCE` prose), §10 (tenant scoping — column plus predicates, no RLS)
