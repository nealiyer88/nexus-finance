# Feature Brief: AR Reconciliation Module

**Author:** Neal Iyer
**Date:** 2026-05-10 (audited and repaired 2026-08-22 against the shipped tree)
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 2 (AR/AP Recon)
**Feature #:** 15 (depends on 12 and 14; see Forward Dependencies for the contracts it also needs)

---

## Problem Statement

The first FP&A feature built on the canonical entity graph. For each resolved client entity, cross-reference RUDDR labor against QB invoiced revenue. "RUDDR shows 450 hrs × $200/hr = $90K for Cenlar FSB. QB shows $65K invoiced. $25K unbilled." This query is impossible without cross-category entity resolution — RUDDR knows "cenlar-fsb" and QB knows "Cenlar, LLC." but neither knows they're the same client. The canonical entity graph does.

---

## Starting State (verified in tree, 2026-08-22)

Read this before assuming anything below is a create-from-nothing.

- `api/routers/reconciliation.py` **exists** as a docstring-only "Not yet implemented" stub with no executable body. `api/main.py` is a bare FastAPI app exposing `/health` and **registers no routers at all** — wiring this router into `api/main.py` is part of this feature's work and is named in scope below.
- `dashboard/pages/ar_reconciliation.py` **exists** as a `dash.register_page(__name__, path="/ar-reconciliation", name="AR Reconciliation")` placeholder whose body is an `html.H1` plus a TODO comment. There is **no Dash application object anywhere in the tree** — the page registers into an app that does not exist. Feature 16 owns `dashboard/app.py`; see Forward Dependencies.
- `core/reconciliation/` does not exist. Net-new package.
- **The money data now has a real home.** Feature 8b shipped a `transactions` table in `db/schema_sqlite.sql` (SQLite is the only engine that is created at runtime, per rules §0) with columns `txn_id, tenant_id, source, category, external_source_id, txn_type, amount, currency, txn_date, period, counterparty_source_id, canonical_id, created_at`, `UNIQUE (tenant_id, source, external_source_id)`, and indices on `(tenant_id, source, counterparty_source_id, period)` and `(tenant_id, canonical_id, period)`. This feature **reads that table; it does not define a second money store.**
- **Nothing writes transaction rows yet.** 8b states plainly that B3 is dark outside tests: QuickBooks `read_transactions` returns `[]` in fixture mode, RUDDR `read_transactions` returns `[]` in fixture mode (V1 fixtures are entities-only), and no module persists `NormalizedTransaction` to `transactions`. See Forward Dependencies and the Open Question at the end — this is the load-bearing gap in this feature.
- Amount tolerance is **already a shipped constant pair**: `AMOUNT_TOLERANCE_PCT` and `AMOUNT_TOLERANCE_CAP` in `core/graph/entity_store.py`, encoding rules §5's `min(amount × pct, cap)`. This feature **imports them**; it must not re-declare a literal tolerance.
- Tenant scoping in this repo is an explicit `tenant_id: Optional[str] = None` function parameter carried into a SQL predicate (the idiom throughout `core/graph/entity_store.py`). `canonical_entities` and `transactions` carry a `tenant_id` column; `system_references`, `entity_aliases` and `entity_edges` do **not** — they are scoped by joining to `canonical_entities`. There is no row-level security anywhere: no `CREATE POLICY`, no `current_setting`, no session context. Rules §10 marks RLS `[PLANNED]`.

---

## Scope

### In Scope

- Fill in `api/routers/reconciliation.py` (stub exists) and register it on `api/main.py`:
  - `GET /reconciliation/ar` — AR reconciliation report for tenant, showing per-client: RUDDR labor total, QB invoiced total, variance, status (matched | unbilled | overbilled)
  - `GET /reconciliation/ar/{canonical_id}` — per-entity detail: line-item breakdown of RUDDR transactions vs QB invoices
  - Both take `tenant_id` and thread it into every query as an explicit predicate — the shipped convention, not RLS.

- Fill in `dashboard/pages/ar_reconciliation.py` (placeholder exists; keep its registered path unchanged):
  - Summary table: canonical client name, RUDDR labor $, QB invoiced $, variance $, variance %, status flag
  - Color-coded status: green (matched within tolerance), amber (unbilled beyond tolerance), red (variance beyond the variance-% band)
  - Expandable detail: RUDDR rows on left, QB rows on right, joined by `canonical_id`
  - **Structure contract** (so the page is testable without a browser): `layout` is a zero-argument callable; the summary is a `dash_table.DataTable` with `id="ar-recon-table"`; and a pure `classify_rows(rows: list[dict]) -> list[dict]` helper performs status assignment. Tests exercise the helper directly on fixture rows.
  - Amount tolerance is imported from `core.graph.entity_store` (`AMOUNT_TOLERANCE_PCT`, `AMOUNT_TOLERANCE_CAP`), never re-literalled here.

- Reconciliation logic in `core/reconciliation/ar.py` (new package):
  - Read side is `transactions`, tenant-scoped, in `entity_store`'s style: a `sqlite3.Connection` plus `tenant_id: Optional[str] = None`.
  - PSA-side labor total = SUM of `amount` over rows with `category = 'psa'`; accounting-side invoiced total = SUM of `amount` over rows with `category = 'accounting'` and the invoice `txn_type`. Rows attach to a client either directly via `transactions.canonical_id`, or via `(source, counterparty_source_id)` joined to `system_references (source, external_id)` for rows whose `canonical_id` is still null — the same dual join key 8b established, and for the same reason: the source side is frequently unresolved.
  - **Money arrives as `amount` already.** Do not recompute `hours × billing_rate` in this feature — `NormalizedTransaction` carries `amount`, and hours/rate live only in connector fixtures as entity attributes, not as transaction rows. Rate arithmetic belongs to whichever feature writes `transactions` rows.
  - Calculate variance and classify: MATCHED, UNBILLED, OVERBILLED.
  - Handle partial coverage: PSA rows exist for a client with no accounting-side counterpart in the same period.

- **Test suite:** `tests/test_ar_reconciliation.py`
  - Seed a SQLite graph from the schema plus fixtures, then seed `transactions` rows directly (there is no ingestion writer to seed through — see Forward Dependencies).
  - Assert: variance for a seeded client equals the difference of the two sums the test itself computed from the rows it inserted.
  - Assert: unbilled labor flagged when PSA rows exist for a canonical with no accounting-side rows.
  - Assert: tolerance boundary behaves as `min(amount × AMOUNT_TOLERANCE_PCT, AMOUNT_TOLERANCE_CAP)` — assert against the imported constants, not against typed-in numbers.
  - Assert: a second tenant's rows never appear in the first tenant's report.

### Out of Scope

- AP reconciliation — V2 with Bill.com connector
- Cash flow forecasting — Phase 3
- Write-back of reconciliation results to QB or RUDDR — Shadow Ledger only
- Historical trend analysis — V2
- **Transaction ingestion** — persisting connector `read_transactions()` output into the `transactions` table is not this feature's work. See the Open Question.
- The Dash application shell — feature 16 owns `dashboard/app.py`.

---

## Success Criteria

- [ ] `core/reconciliation/ar.py` exists and exposes the aggregation entry point; every query it issues carries a `tenant_id` predicate when `tenant_id` is not None.
- [ ] `api/routers/reconciliation.py` exposes both routes and is registered on `api/main.py`: assert both paths appear in `{r.path for r in api.main.app.routes}`.
- [ ] `dashboard/pages/ar_reconciliation.py` still registers at the path its own source declares (derive both sides at runtime; compare the registered path to the `path=` argument parsed from the module source — no hardcoded expectation), `layout` is callable, and its tree contains a `dash_table.DataTable` with `id == "ar-recon-table"`.
- [ ] Per-client variance equals PSA-side total minus accounting-side total, where **both sides are recomputed by the test from the rows it seeded** — no expected total is written into the brief or the test as a literal.
- [ ] Tolerance: for a seeded pair whose absolute difference is strictly below `min(max(|a|,|b|) * AMOUNT_TOLERANCE_PCT, AMOUNT_TOLERANCE_CAP)` the status is MATCHED, and for a pair strictly above it is not MATCHED. Both bounds computed in the test from the imported constants.
- [ ] Unbilled flagged: a canonical with seeded PSA rows and zero accounting-side rows classifies UNBILLED.
- [ ] Detail endpoint returns, for one canonical, exactly the row set the test seeded for that canonical — compared as sets of `(source, external_source_id)`, not by count.
- [ ] Tenant isolation: seed two tenants; assert the canonical ids returned for tenant A and tenant B are disjoint, and that neither report references a row whose `tenant_id` differs from the requested one.
- [ ] `.venv/bin/python -m pytest tests/test_ar_reconciliation.py -x --tb=short` exits 0 **and** reports a collected test count greater than zero (bare `pytest` is not on PATH and `.venv/bin/pytest` fails collection on `from core...` imports — CLAUDE.md). A run that collects nothing is a failure, not a pass.
- [ ] Full suite `.venv/bin/python -m pytest tests/ -x --tb=short` stays green with a collected count no lower than the count observed on the pre-build commit, both measured at build time.

---

## Dependencies

**Satisfied in tree:**

- [x] Canonical schema (feature 2) — `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references` in `db/schema_sqlite.sql`.
- [x] Both connectors (5, 6) — `QuickBooksConnector` (category `accounting`) and `RUDDRConnector` (category `psa`) implement `ConnectorInterface`. Note the caveat: both return `[]` from `read_transactions` in fixture mode.
- [x] Transactions table + tolerance constants (feature 8b) — the `transactions` DDL and `AMOUNT_TOLERANCE_PCT` / `AMOUNT_TOLERANCE_CAP`.
- [x] Resolution / graph update (feature 10) — `core/graph/resolution.py` populates the canonical graph this feature reads.

**Forward dependencies — named by needed contract, not assumed present:**

- [ ] **Matcher orchestrator (feature 12)** — needed contract: a batch ingestion entry point that resolves connector entities into the graph so that `canonical_entities` and `system_references` contain resolved clients. Until it lands, this feature's tests seed the graph directly. Feature 12 is not built.
- [ ] **Overview + Entity Browser (feature 14)** — needed contract: the entity-detail route/identifier this page deep-links into. If 14 has not landed, the AR page renders standalone and links nowhere; that is an acceptable degraded state, not a build blocker. 14 does **not** provide a "dashboard framework" — that claim in the original brief was false.
- [ ] **Dash application shell (feature 16)** — needed contract: a `dashboard/app.py` exposing a `dash.Dash(use_pages=True)` `app` whose sidebar is built by iterating `dash.page_registry`, so that any page module present at build time is picked up unmodified. **Feature 15 does not create this file and must not.** Consequence: until 16 lands, "renders" for this page means the module imports, `layout` is callable, and the structure contract above holds — there is no application to serve it. Note the queue orders 16 after 15; see the Open Question.
- [ ] **A transactions writer** — needed contract: something that persists connector `read_transactions()` output into `transactions`, deriving `period` from `txn_date` at insert (8b's rule), setting `counterparty_source_id`, and setting `canonical_id` where resolution is known. **No feature currently owns this.** Without it, this feature is correct but dark against real data — exactly the way B3 is dark today.

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

### Open Question for the Human

**Nothing populates `transactions`, and no feature owns doing so.** This feature can be built and fully tested against seeded rows, but it computes zero for every client against the real fixture path, because both connectors return `[]` from `read_transactions` in fixture mode and no persistence layer exists between `NormalizedTransaction` and the table. Decide before build: (a) ship AR reconciliation against seeded rows and accept it is dark until an ingestion feature lands, (b) widen this feature to include a transactions writer and raise it to L, or (c) queue a separate ingestion feature ahead of this one.
