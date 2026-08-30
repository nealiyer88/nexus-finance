# Feature Brief: Overview Dashboard + Entity Registry Browser

**Author:** Neal Iyer
**Date:** 2026-05-10 (reality-corrected 2026-08-22)
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 14

---

## Problem Statement

The matching engine resolves entities but there's no way to see the results. The Controller needs two views: (1) an overview with KPI cards showing system health (entities resolved, auto-match rate, pending approvals, cross-category coverage), and (2) an entity registry browser showing every canonical entity with its cross-category aliases and graph relationships. Without these, the product is a black box.

**Starting state.** `dashboard/pages/overview.py` and `dashboard/pages/entity_graph.py` exist, but each is a `dash.register_page` placeholder whose body is an `html.Div` with an `H1` and a TODO comment — no layout logic, no callbacks, no data access. `overview.py` registers at path `/` (not `/overview`); `entity_graph.py` registers at `/entity-graph`. `api/routers/entities.py` is a docstring-only "Not yet implemented" stub. `api/main.py` already registers the connectors and approvals routers, so registering an entities router is an append beside them. `dashboard/app.py` exists (feature 16) and builds the sidebar by iterating the page registry; this feature must not edit it (see Constraints). The work below is fill-in and wiring, not creation.

Per `.claude/rules/01-nexus-finance-v1.md` §0, anything not marked `[BUILT]` must be treated as NOT EXISTING. §10 marks tenant RLS and auth `[PLANNED]`; this brief is written against that.

---

## Scope

### In Scope

- Fill in `dashboard/pages/overview.py` (keep its existing `dash.register_page(__name__, path="/", name="Overview")` call and path unchanged):
  - One KPI card per entry in the KPI Definitions table below, driven from a module-level mapping so the card set and the metric set cannot drift
  - KPIs queried from the canonical entity store; the Pending Approvals card reads the shipped pending store (see Constraints)
  - Refresh on page load

- Fill in `dashboard/pages/entity_graph.py` (keep its existing `dash.register_page(__name__, path="/entity-graph", name="Entity Graph")` call and path unchanged) — Entity Registry Browser:
  - Searchable table of canonical entities: `canonical_id`, `canonical_name`, `entity_type`, `entity_category`, `confidence`, alias count, connected source categories
  - Expandable detail row per entity: aliases grouped by **`entity_aliases.category`** (the source-system category — `accounting` / `psa`), system references. **Last transaction date is DEFERRED**: nothing in the tree writes the `transactions` table or `entity_edges.last_transaction`, so both are unpopulatable until a transaction-ingestion feature exists. Do not add a last-transaction column.
  - Cross-category relationship visualization over `entity_edges` (`source_node`, `target_node`, `relationship`, `source_category`, `target_category`, `weight`). **Use Plotly** — `plotly` is pinned in `requirements.txt`; `dash-cytoscape` is **not** installed and is not in `requirements.txt`. Adding it would be a new dependency and is out of scope.
  - Filter by `entity_type`, `entity_category`, confidence range, source category
  - Search by name (fuzzy — RapidFuzz, pinned in `requirements.txt`, over `canonical_name` and alias values)

- **Read helpers**: `core/graph/entity_store.py` exposes only per-canonical point lookups (`get_aliases` returns alias *values* with no category; `get_system_refs` returns `(source, external_id)` pairs and takes no `tenant_id`). There is **no list-all, no aggregate, and no pagination read** in the shipped store. This feature adds the list/aggregate/grouped-alias reads it needs, following the existing store conventions (module-level `def`, `conn: sqlite3.Connection` first arg, `tenant_id: Optional[str] = None` last). Every new read takes `conn` first and an optional `tenant_id` last. Router-level callers obtain their SQLite connection **exclusively** via `core.matching.pending_store.get_connection` — never `sqlite3.connect` inside `api/`.

- **API endpoints** — fill in the existing `api/routers/entities.py` stub:
  - `GET /entities/stats` — KPI values for the requested tenant
  - `GET /entities/` — paginated entity list with filters
  - `GET /entities/{canonical_id}` — entity detail with aliases, references, edges
  - **Register the router in `api/main.py`** — add the entities router beside the two existing registrations; without this the endpoints are unreachable.

- **Test suite:** `tests/test_entity_api.py`
  - Assert: each stats field is recomputed independently in the test from rows the test itself inserted, and matches the endpoint's value
  - Assert: reads are tenant-scoped per the shipped convention (below)
  - Assert: entity detail groups aliases by every distinct `entity_aliases.category` present for that canonical

### Out of Scope

- The Dash application shell — feature 16 owns it (see Constraints). This feature must not create or edit `dashboard/app.py`.
- Persisting the approval queue — feature 11's problem.
- AR reconciliation view — separate feature
- Real-time WebSocket updates — V1 refreshes on page load
- Entity merge UI (manually merge two canonical entities) — V2
- Export to CSV/Excel from browser — V2

---

## Tenant Scoping (match the shipped convention — do not invent one)

There is **no row-level security in this repo**. No `CREATE POLICY` / `ROW LEVEL SECURITY` exists in any schema file. The shipped convention in `core/graph/entity_store.py` and `core/graph/resolution.py` is:

- Every read takes `tenant_id: Optional[str] = None`. When `None`, **no WHERE filter is applied** (V1 single-tenant SQLite default; `canonical_entities.tenant_id` is nullable and fixtures load it NULL). When set, the query filters on `canonical_entities.tenant_id`, joining child tables (`entity_aliases`, `system_references`) back to the canonical row — the child tables carry no `tenant_id` column of their own.
- Writes call `_assert_tenant_scope(conn, canonical_id, tenant_id)`, which raises `ValueError` when the parent canonical is not visible under that tenant and is a no-op when `tenant_id` is `None`.
- **On the HTTP path**, routers read `request.state.tenant_id`, stamped by the shipped `TenantMiddleware`, exactly as the shipped approvals and connectors routers do. It is a UUID and is **never `None`** — the `tenant_id=None` no-filter branch above is a store-layer affordance for direct-call tests only. API tests must therefore either send the tenant header or seed under the middleware's default tenant constant — **imported** from the middleware module, never typed as a bare UUID literal, because a shipped test forbids bare UUID literals anywhere in `api/`, `dashboard/` or `tests/`. Second-tenant fixtures must be generated at runtime with `uuid.uuid4()`.

Every read this feature adds follows that shape. The testable invariant is: **given rows seeded under two distinct `tenant_id` values, a read passing one tenant returns only that tenant's rows and none of the other's, and the two result sets are disjoint** — derived from what the test seeded, never from a written-in number. Each of the two result sets must be asserted non-empty *before* the disjointness assertion; as stated without that precondition it passes vacuously when both are empty. Do not write a criterion asserting "enforces RLS"; there is nothing to enforce.

---

## Constraints (shipped surfaces this feature builds against and must not break)

1. **Dash application shell (`dashboard/app.py`) is SHIPPED and out of bounds.** Do not create or edit it.
   - A page registers itself **at module import** by calling `dash.register_page(__name__, path=..., name=...)` and exposing a module-level `layout`. Both target modules already do this; keep the exact call shape — a shipped test matches `path=` as the first keyword after `__name__`.
   - The module-level `layout` must build with **no database**: no connection, no query, no store file touched at import time. All data access lives inside callback bodies, following the shipped approval-queue page.
   - A shipped live test GETs every registered page path and asserts 200; a layout that touches a database at import will fail it.
   - The shell picks up every module in `dashboard/pages/` automatically and renders navigation by **iterating `dash.page_registry`** — no registration list is edited to add a page, and `name=` is the sidebar label.
   - Nav-badge slots for the overview and entity-graph paths are reserved by the shell and are attachable from a page callback without editing the shell.

2. **Pending Approvals is LIVE (feature 11 shipped).** Read the count of pending-status rows in tenant scope through `core.matching.pending_store`. It is **not** a denominator for auto-match rate: it holds human-routed items only.

---

## Success Criteria

- [ ] `dashboard/pages/overview.py` imports and its `layout` contains exactly one KPI card per key of the module's KPI mapping — assert the mapping is non-empty, then assert `set(card_ids_found_in_layout) == set(KPI_MAPPING)`, both derived at test time. No card count is written into the test.
- [ ] Each KPI value returned by `GET /entities/stats` equals the same quantity recomputed in the test from the rows the test seeded — not compared against any literal.
- [ ] `dashboard/pages/entity_graph.py` imports and its `layout` contains a table component and a search input; the table's column ids equal the explicitly named expected set `{canonical_id, canonical_name, entity_type, entity_category, confidence, alias_count, source_categories}` — named in the test, never read back from the module's own declaration.
- [ ] The shipped app's test client returns **200** for the overview path and the entity-graph path with **no database present**.
- [ ] Entity detail groups aliases by `entity_aliases.category`: for a seeded entity, the returned grouping's key set equals `SELECT DISTINCT category FROM entity_aliases WHERE canonical_id = ?` and each group's members equal that category's alias values.
- [ ] Cross-category coverage = (entities whose aliases span ≥2 distinct `entity_aliases.category` values) / (all canonical entities in scope). Assert the endpoint's numerator and denominator each match the same aggregate recomputed in the test over seeded rows.
- [ ] Entity search returns results via RapidFuzz over `canonical_name` and alias values: for a seeded name, a deliberately misspelled query returns that entity, and a query sharing no tokens with any seeded row returns an empty result.
- [ ] Tenant-scoped reads satisfy the disjointness invariant stated above.
- [ ] Every registered route is reachable: after registering the router, `GET /entities/stats`, `GET /entities/`, and a `GET /entities/{canonical_id}` for a seeded id each return status **200** via `fastapi.testclient.TestClient`, constructed as `TestClient(app)` and used **without** the `with` context manager — the convention the shipped approvals-API tests use, so the app's Postgres `startup` hook never runs.
- [ ] `.venv/bin/python -m pytest tests/test_entity_api.py -x --tb=short` exits 0 **and reports a collected test count greater than zero** — a bare exit code is not sufficient, because a collection error or an empty file can pass silently. Bare `pytest` is not on PATH and `.venv/bin/pytest` breaks `from core...` imports; the `python -m` form is required (CLAUDE.md, `rocket.config.sh` `TEST_CMD`).

---

## Dependencies

- [ ] Resolution + Graph Update (feature 10) — **SHIPPED.** `core/graph/resolution.py` and the write path through `core/graph/entity_store.py` are real.
- [ ] Canonical schema (feature 2) — **SHIPPED.** The only runtime schema is `db/schema_sqlite.sql`; queries run against `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references`. Grep the DDL for the current column list — do not assume the rules-file §3/§4 literals are the DDL (e.g. `entity_edges` has `last_transaction` and `approval_count` but no `approved_at`, and `canonical_entities.match_pattern` / `match_signals` exist as columns but are **never written by any shipped code path**).
- [ ] Approval queue (feature 11) — **SHIPPED.** Only the Pending Approvals KPI depends on it; see Constraints.
- [ ] Dash application shell (feature 16) — **SHIPPED.** See Constraints; this feature must not edit it.

---

## Open Questions (human decision required)

1. **Auto-Match Rate has no stored source.** Nothing in the tree records whether a resolution was auto-approved: `match_pattern` and `match_signals` are never written, and there is no resolution-event table with a timestamp and a disposition. `canonical_entities` carries only `confidence` and `created_at`. Either the metric is proxied by `confidence >= AUTO_APPROVE_THRESHOLD` over rows created in the window (an approximation, since a human-approved high-confidence match is indistinguishable from an auto-approved one), or a resolution-event record must be persisted — new scope, and arguably feature 10's or 12's.
2. **The "Entities Resolved" KPI field cannot use its obvious name.** A shipped guard test forbids the literal string `entity` + `_count` anywhere in `api/`, `dashboard/` or `tests/`. Pick a different field/card id for that KPI (e.g. `resolved_entities`) and use it consistently across the endpoint payload, the card id, and the tests.

---

## Estimated Complexity

**Rating:** M

**Rationale:** Three API endpoints in a stub router plus one added router registration in `api/main.py` (straightforward). Two Dash pages filled in from placeholders with DataTables and expandable rows (moderate). One Plotly network visualization (moderate). New list/aggregate reads in `entity_store.py`, since the shipped store is point-lookup only (moderate). The entity browser's fuzzy search across aliases is the trickiest UI element. Every dependency is shipped, so the render path is exercisable end-to-end; only Auto-Match Rate lacks a source (Open Question 1).

---

## PROJECT CONTEXT

### Dashboard Navigation (from spec Section 14)

```
Sidebar                          ◄── shell owned by feature 16 (dashboard/app.py), NOT this feature
├── Overview ◄── THIS FEATURE (registers at "/")
├── Entity Graph ◄── THIS FEATURE (registers at "/entity-graph")
├── Approval Queue (feature 11)
├── Modules
│   └── AR Reconciliation (feature 15)
└── System
    ├── Connectors (feature 16)
    └── Audit Log (feature 16)
```

This tree is the spec's *intent*, not an assertion about the registry's contents. The shell renders links by iterating `dash.page_registry`; pages other features add appear too, and that is not a failure. No test asserts the registry's size.

### KPI Definitions

| Metric | Calculation | Source status |
|--------|------------|---------------|
| Entities Resolved | COUNT(canonical_entities) in tenant scope | Available |
| Auto-Match Rate | auto-approved / total resolved over a trailing window | **No source — see Open Question 1** |
| Pending Approvals | COUNT(pending items) in tenant scope | Available — pending-status rows via `core.matching.pending_store` |
| Cross-Category Coverage | entities whose aliases span ≥2 distinct `entity_aliases.category` values / COUNT(canonical_entities) | Available |

Note the two distinct "category" fields: `canonical_entities.entity_category` is `organization` \| `person`, while `entity_aliases.category` / `system_references.category` / `entity_edges.source_category` are the source-system category (`accounting` \| `psa`). Cross-category coverage and the alias grouping use the **latter**; the `entity_category` filter uses the **former**.

### Relevant Spec Sections

- Section 14: Product UI — V1 Feature Set (overview metric cards, entity detail view)
- Section 4: V1 Product Definition (configuration UI)
</content>
</invoke>
