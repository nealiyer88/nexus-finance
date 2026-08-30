# Feature Brief: AR Reconciliation Module

**Author:** Neal Iyer
**Date:** 2026-05-10 (last revised 2026-08-29)
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 2 (AR/AP Recon)
**Feature #:** 15 (depends on 12, 12a and 14; see Dependencies for the contracts it also needs)

---

## Problem Statement

The first FP&A feature built on the canonical entity graph. For each resolved client entity, cross-reference RUDDR labor against QB invoiced revenue. "RUDDR shows 450 hrs × $200/hr = $90K for Cenlar FSB. QB shows $65K invoiced. $25K unbilled." This query is impossible without cross-category entity resolution — RUDDR knows "cenlar-fsb" and QB knows "Cenlar, LLC." but neither knows they're the same client. The canonical entity graph does.

The money rows this feature reads are written by **feature 12a (transaction-ingestion)**, which persists normalized transactions from both connectors into the `transactions` table and lights up matching signal B3. 12a is a hard prerequisite of this feature; feature 15 aggregates and reports over the rows 12a ingests and defines no writer of its own.

---

## Starting State

Read this before assuming anything below is a create-from-nothing.

- `api/routers/reconciliation.py` **exists** as a docstring-only "Not yet implemented" stub with no executable body. `api/main.py` already registers the connectors, approvals and entities routers and adds the audit and tenant middleware; wiring this router in is an append of one more registration beside the existing ones.
- `dashboard/pages/ar_reconciliation.py` **exists** as a `dash.register_page(__name__, path="/ar-reconciliation", name="AR Reconciliation")` placeholder whose body is an `html.H1` plus a TODO comment. The Dash application shell shipped with feature 16: `dashboard/app.py` is **out of bounds for this feature and must not be edited**. Two shipped constraints follow. First, the module-level `layout` must build with **no database**: no connection opened, no query issued, no store file touched at import or layout-build time — all data access lives inside callback bodies. Second, a shipped live test GETs every registered page path and asserts a 200 response; a `layout` that touches a database or raises will turn that already-green test red.
- `core/reconciliation/` does not exist. Net-new package.
- **The money data now has a real home.** Feature 8b shipped a `transactions` table in `db/schema_sqlite.sql` with columns `txn_id, tenant_id, source, category, external_source_id, txn_type, amount, currency, txn_date, period, counterparty_source_id, canonical_id, created_at`, `UNIQUE (tenant_id, source, external_source_id)`, and indices on `(tenant_id, source, counterparty_source_id, period)` and `(tenant_id, canonical_id, period)`. This feature **reads that table; it does not define a second money store.** Postgres is live in this tree and connected at application startup; transaction **reads** for this feature nonetheless stay on SQLite, which is where the `transactions` table is created and populated.
- **Transaction rows are written by feature 12a (transaction-ingestion).** 12a persists normalized transactions from both connectors, adds transaction fixtures, and lights up matching signal B3. This feature reads the rows 12a writes and adds no writer of its own.
- Amount tolerance is **already a shipped constant pair**: `AMOUNT_TOLERANCE_PCT` and `AMOUNT_TOLERANCE_CAP` in `core/graph/entity_store.py`, encoding rules §5's `min(amount × pct, cap)`. This feature **imports them**; it must not re-declare a literal tolerance.
- Tenant scoping at the **store layer** is an explicit `tenant_id: Optional[str] = None` function parameter carried into a SQL predicate — the `conn`-first, optional-`tenant_id`-last idiom throughout `core/graph/entity_store.py`. At the **router layer** the convention is the opposite: every shipped router reads the tenant from `request.state.tenant_id`, stamped by the shipped tenant middleware, and parses no tenant parameter of its own. `canonical_entities` and `transactions` carry a `tenant_id` column; `system_references`, `entity_aliases` and `entity_edges` do **not** — they are scoped by joining to `canonical_entities`. There is no row-level security anywhere: no `CREATE POLICY`, no `current_setting`, no session context. Rules §10 marks RLS `[PLANNED]`.
- **No bare UUID literals** anywhere under `api/`, `dashboard/` or `tests/`. Identifiers are generated with `uuid.uuid4()`; never type a UUID string into source or a test.
- **One engine in the API layer.** The API opens exactly one engine; SQLite connections are obtained only through the pending-store connection provider (`core.matching.pending_store.get_connection`), never by calling `sqlite3.connect` inside `api/`.

---

## Scope

### In Scope

- Fill in `api/routers/reconciliation.py` (stub exists) and add its registration beside the existing router registrations in `api/main.py`:
  - `GET /reconciliation/ar` — AR reconciliation report for tenant, showing per-client: RUDDR labor total, QB invoiced total, variance, status (matched | unbilled | overbilled)
  - `GET /reconciliation/ar/{canonical_id}` — per-entity detail: line-item breakdown of RUDDR transactions vs QB invoices
  - Both routes read the tenant from `request.state.tenant_id` (stamped by the shipped tenant middleware) and declare no tenant parameter of their own — the shipped router convention. They pass that tenant down to `core/reconciliation/ar.py`, where the explicit `conn`-first / optional-`tenant_id`-last parameter idiom applies and becomes a SQL predicate. Tenant scoping is by predicate, not RLS.

- Fill in `dashboard/pages/ar_reconciliation.py` (placeholder exists; keep its registered path unchanged):
  - Summary table: canonical client name, RUDDR labor $, QB invoiced $, variance $, variance %, status flag
  - Color-coded status: green (matched within tolerance), amber (unbilled beyond tolerance), red (variance beyond the variance-% band)
  - Expandable detail: RUDDR rows on left, QB rows on right, joined by `canonical_id`
  - **Structure contract** (so the page is testable without a browser): `layout` is a zero-argument callable; the summary is a `dash_table.DataTable` with `id="ar-recon-table"`; and a pure `classify_rows(rows: list[dict]) -> list[dict]` helper performs status assignment. Tests exercise the helper directly on fixture rows.
  - Amount tolerance is imported from `core.graph.entity_store` (`AMOUNT_TOLERANCE_PCT`, `AMOUNT_TOLERANCE_CAP`), never re-literalled here.

- Reconciliation logic in `core/reconciliation/ar.py` (new package):
  - Read side is `transactions`, tenant-scoped, in `entity_store`'s style: a `sqlite3.Connection` plus `tenant_id: Optional[str] = None`.
  - PSA-side labor total = SUM of `amount` over rows with `category = 'psa'`; accounting-side invoiced total = SUM of `amount` over rows with `category = 'accounting'` and the invoice `txn_type`. Rows attach to a client either directly via `transactions.canonical_id`, or via `(source, counterparty_source_id)` joined to `system_references (source, external_id)` for rows whose `canonical_id` is still null — the same dual join key 8b established, and for the same reason: the source side is frequently unresolved.
  - **Money arrives as `amount` already.** Do not recompute `hours × billing_rate` in this feature — `NormalizedTransaction` carries `amount`, and hours/rate live only in connector fixtures as entity attributes, not as transaction rows. Rate arithmetic belongs to feature 12a, which writes `transactions` rows.
  - Calculate variance and classify: MATCHED, UNBILLED, OVERBILLED.
  - Handle partial coverage: PSA rows exist for a client with no accounting-side counterpart in the same period.

- **Test suite:** `tests/test_ar_reconciliation.py`
  - Seed a SQLite graph from the schema plus fixtures, then seed `transactions` rows directly (tests seed rows in the shape 12a ingests; they do not exercise 12a's writer).
  - Assert: variance for a seeded client equals the difference of the two sums the test itself computed from the rows it inserted.
  - Assert: unbilled labor flagged when PSA rows exist for a canonical with no accounting-side rows.
  - Assert: tolerance boundary behaves as `min(amount × AMOUNT_TOLERANCE_PCT, AMOUNT_TOLERANCE_CAP)` — assert against the imported constants, not against typed-in numbers.
  - Assert: a second tenant's rows never appear in the first tenant's report.

### Out of Scope

- AP reconciliation — V2 with Bill.com connector
- Cash flow forecasting — Phase 3
- Write-back of reconciliation results to QB or RUDDR — Shadow Ledger only
- Historical trend analysis — V2
- **Transaction ingestion** — persisting connector `read_transactions()` output into the `transactions` table is feature 12a's work, not this feature's.
- The Dash application shell — `dashboard/app.py` shipped with feature 16 and must not be edited here.

---

## Success Criteria

- [ ] `core/reconciliation/ar.py` exists and exposes the aggregation entry point. Tenant predicate is checked by execution, not by inspection: call the entry point with a non-None `tenant_id` while capturing every SQL statement the connection actually executes (e.g. via `sqlite3.Connection.set_trace_callback`), assert the captured set is non-empty, and assert every captured statement that reads `transactions` or `canonical_entities` contains a `tenant_id` predicate. Do not enumerate a hard-coded list of expected queries.
- [ ] `api/routers/reconciliation.py` exposes both routes, and one router registration for it is added beside the existing router registrations in `api/main.py`: assert both paths appear in `{r.path for r in api.main.app.routes}`, and assert the previously registered routers are still present.
- [ ] `dashboard/pages/ar_reconciliation.py` still registers at the path its own source declares (derive both sides at runtime; compare the registered path to the `path=` argument parsed from the module source — no hardcoded expectation), `layout` is callable, and its tree contains a `dash_table.DataTable` with `id == "ar-recon-table"`.
- [ ] Per-client variance equals PSA-side total minus accounting-side total, where **both sides are recomputed by the test from the rows it seeded** — no expected total is written into the brief or the test as a literal.
- [ ] Tolerance: for a seeded pair whose absolute difference is strictly below `min(max(|a|,|b|) * AMOUNT_TOLERANCE_PCT, AMOUNT_TOLERANCE_CAP)` the status is MATCHED, and for a pair strictly above it is not MATCHED. Both bounds computed in the test from the imported constants.
- [ ] Unbilled flagged: a canonical with seeded PSA rows and zero accounting-side rows classifies UNBILLED.
- [ ] Detail endpoint returns, for one canonical, exactly the row set the test seeded for that canonical — compared as sets of `(source, external_source_id)`, not by count. Assert the seeded set is non-empty first, so the comparison cannot pass on two empty sets.
- [ ] Tenant isolation: seed two tenants; assert each tenant's returned canonical-id set is non-empty before asserting the two sets are disjoint, and assert neither report references a row whose `tenant_id` differs from the requested one.
- [ ] `.venv/bin/python -m pytest tests/test_ar_reconciliation.py -x --tb=short` exits 0 **and** reports a collected test count greater than zero (bare `pytest` is not on PATH and `.venv/bin/pytest` fails collection on `from core...` imports — CLAUDE.md). A run that collects nothing is a failure, not a pass.
- [ ] Full suite `.venv/bin/python -m pytest tests/ -x --tb=short` stays green with a collected count no lower than the count observed on the pre-build commit, both measured at build time.

---

## Dependencies

**Satisfied in tree:**

- [x] Canonical schema (feature 2) — `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references` in `db/schema_sqlite.sql`.
- [x] Both connectors (5, 6) — `QuickBooksConnector` (category `accounting`) and `RUDDRConnector` (category `psa`) implement `ConnectorInterface`. Their `read_transactions` output reaches the `transactions` table through feature 12a.
- [x] Transactions table + tolerance constants (feature 8b) — the `transactions` DDL and `AMOUNT_TOLERANCE_PCT` / `AMOUNT_TOLERANCE_CAP`.
- [x] Resolution / graph update (feature 10) — `core/graph/resolution.py` populates the canonical graph this feature reads.
**Shipped since this brief was first written:**

- [x] **Matcher orchestrator (feature 12)** — the batch entry point that resolves connector entities into the graph so that `canonical_entities` and `system_references` contain resolved clients. Shipped. This feature's tests still seed the graph directly.
- [x] **Overview + Entity Browser (feature 14)** — the entity-detail route this page deep-links into. Shipped; the entity deep-link target on the entities router is live.
- [x] **Dash application shell (feature 16)** — `dashboard/app.py` exposing a pages-enabled `dash.Dash` app whose sidebar is built by iterating the page registry. Shipped. **Feature 15 does not create or edit this file.** The page is served for real at test time, so `layout` must build with no database access.

**Hard prerequisite — must land before this feature:**

- [ ] **Transaction ingestion (feature 12a)** — persists connector `read_transactions()` output into `transactions`, deriving `period` from `txn_date` at insert (8b's rule), setting `counterparty_source_id`, and setting `canonical_id` where resolution is known; adds transaction fixtures and lights up signal B3. Brief: `features/pipeline/transaction-ingestion.md`. Feature 15 reads the rows 12a writes and cannot report real numbers without it.

---

## Estimated Complexity

**Rating:** M

**Rationale:** Reconciliation logic is aggregation + comparison — not algorithmically complex. The cross-category join (PSA labor matched to accounting invoices via `canonical_id`, with the `(source, counterparty_source_id)` → `system_references` fallback for unresolved rows) is the novel part, and the canonical entity graph makes it a tractable SQL join. Dashboard work is moderate: a DataTable, expandable detail rows, and a pure classification helper. The rating stays M only because the money substrate already exists; if this feature also had to build transaction ingestion it would be L.

---

## PROJECT CONTEXT

### The Cross-Category Query This Enables

Shape only — the runtime dialect is SQLite, and `system_references` carries no `tenant_id` of its own, so tenant scope is applied on `canonical_entities` and `transactions`.

```sql
SELECT
  ce.canonical_id,
  ce.canonical_name,
  SUM(CASE WHEN t.category = 'psa'        THEN t.amount ELSE 0 END) AS labor_total,
  SUM(CASE WHEN t.category = 'accounting' THEN t.amount ELSE 0 END) AS invoiced_total,
  SUM(CASE WHEN t.category = 'psa'        THEN t.amount ELSE 0 END)
    - SUM(CASE WHEN t.category = 'accounting' THEN t.amount ELSE 0 END) AS variance
FROM canonical_entities ce
JOIN transactions t
  ON t.canonical_id = ce.canonical_id
 -- or, for rows not yet carrying canonical_id, via
 -- system_references sr ON (sr.source, sr.external_id) = (t.source, t.counterparty_source_id)
WHERE ce.entity_type = 'client'
  AND ce.tenant_id = :tenant_id
  AND t.tenant_id  = :tenant_id
GROUP BY ce.canonical_id
```

This query is impossible without the canonical entity graph. It's the product thesis in SQL.

### Relevant Spec Sections

- Section 7: FP&A Feature Roadmap — Phase 2 (AR/AP Reconciliation)
- Section 14: Product UI — AR Reconciliation description
- Section 10: Agent 4 — Reconciliation Agent (cross-category reconciliation)

### Open Question for the Human — DECIDED

**DECIDED: feature 15 builds on real ingested rows supplied by feature 12a.** A separate ingestion feature (12a, `features/pipeline/transaction-ingestion.md`) is queued ahead of this one and owns persisting connector `read_transactions()` output plus transaction fixtures. Feature 15 stays at M, adds no writer, and reports over the rows 12a ingests.
