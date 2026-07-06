# Build Prompt: 8b — B3 Transactions Table + Amount Co-occurrence Signal

## SITUATION

Repo: /Users/nealiyer/code/nexus-finance (branch `feature/b3-transactions-table-and-amount-signal`, forked from main after 8a merged via PR #11). Python 3.10+ matching pipeline for cross-system entity resolution (QuickBooks accounting ↔ RUDDR PSA).

Read BEFORE writing any code:
1. `.claude/rules/01-nexus-finance-v1.md` — V1 guardrails. §5: `AMOUNT_TOLERANCE = min(TotalAmt * 0.02, $500)`. §10: every table tenant-scoped. §1 (amended): B3 lands with this feature.
2. `features/_adversaries/b3-transactions-table-and-amount-signal.md` — the HARDENED DESIGN. Its decisions D1–D6 are BINDING: table shape, join keys, period semantics, tolerance formula, tiering. Where this prompt and the stub brief disagree with it, the hardened design wins.
3. `core/matching/scoring.py` — `_compute_b_boosts` (the dispatch you extend: band gate via `B_BOOST_BAND_LOW/HIGH`, +0.20 proportional cap `MAX_B_BOOST`, `BoostEntry`), `score_pair` (the `in_b_band` gating pattern for `candidate_ext`, precomputed-count pattern for B1/B6).
4. `core/graph/entity_store.py` — read-only query helper conventions (tenant_id Optional, parameterized SQL).
5. `db/schema.sql`, `db/schema_sqlite.sql`, `db/migrations/002_llm_training_data.sql` + `db/migrations/002_llm_training_data_sqlite.sql` — dialect pair pattern.
6. `tests/test_scoring.py` section 12 (B-boost seed/mock test patterns), `tests/test_schema_parity.py` (`SHARED_TABLES` hardcoded list).

Shipped prior: 8a (Signal Set B = B1, B2, B4, B5, B6; fastText Signal Set C with dynamic-budget renormalization; Stage 4 abbreviation rescue). 316→340 tests green with model present.

## TASK

Add the `transactions` table (both SQL dialects + migration 003 pair) and implement Signal B3 (amount co-occurrence) in the existing `_compute_b_boosts` dispatch, per the hardened design.

## FILE PATHS

Modify:
- `db/schema.sql` — add `transactions` table + 2 indexes
- `db/schema_sqlite.sql` — same table, SQLite dialect
- `core/graph/entity_store.py` — add `count_amount_cooccurrence_periods(...)`
- `core/matching/scoring.py` — add "B3" to BOTH `Literal` annotations; B3 block in `_compute_b_boosts` (new optional param `amount_cooccurrence_periods: Optional[int] = None`); `score_pair` queries the helper once when `in_b_band` and passes the count
- `tests/test_scoring.py` — B3 tests (hardened design test list items 1–11) appended as a new section
- `tests/test_schema_parity.py` — add `transactions` to `SHARED_TABLES`

Create:
- `db/migrations/003_transactions.sql` (Postgres; `CREATE TABLE IF NOT EXISTS` — do NOT copy 002's `DROP TABLE ... CASCADE`)
- `db/migrations/003_transactions_sqlite.sql` (SQLite mirror)

Touch NOTHING else. No changes to B1/B2/B4/B5/B6 logic, no changes to shipped tests (especially `test_b_boosts_total_applied_capped_at_max` — write a NEW 5-signal cap test instead).

## CONVENTIONS

- snake_case functions, UPPER_CASE constants, frozen dataclasses.
- entity_store helpers: module-level functions, `tenant_id: Optional[str] = None` last param, parameterized SQL only, tenant filter via JOIN/WHERE (no PG RLS policies).
- scoring.py is the only matcher module importing rapidfuzz — B3 adds no imports beyond what exists.
- All B-signal constants module-level in scoring.py near the other B constants.
- Docstrings state constraints (join keys, symmetry, ABS semantics) not narration.

## TEST COMMAND

```
python3 -m pytest tests/ -x --tb=short
```
Must pass from repo root. Run it BEFORE your first change (baseline: 340 passed with model present / 338+2 skipped without) and after each numbered step. Any previously-passing test that fails is a regression you must fix.

## ACCEPTANCE CRITERIA

- [ ] `transactions` in BOTH `db/schema.sql` and `db/schema_sqlite.sql` with the hardened-design columns, `UNIQUE(tenant_id, source, source_txn_id)`, indexes `(source, counterparty_source_id, period)` and `(canonical_id, period)`; parity test passes with `transactions` in `SHARED_TABLES`.
- [ ] Migration 003 PG+SQLite pair exists; SQLite migration applies cleanly on top of `schema_sqlite.sql` (idempotent `IF NOT EXISTS`).
- [ ] `count_amount_cooccurrence_periods(conn, source, source_entity_id, candidate_canonical_id, tenant_id) -> int` returns the number of DISTINCT periods containing ≥1 co-occurring row pair where: source-side rows match `(source, counterparty_source_id) = (source, source_entity_id)`, candidate-side rows match `canonical_id = candidate_canonical_id`, `row.source` values DIFFER between the two sides, currencies are EQUAL, and `ABS(ABS(a.amount) - ABS(b.amount)) <= MIN(MAX(ABS(a.amount), ABS(b.amount)) * 0.02, 500)`, with both rows in the same `period`.
- [ ] B3 in `_compute_b_boosts`: fires only in the existing band gate; raw boost `+0.10` (1 period) / `+0.15` (≥2 periods); `signal_id="B3"`; participates in the +0.20 proportional cap; `amount_cooccurrence_periods=None` → B3 skipped (direct-call test compat).
- [ ] `score_pair` computes the count ONLY when `in_b_band` (mirror `candidate_ext`) and passes it through; entity side keyed on `(entity.source, entity.source_id)` — never on `source_canonical_id`.
- [ ] Tests 1–11 from the hardened design list, incl. NEW 5-signal cap test asserting `sum(applied) == 0.20` exactly, symmetry test, ABS/credit-memo test, tolerance boundary test, cross-currency & same-source negative tests, band-gate test, tenant-scoping test, empty-table darkness test, period-derivation guard.
- [ ] Full suite green; the shipped 8a 4-signal cap test UNMODIFIED.

## NON-GOALS

- No connector ingestion wiring (QB/RUDDR `read_transactions` → table).
- No currency conversion; cross-currency rows simply never co-occur.
- No aggregation of amounts (row-level co-occurrence only).
- No period windows beyond exact-month equality.
- No changes to Signal Set C, weights, disposition, or the other five B signals.
- No new pip dependencies.

## EXECUTION (ONE STEP AT A TIME)

1. Baseline: run the test command; record the pass count.
2. Schema: add `transactions` to both dialect files; write migration 003 pair; extend `SHARED_TABLES`; run tests.
3. Helper: implement `count_amount_cooccurrence_periods` in entity_store.py; unit-test it directly (seed raw SQL rows) covering tolerance boundary, ABS, currency, same-source exclusion, distinct-period counting, tenant scoping.
4. Signal: add "B3" to both Literals; B3 block in `_compute_b_boosts`; wire `score_pair` (band-gated). Run tests.
5. Scoring-level tests: band gate, cap with 5 signals, symmetry via two mirrored `score_pair` calls, empty-table darkness.
6. Full suite green twice: once as-is; once with `models/cc.en.300-compress.bin` temporarily moved aside (restore after). Both must pass.
7. Commit on this branch: `feat: 8b — transactions table + B3 amount co-occurrence signal` with a body summarizing table shape and signal semantics. Do not push.
