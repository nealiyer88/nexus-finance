# Feature Brief: Transaction Ingestion (Connector `read_transactions` → `transactions` table) — SQLite

**Author:** Neal Iyer
**Date:** 2026-08-29
**Status:** Draft
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution) — unblocks Phase 2
**Feature #:** 12a

---

## Problem Statement

Feature 8b shipped the `transactions` table and Signal B3 (amount co-occurrence), and `core/graph/entity_store.py` exposes `count_amount_cooccurrence_periods` as the reader that turns that table into a scoring boost. **Nothing in this repo ever writes a transaction row.** Both V1 connectors implement `read_transactions` and both return an empty list on every path a test or a fixture-mode run can reach, no module persists a `NormalizedTransaction`, and `tests/fixtures/` contains entity fixtures only.

Two things are consequently dark in production:

1. **Signal B3 is permanently zero.** `count_amount_cooccurrence_periods` queries a table that is always empty outside the rows `tests/test_scoring.py` seeds by hand, so the only Signal Set B member that carries money evidence contributes nothing to any real score. 8b's own criterion for "dark by default" was written as a temporary state; this feature ends it.
2. **AR reconciliation (feature 15) could only ever render an empty table.** Feature 15's brief names this exact gap in its Starting State and in its closing Open Question: it reads `transactions` and deliberately does not define a second money store, so with no writer it computes zero on the real fixture path.

**This feature is the missing writer, and nothing else.** It calls the transaction read on both connectors, persists each `NormalizedTransaction` into the shipped `transactions` table under the shipped UNIQUE constraint, and ships the transaction fixtures without which fixture mode has nothing to read.

Scoped to the **SQLite store every runtime and test path already uses** (rules §0 marks SQLite `[BUILT]`). Buildable, runnable and fully verifiable with no Postgres present and no `DATABASE_URL` set.

---

## Starting State (stated by symbol, re-derived at build time)

- **`transactions` is shipped and this feature does not alter it.** Its DDL lives in `db/schema_sqlite.sql` and, identically, in `db/migrations/003_transactions_sqlite.sql`. Its columns, its `UNIQUE (tenant_id, source, external_source_id)` constraint and its tenant-leading lookup indexes (`transactions_counterparty`, `transactions_canonical`) are read from those files at build time. **No new migration, no column added, no index added, no edit to either schema file.**
- **`NormalizedTransaction` is shipped** in `connectors/base.py` and is the shape both connectors already return. This feature does not change that dataclass.
- **Both transaction reads return empty under two separately-named conditions, not one — the second is easy to miss.** `QuickBooksConnector._fetch_raw_transactions` and `RuddrConnector._fetch_raw_time_entries` each open with the same guard pair, in this order:
  1. **fixture mode** — when `fixture_path` is set, the method returns an empty list immediately, without opening the fixture at all (the shipped docstrings say "Fixture-mode returns []" / "V1 fixtures are entities-only");
  2. **no HTTP client** — when `fixture_path` is unset *and* `http_client` is `None`, the method returns an empty list rather than attempting a live call.

  Both are why the mapping helpers `QuickBooksConnector._map_transaction` and `RuddrConnector._map_time_entry` — which are fully written and correct — are unreachable in every test and every local run. **This feature closes condition (1) only.** Condition (2) is the correct behaviour for a connector with no transport and stays exactly as it is.
- **`count_amount_cooccurrence_periods` is the consumer this write shape must satisfy.** It self-joins `transactions` on equal `period`, equal `currency` and *differing* `source`, filters the source side on `(source, counterparty_source_id)` and the candidate side on `canonical_id`, and applies the shipped `AMOUNT_TOLERANCE_PCT` / `AMOUNT_TOLERANCE_CAP` constants. Therefore a row is only ever useful to B3 if `period`, `currency`, `amount`, `source`, `counterparty_source_id` and (on the candidate side) `canonical_id` are all populated correctly. That column set — enumerated by name, not by count — is this feature's real contract.
- **`period` derivation is already specified by 8b** and is not open for reinvention — see Scope.
- **`core/ingestion/pipeline.py`'s `run_ingestion` is the shipped entity path** and is deliberately single-connector ("multi-connector orchestration is a later scheduling layer's job — NOT-SCOPE"). This feature's runner mirrors that stance rather than contradicting it.
- **`core/matching/pending_store.py` owns the SQLite connection provider.** `get_connection` resolves the path from `NEXUS_STORE_PATH`, falling back to `DEFAULT_STORE_PATH`, self-provisions the schema by applying `db/schema_sqlite.sql` plus every `*_sqlite.sql` migration listed from `db/migrations/` at call time, yields, and closes. Because `transactions` is defined in both the base schema and a `*_sqlite.sql` migration, **a store provisioned by that provider already has the table** — this feature adds no bootstrap of its own.
- **Fixture conventions** are set by `tests/fixtures/qb_entities.json` and `tests/fixtures/ruddr_entities.json`: a top-level JSON list of flat records, source-shaped keys, `id` as the source-system identifier, and validation-by-script in `tests/fixtures/generate_fixtures.py`.
- **`tests/test_scoring.py` is the only place that inserts transaction rows today**, via a private helper that writes the non-generated columns positionally and defaults `period` to the month prefix of `txn_date`. That helper is a test seeder, not a writer; this feature does not modify, import or replace it.

---

## Scope

### In Scope

- **Create `core/ingestion/transactions.py`** — module-level functions only, no class wrapper, `conn: sqlite3.Connection` first and `tenant_id` positioned per the shipped store-layer idiom (see Tenant Scoping). It exposes:

  | Symbol | Contract |
  |---|---|
  | `derive_period(txn_date) -> str` | The 8b rule, in exactly one place (see Period Derivation). Raises on a value from which no `YYYY-MM` bucket can be derived; never silently substitutes today's date. |
  | `upsert_transaction(conn, txn, tenant_id) -> int` | Persist one `NormalizedTransaction`; return the `txn_id` of the new or pre-existing row. Idempotent on the shipped UNIQUE constraint (see Upsert). |
  | `ingest_transactions(connector, conn, tenant_id, date_range) -> TransactionIngestionSummary` | Call `connector.read_transactions(date_range)` and upsert every element. One connector per call, mirroring `run_ingestion`. |
  | `TransactionIngestionSummary` | Frozen dataclass. Buckets are mutually exclusive and exhaustive over the transactions read: `read_total == inserted + updated + skipped`, with `skipped` carrying rows rejected by the write guards below. Same accounting discipline `IngestionSummary` in `core/ingestion/pipeline.py` already states. |

  This module **never calls `conn.commit()` or `conn.rollback()`** — the caller owns the transaction boundary, exactly as `core/graph/entity_store.py`, `core/matching/training_data.py` and `core/matching/pending_store.py` already do. Grep-asserted.

- **Period derivation — follow 8b, do not invent.** 8b's brief states the rule in its DDL comment, restates it in its Period Semantics section, and locks it with a period-derivation guard criterion: `period` is a `'YYYY-MM'` string bucket derived **from `txn_date`, at insert time, never from insert/load time** — the `created_at` failure mode B5 already suffered. `derive_period` is the single implementation of that rule in non-test code; no call site re-derives a month prefix inline. The invariant is asserted as a property over written rows (`period` equals the month bucket of that row's own `txn_date`), never against a date literal transcribed into this brief.

- **Counterparty reference — the source-side customer / client id.** `transactions.counterparty_source_id` is the **source system's own customer / client identifier**, because that is precisely what `count_amount_cooccurrence_periods` filters the source side on. Derivation is one documented helper in the new module, applied uniformly:
  - When the connector's `NormalizedTransaction.counterparty_kind` already names a customer- or client-side role, `counterparty_source_id` is carried through verbatim. This is the QuickBooks Invoice / Payment path, where `_map_transaction` sets the customer reference and the kind together.
  - When `counterparty_kind` names a role that is **not** the counterparty a receivable is owed by — RUDDR time entries carry the *resource* who logged the hours — the helper falls back to the client reference on `NormalizedTransaction.raw_record`, under the client-id key the RUDDR fixture ships (see Fixtures). The connectors' mapping helpers are **not** rewritten to do this; the write layer owns the interpretation, so no shipped mapper signature or behaviour moves.
  - When neither yields a value, the row is still written with a NULL `counterparty_source_id` (the column is nullable) and counted in `TransactionIngestionSummary`. A NULL-counterparty row is inert for B3 by construction — the join predicate cannot match on it — and that is the correct, lossless outcome. It is never silently dropped.

- **`canonical_id` — set only where `system_references` already resolves it, never invented.** After deriving the counterparty reference, the writer looks the pair `(source, counterparty_reference)` up against `system_references`, whose shipped `UNIQUE (source, external_id)` makes that lookup single-valued, and writes the resolved `canonical_id`. **When no row resolves, `canonical_id` is written as NULL and the transaction is still persisted.** No fuzzy matching, no alias search, no Stage 1–5 invocation, no entity creation: resolution is the matcher's job and this feature must not do it. The lookup is tenant-scoped by joining back to `canonical_entities` (`system_references` carries no `tenant_id` column of its own — the shipped scoping convention). It lives as a module-private reader in the new module rather than widening `core/graph/entity_store.py`'s public surface, so no shipped module is edited.
  - **A later run upgrades NULL to resolved.** Once the matcher resolves an entity, re-running ingestion over the same transactions re-derives `canonical_id` and updates the existing rows in place (see Upsert). This is the mechanism by which B3 comes alive without any backfill script.

- **Upsert — SELECT-then-branch on the shipped UNIQUE key. `INSERT OR REPLACE` is forbidden.** The repo's shipped idempotency idiom, used by `add_system_reference` and `upsert_edge` in `core/graph/entity_store.py` and by the enqueue path in `core/matching/pending_store.py`, is: **SELECT on the unique key, then either UPDATE the existing row in place or INSERT a fresh one** — never `INSERT OR REPLACE`. `upsert_transaction` follows it exactly, keyed on `(tenant_id, source, external_source_id)`:
  - Existing row → UPDATE the mutable columns (`category`, `txn_type`, `amount`, `currency`, `txn_date`, `period`, `counterparty_source_id`, `canonical_id`) in place, preserving `txn_id` and `created_at`, and count it as `updated`.
  - No existing row → INSERT and count it as `inserted`.
  - `INSERT OR REPLACE` would delete-and-reinsert, minting a new `txn_id` and resetting `created_at` on every re-run — the same class of silent destruction the pending-decision store rejected it for. **A grep asserts the string is absent from the new module.**
  - **Named hazard: SQLite treats NULLs as distinct in a UNIQUE index.** `transactions.tenant_id` is nullable, so a NULL-tenant row can never conflict with another NULL-tenant row and an `ON CONFLICT`-style upsert would silently duplicate on every run. The SELECT-then-branch idiom sidesteps this only if the SELECT uses `IS` rather than `=` for the tenant predicate — **or** if the tenant is required. This feature takes the stricter road: `upsert_transaction` and `ingest_transactions` **require a non-empty `tenant_id`** and raise `ValueError` otherwise, matching `run_ingestion`, whose `tenant_id` is likewise a required non-optional parameter. Reads remain optionally scoped (below). A criterion covers the raise.

- **Write guards, counted not swallowed.** A transaction is skipped (and counted in `skipped`) only when it cannot produce a valid row against the shipped `NOT NULL` columns: an empty `source_id` (nothing to key the UNIQUE constraint on) or a `txn_date` from which no period derives. Guards are explicit and specific; **no bare `except Exception` wraps the ingestion loop**, since such a clause passes vacuously on empty input and would hide exactly the mapping bug this feature exists to surface.

- **Connector fixture-mode transaction reads — close empty-return condition (1), both connectors.** Each connector gains one new constructor parameter for a transaction fixture file, appended after the existing `fixture_path` with a `None` default so **no existing construction site changes**, and each `_fetch_raw_*` guard chain becomes:
  1. transaction fixture path set → load that JSON list, filter to the inclusive `DateRange`, and return the raw records (the shipped mapping helpers then run unchanged);
  2. entity `fixture_path` set and no transaction fixture → return empty, **preserving today's behaviour byte-for-byte** so existing connector tests keep passing;
  3. `http_client is None` → return empty, unchanged;
  4. otherwise → the live API path, unchanged.

  Fixture loading reuses each connector's existing private fixture loader and its existing error type, so a missing or non-list fixture raises the same `ConnectorError` an entity fixture would. The QuickBooks path preserves the shipped per-type tagging its mapper reads, so `_map_transaction` distinguishes Invoice / Payment / Bill exactly as on the live path. **No mapper is rewritten; no public connector method signature changes; `read_transactions` still returns `List[NormalizedTransaction]`.**

- **Transaction FIXTURES for both connectors — a first-class deliverable, not a test detail.** Without these, fixture mode has nothing to read and this feature is untestable end to end. Two new files under `tests/fixtures/`, in the shipped fixture style (top-level JSON list, flat source-shaped records, `id` as the source identifier):
  - a **QuickBooks transactions** fixture covering Invoice, Payment and Bill, with the source-shaped amount, currency, transaction-date and customer/vendor-reference keys `_map_transaction` already reads;
  - a **RUDDR time-entries** fixture with the hours, rate, currency, date, resource-id and project-code keys `_map_time_entry` already reads, **plus the client-id key** the counterparty derivation above depends on.
  - **The two fixtures are deliberately cross-linked to the shipped entity fixtures and to `tests/fixtures/canonical_ground_truth.json`:** counterparty references point at ids that exist in `qb_entities.json` / `ruddr_entities.json`, and at least one ground-truth entity has same-period, same-currency, within-tolerance amounts on **both** sides, so B3 demonstrably fires on the fixture path. Amount tolerance is evaluated with the shipped `AMOUNT_TOLERANCE_PCT` / `AMOUNT_TOLERANCE_CAP` constants — never a re-declared literal.
  - Referential integrity is validated the way this repo already validates fixtures: new checks in `tests/fixtures/generate_fixtures.py` in the style of its existing cross-file checks, so a fixture that dangles fails loudly rather than quietly producing zero co-occurrences.
  - Every date in both fixtures is chosen so that periods align across the two sources; a fixture whose two sides never share a period would make every B3 criterion below vacuously pass.

- **Test suite `tests/test_transaction_ingestion.py`** — pure SQLite, no database server, no network, no `DATABASE_URL`, no live HTTP client. It builds its schema from files (`db/schema_sqlite.sql`, then the SQLite migrations), never from Python DDL, following the path-constant pattern in `tests/test_fixture_loads.py` and `tests/test_pending_decisions.py`.

### Out of Scope

- **Payment matching, AR aging, unbilled-labour computation, and any dashboard page or API route that displays money — feature 15.** This feature ships no `core/reconciliation/` package, no `api/routers/reconciliation.py` body, no Dash page, no aging buckets, no invoice↔payment pairing. It writes rows; feature 15 reads them. That boundary is the whole point of splitting this out.
- **Any change to the `transactions` DDL.** No new migration, no column, no index, no edit to `db/schema_sqlite.sql`, `db/schema.sql` or `db/migrations/003_*`. If a criterion here needs a column that does not exist, the criterion is wrong, not the schema.
- **Any change to `count_amount_cooccurrence_periods`, `core/matching/scoring.py`, or the B3 tiering / tolerance / cap semantics.** B3's behaviour is 8b's; this feature only stops starving it.
- **Entity resolution of counterparties.** No matcher invocation, no alias lookup, no entity creation, no backfill of `canonical_id` by any means other than an exact `system_references` hit.
- **Closing empty-return condition (2).** A connector with no transport and no fixture still returns empty. No live HTTP is added, no credential flow changes, no rate-limit behaviour changes.
- **Multi-connector orchestration, scheduling, incremental sync cursors, watermarks and backfill windows.** `ingest_transactions` takes one connector and one explicit `DateRange`, mirroring `run_ingestion`'s single-connector stance; a scheduler is a later layer's job.
- **Transaction line items.** The connectors read headers only in V1 (their own docstrings defer line items to V2) and nothing here changes that.
- **Postgres.** No entry added to `requirements.txt`, no driver imported, no new environment variable read, no mirror of anything into `db/schema.sql`, no edit to `tests/test_schema_parity.py`.
- **Any edit to `core/ingestion/pipeline.py`, `core/graph/entity_store.py`, `core/matching/scoring.py`, `core/matching/pending_store.py`, or `tests/test_scoring.py`.** This feature is additive: one new module, two guarded connector branches plus one new defaulted constructor parameter each, two new fixtures, fixture-validator additions, one new test file.

---

## Success Criteria

Every criterion runs with **no `DATABASE_URL` set and no Postgres installed**, via `.venv/bin/python -m pytest`.

- [ ] `from core.ingestion.transactions import derive_period, upsert_transaction, ingest_transactions, TransactionIngestionSummary` succeeds.
- [ ] **Fixtures exist and are non-empty.** Both new fixture files parse as JSON **lists**, and each has `len(...) > 0`. Every later criterion that iterates or counts is preceded by this precondition — no criterion in this brief may pass on an empty fixture.
- [ ] **Fixture referential integrity.** The set of counterparty references in the QuickBooks transactions fixture is non-empty, and every member resolves to an `id` present in `qb_entities.json`; the same holds for the RUDDR fixture against `ruddr_entities.json`. Asserted as `set(...)` non-emptiness first, then containment — never as a subset assertion against a possibly-empty left side.
- [ ] **Both connectors now read transactions in fixture mode.** Constructed with a transaction fixture path and **no** `http_client`, `QuickBooksConnector.read_transactions(range)` and `RuddrConnector.read_transactions(range)` each return a list whose length is `> 0`, and every element is a `NormalizedTransaction` whose `source_id` is non-empty, whose `txn_date` is non-empty, and whose `amount` is a finite number. (Length and field content, not an isinstance check alone.)
- [ ] **Empty-return condition (1) closed, condition (2) preserved.** With **only** the entity `fixture_path` set and no transaction fixture, both connectors still return an empty list. With neither fixture and `http_client is None`, both still return an empty list. Both asserted explicitly, so today's shipped contract is provably intact for callers that do not opt in.
- [ ] **Date-range filtering.** Given a `DateRange` that excludes some fixture rows, the returned count is strictly less than the count returned for a range covering all of them, **and** both counts are `> 0` — so the filter is proven to filter rather than to empty the result.
- [ ] **Rows land.** After `ingest_transactions` runs against each connector on a fresh in-memory database loaded from the schema files, `SELECT COUNT(*) FROM transactions` is `> 0`, equals `summary.inserted`, and `summary.read_total == summary.inserted + summary.updated + summary.skipped`.
- [ ] **Period derivation is the 8b rule, asserted as a property.** `SELECT txn_date, period FROM transactions` returns a non-empty row set, and for **every** row the stored `period` equals the `YYYY-MM` bucket of that row's **own** `txn_date`. A second assertion proves the rule is date-derived and not clock-derived: no stored `period` equals the current month unless that row's own `txn_date` falls in it. No date literal from this brief appears in the test.
- [ ] **`derive_period` rejects, never guesses.** Called with an empty string and with a non-date string, it raises; it never returns a bucket derived from the current time. Asserted with `pytest.raises` on the specific exception type, not a bare `except`.
- [ ] **Counterparty is the customer / client reference.** The set of non-NULL `counterparty_source_id` values written from the QuickBooks fixture is non-empty and is a subset of that fixture's customer/vendor references. The set written from the RUDDR fixture is non-empty and consists of **client** ids: the test first asserts the fixture's *resource*-id set is itself non-empty and disjoint from its client-id set (otherwise the next assertion proves nothing), then asserts every written counterparty is a member of the client-id set and none is a member of the resource-id set — locking in the deliberate fallback rather than accepting whatever the mapper happened to put in the field.
- [ ] **`canonical_id` is set where — and only where — `system_references` resolves it.** With a graph seeded from `canonical_ground_truth.json`, ingestion produces a non-empty set of rows with non-NULL `canonical_id`, and every such row's `(source, counterparty_source_id)` corresponds to a real `system_references` row. Ingesting the same fixtures against an **empty** graph produces the same total row count (itself asserted `> 0`) with **every** `canonical_id` NULL — proving nothing is invented.
- [ ] **NULL upgrades to resolved on re-run.** Ingest against an empty graph (all `canonical_id` NULL, row count `> 0`), then seed `system_references`, then ingest again: the row count is unchanged, the count of non-NULL `canonical_id` rows rises from zero to a number `> 0`, and no `txn_id` changes value.
- [ ] **Idempotency.** Running `ingest_transactions` twice with the same connector, range and tenant leaves `SELECT COUNT(*) FROM transactions` unchanged after the second run, reports `inserted == 0` and `updated > 0` on the second run, and leaves the set of `txn_id` values **identical** across runs (captured before and compared as sets, both non-empty). A `created_at` captured before the second run is unchanged after it.
- [ ] **`INSERT OR REPLACE` is absent.** `grep -in "insert or replace" core/ingestion/transactions.py` returns nothing.
- [ ] **Non-empty tenant is required on the write path.** `upsert_transaction` and `ingest_transactions` raise `ValueError` for `None` and for an empty-string tenant. A separate test proves rows written under two distinct tenants do not collide: both tenants' rows are present, the total is the sum of the two per-tenant counts, and each per-tenant count is `> 0`.
- [ ] **Transaction neutrality.** `grep -n "\.commit(\|\.rollback(" core/ingestion/transactions.py` returns nothing; a test that ingests and then rolls back on the caller's connection leaves `SELECT COUNT(*) FROM transactions` at its pre-call value, which it captured before the call.
- [ ] **B3 stops being zero — the headline criterion.** In one test: seed the graph from the ground-truth fixture, assert `count_amount_cooccurrence_periods(...)` returns `0` for the chosen pair **before** ingestion (the pre-state is asserted, not assumed), run `ingest_transactions` for **both** connectors, assert the resulting row count is `> 0`, then assert the same call now returns a value `>= 1`. A second assertion runs the same pair through `core.matching.scoring`'s pair-scoring entry point and asserts a `BoostEntry` with `signal_id == "B3"` is present in the resulting `signal_breakdown` boosts and that the boosts tuple is non-empty — the +0.20 cap and the tiering thresholds are 8b's and are not re-asserted here.
- [ ] **Guards count, they do not swallow.** A fixture record with an empty source id and one with an unparseable date are both reported in `summary.skipped` with `skipped > 0` while `summary.inserted > 0` for the valid remainder; `grep -n "except Exception" core/ingestion/transactions.py` returns nothing.
- [ ] **No bare UUID literals.** `tests/test_connectors_api.py`'s repo-wide UUID-literal scan over `api/`, `dashboard/` and `tests/` still passes; any identifier this feature's tests need is generated with `uuid.uuid4()`.
- [ ] **One engine, one connection provider.** `grep -n "sqlite3.connect" core/ingestion/transactions.py api/ dashboard/` returns no new match: this module never opens a connection, and `api/` / `dashboard/` continue to obtain SQLite solely through `core.matching.pending_store.get_connection`. `grep -rniE "psycopg|DATABASE_URL|postgres" core/ingestion/transactions.py` returns nothing, and `git diff requirements.txt` is empty.
- [ ] **Schema untouched.** `git diff db/` shows no change to `db/schema_sqlite.sql`, `db/schema.sql` or any file under `db/migrations/`, and no file is added there.
- [ ] `.venv/bin/python -m pytest tests/test_transaction_ingestion.py --collect-only -q` reports a **collected count greater than zero** — asserted on the reported count, not the exit code, because an unregistered marker silently deselects everything and still exits 0.
- [ ] `.venv/bin/python -m pytest tests/test_transaction_ingestion.py -x --tb=short` passes.
- [ ] `.venv/bin/python -m pytest tests/test_qb_connector.py tests/test_ruddr_connector.py tests/test_connector_base.py tests/test_scoring.py tests/test_pipeline.py tests/test_fixture_loads.py -x --tb=short` passes **unmodified** — no existing test file is edited to accommodate this feature.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` passes with no regression, and the collected count is greater than or equal to the count collected on the commit before this feature (both measured at build time; no number is written into this brief).

---

## Dependencies

- [ ] **Feature 4 (connector-base) — SHIPPED.** Supplies `ConnectorInterface`, `DateRange` and `NormalizedTransaction` in `connectors/base.py`. The contract is read at build time; `read_transactions`' signature does not move.
- [ ] **Feature 5 (qb-connector) — SHIPPED.** Supplies `QuickBooksConnector`, its transaction fetch guard chain and its `_map_transaction` mapper, which this feature makes reachable in fixture mode.
- [ ] **Feature 6 (ruddr-connector) — SHIPPED.** Supplies `RuddrConnector`, its time-entry fetch guard chain and its `_map_time_entry` mapper, likewise.
- [ ] **Feature 8b (b3-transactions-table-and-amount-signal) — SHIPPED.** Supplies the `transactions` table, its UNIQUE constraint and indexes, the `'YYYY-MM'`-from-`txn_date` period rule this feature obeys, the `AMOUNT_TOLERANCE_PCT` / `AMOUNT_TOLERANCE_CAP` constants, and `count_amount_cooccurrence_periods` — the reader whose expectations define this feature's write shape.
- [ ] **Feature 12 (matcher-orchestrator) — SHIPPED.** Supplies `core/matching/engine.py` and `core/ingestion/pipeline.py`'s `run_ingestion`, the single-connector ingestion stance this feature's runner mirrors and the path that populates `system_references` so `canonical_id` can resolve. Neither module is edited.
- **Downstream, not a dependency:** feature 15 (AR reconciliation) reads the rows this feature writes. Nothing in this brief may reference feature 15's modules, routes or pages.

---

## Estimated Complexity

**Rating:** M

**Rationale:** One new module of module-level functions, two narrow guarded branches in shipped connectors, two fixtures and one test file — no new DDL, no new dependency, no shipped public signature moved. The load-bearing risks are (a) the **NULL-tenant UNIQUE hazard**, where SQLite's distinct-NULLs semantics turn a naive upsert into a silent duplicator on every run; (b) **fixture cross-linking**, where a fixture whose two sides never share a period or a tolerance-satisfying amount would let every B3 criterion pass vacuously while the signal stays dark — the exact failure this feature exists to prevent; and (c) **`canonical_id` discipline**, where the temptation to "help" by fuzzy-resolving an unmatched counterparty would quietly reimplement the matcher inside the writer. Rated M rather than L because the correctness contract is owned by another feature's reader and must be derived from it, not transcribed.

---

## PROJECT CONTEXT

### Pipeline position

```
connector.read_transactions(DateRange)      ← fixture branch added here (condition 1)
    → NormalizedTransaction (shipped shape, shipped mappers)
        → THIS FEATURE: ingest_transactions → upsert_transaction
             period            = derive_period(txn_date)        [8b rule]
             counterparty      = source-side customer/client id
             canonical_id      = system_references hit, else NULL
             key               = (tenant_id, source, external_source_id)
             idiom             = SELECT-then-branch → UPDATE | INSERT
        → transactions rows
             → count_amount_cooccurrence_periods (8b)  → Signal B3 fires
             → feature 15 reads for AR reconciliation  (NOT this feature)
```

### Why the writer, not the connector, owns counterparty interpretation

The connectors' mappers are correct for what they describe: RUDDR's time entry genuinely has a *resource* as its immediate counterparty. What the `transactions` table needs is the party a receivable is owed by, which is a **store-layer** concept defined by the B3 join, not a connector-layer one. Putting the interpretation in the writer keeps both mappers untouched, keeps the rule in one place, and means a future connector inherits the behaviour by returning a well-formed `NormalizedTransaction` rather than by copying logic.

### Named tradeoff — month-bucket periods

`period` is a calendar-month bucket, so billing lag that pushes a QuickBooks invoice into the month after the RUDDR labour it bills produces a false negative in B3. This is 8b's accepted V1 tradeoff, restated here so no builder "improves" it: false negatives degrade gracefully inside a corroborative boost budget, and a `period ± 1` window is documented follow-up tuning that only real ingestion volume can justify. **This feature must not widen the window.**

### Idempotency Requirements

- One row per `(tenant_id, source, external_source_id)`, ever. Re-running refreshes the mutable columns in place and preserves `txn_id` and `created_at`.
- Idempotency is enforced by SELECT-then-branch on the shipped UNIQUE key, never by `INSERT OR REPLACE`, and never by an `ON CONFLICT` clause whose tenant component can be NULL.
- Re-running after the matcher has resolved more entities is the supported and expected way `canonical_id` fills in. It is an UPDATE, not a second row.

### Implementation Notes (constraints for the build)

1. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` / `from connectors...` import fails to collect.
2. **A criterion that only checks an exit code is not a criterion.** Assert on the reported collected count.
3. **Tests build their schema from files, not from Python DDL.** The schema files are the single source of truth and gain tables from other features.
4. **Every count or comparison is preceded by a non-emptiness assertion.** A subset check against an empty set, or a loop over an empty derived set, passes while proving nothing — and on this feature specifically, an empty `transactions` table is the exact bug being fixed.
5. **Derive, do not transcribe.** Column names come from `PRAGMA table_info`, tolerance from the shipped constants, the migration list from the directory, the fixture contents from the fixture files. No number describing today's tree is written into this brief.
6. **Never commit or roll back inside the new module.** The caller owns the boundary, exactly as every shipped store module does.
7. **No bare UUID literals in `api/`, `dashboard/` or `tests/`** — generate with `uuid.uuid4()`. A shipped repo-wide scan enforces this.
8. **Cite symbols, not coordinates.** Every fact this brief asserts about the tree is a symbol name, a docstring phrase, a DDL constraint or a grep — never a file-and-line, never a count. Upstream features land between briefing and build.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — the only engine any code uses. Transaction rows land here.
- `[BUILT]` `transactions` and Signal B3 (8b) — consumed, never modified.
- `[BUILT]` Connector set is QuickBooks (accounting) + RUDDR (psa). No third connector is scaffolded.
- `[BUILT]` Shadow Ledger — untouched. This feature is read-side ingestion only; no `execute_write` behaviour changes.
- `[PLANNED]` Postgres, `DATABASE_URL`, any driver — treated as not existing.
- `[PLANNED]` RLS. Tenant scoping is an explicit parameter carried into a SQL predicate; no `CREATE POLICY` anywhere.

### Relevant Spec Sections

- Section 9: Stage 3 — Signal Set B, B3 amount co-occurrence (the signal this feature feeds)
- Section 8: System Architecture — idempotency everywhere
- Feature 15's brief: the downstream reader, and the source of the "nothing writes transaction rows yet" gap this feature closes
