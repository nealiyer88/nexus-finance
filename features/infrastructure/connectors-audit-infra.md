# Feature Brief: Connectors Page + Audit Log + System Infrastructure

**Author:** Neal Iyer
**Date:** 2026-05-10 (reality-corrected 2026-08-22; re-verified 2026-08-22 after features 8b, 10 shipped and 10a was rewritten)
**Status:** Approved
**Complexity:** L
**FP&A Phase:** Infrastructure
**Feature #:** 16

---

## Problem Statement

The product needs system management surfaces: a connectors page showing connected systems grouped by category with sync status, and an audit log showing every action the system has taken. The audit log is a compliance requirement — every resolution, every approval, every sync must be traceable. The connectors page is the GTM entry point — the first thing a new customer interacts with after signup.

**Starting state (re-verified in tree 2026-08-22).** Nothing this feature touches exists yet. `dashboard/pages/connectors.py` and `dashboard/pages/audit_log.py` are `dash.register_page` placeholders whose body is an empty `html.Div` plus a TODO — no layout logic of any kind. `api/routers/connectors.py`, `api/middleware/audit.py` and `api/middleware/tenant.py` are "Not yet implemented" docstring stubs with no executable body. `api/main.py` is a bare FastAPI app exposing `/health`; the load-bearing fact is that *the connectors router* is not registered and neither middleware is installed — other features may add routes before this one lands, so derive the router set at build time rather than asserting what is absent. There is **no Dash application object anywhere in the tree** — the page files register against an application that does not exist, so nothing renders them. Per `.claude/rules/01-nexus-finance-v1.md` §0, every item not marked `[BUILT]` must be treated as NOT EXISTING; §10 marks tenant RLS `[PLANNED]`, the audit log `[PARTIAL]` (Postgres DDL only, stub writer), and auth `[PLANNED]`. This brief is written against that reality: the work below is **creation**, not extension.

**What has landed since the previous revision, and what it changes here.** Feature 8b shipped a `transactions` table (present in both the Postgres and SQLite schemas, and added to `tests/test_schema_parity.py`'s shared-table list) and the B3 amount signal. Feature 10 shipped `core/graph/resolution.py` and `core/matching/training_data.py`, and Stage 6 now genuinely writes `system_references` rows — **into SQLite only**. Neither changes this feature's shape, but both invalidate the old phrasing of Owner Decision 5, which is corrected below: the reason `entity_count` is deferred is no longer "nothing populates that table yet" — something does — it is that the populated copy is in the wrong engine and `api/` has no read path to it.

---

## Relationship to feature 10a (read before building)

Feature 10a (`features/infrastructure/postgres-store-bootstrap.md`) stands up the Postgres path and **owns** the following outright. This feature consumes every one of them and duplicates none:

| Owned by 10a | This feature's relationship |
|---|---|
| `psycopg[binary]` pin, `DATABASE_URL`, `core/graph/pg.py` (`get_dsn`/`connect`/`is_available`) | imports and calls; adds no second driver, no second connection helper, no pooling |
| `db/migrations/postgres.manifest` + `scripts/migrate_pg.py` (manifest-driven runner) | **appends one filename** to the manifest; builds no runner and applies nothing itself |
| `pytest.ini` with `--strict-markers` and the registered `integration` marker | uses the marker; **does not create or edit `pytest.ini`** |
| `tests/conftest.py` and its `pg_conn` fixture (skips when Postgres is unavailable, rolls back at teardown) | uses the fixture for every Postgres-touching test; writes no second fixture that opens its own connection |
| `tenants` provisioning: `BOOTSTRAP_TENANT_ID`, the bootstrap migration that seeds the row, `core/graph/tenants.py::resolve_or_create_tenant` | **reads and asserts** the row; writes no `INSERT INTO tenants`, calls no create-or-resolve helper, ships no fixture that creates the row as a side effect |
| `core/graph/audit.py::log_resolution` (Stage 6 resolution audit writer) | **separate writer, same table.** 16's middleware writes API-action rows; it does not call, wrap, or modify 10a's resolution writer |

**Audit-row namespacing, so 10a's reconciliation stays sound.** 10a's `scripts/reconcile_stores.py` asserts one-directional coverage — every canonical identifier mutated by Stage 6 has at least one `audit_log` row — and treats audit rows with no matching graph mutation as informational. Every row this feature writes sets `resource = "connectors"` and a `resource_id` that is a **provider name, never a canonical identifier**, so 16's rows land in that benign informational bucket and can be excluded by a `resource` filter. The build must not widen `resource_id` usage beyond provider names, and must not reuse a resource value 10a writes.

---

## Scope

### In Scope

- **Create the minimal Dash application shell** (`dashboard/app.py`, NEW — does not exist today). Without it none of the render criteria below are verifiable, because `dash.register_page` calls in `dashboard/pages/*.py` have no application to register into. **Feature 11 (approval queue) depends on this shell existing and on the contract below**, so the contract is stated as a contract, not as a sketch:
  - `dashboard/app.py` constructs `dash.Dash(__name__, use_pages=True, pages_folder="pages")` and binds it to a module-level name `app`. `dashboard.app.app` and `dashboard.app.app.server` (the underlying Flask WSGI object) are the two public handles downstream features may import.
  - `dashboard/__init__.py` is created if absent, so `import dashboard.app` does not depend on implicit-namespace-package resolution.
  - The top-level layout is a sidebar plus `dash.page_container`. The sidebar is **built by a module-level pure function `build_sidebar(registry) -> Component`** that takes the page registry as an argument (so it is callable in tests with a fixture dict) and is invoked with `dash.page_registry` in the real layout. It **iterates the registry** — it never hardcodes an entry — so pages added by any feature landing before or after this one appear without editing the shell.
  - **Downstream-stable ids (feature 11 relies on these).** The sidebar container carries `id="app-sidebar"`. Each link carries `id={"type": "nav-link", "path": <the registry entry's path>}`, and each link contains a badge slot `id={"type": "nav-badge", "path": <same path>}` rendered empty by this feature. A later feature (11's live pending-approvals count) attaches a callback targeting `{"type": "nav-badge", "path": "/approval-queue"}` **without editing `dashboard/app.py`**. This feature writes no callback and computes no badge value; it only guarantees the slot exists for every registered page.
  - A `if __name__ == "__main__": app.run(debug=True)` entrypoint.
  - **Explicitly NOT in this shell:** theming, CSS/assets, auth gating, callbacks, state stores, or any page-specific logic. **Every other module in `dashboard/pages/` — whatever set exists at build time, this feature does not enumerate or cap it** — is picked up automatically by `use_pages` and must keep rendering its current layout at its current path, unmodified.

- **Create `dashboard/pages/connectors.py`** (currently an empty-`Div` placeholder; replace its body):
  - Connected systems grouped by category (Accounting: QB ✓ | PSA: RUDDR ✓)
  - Per-connector status: connected, `category`, `last_sync` timestamp, and last-sync outcome (`last_sync_status` / `last_sync_error`, the two columns scoped in by Owner Decision 4). **No entity count** — see Owner Decision 5 and FOLLOW-UP 16-B; the page must not render an entity-count field at all rather than render a field that is always zero.
  - Connect / Disconnect buttons that link out to the OAuth entrypoints owned by feature 17 (this feature renders the controls; it does not implement the flow)
  - Manual sync trigger button per connector
  - Future connector slots shown as "Coming Soon" (Bill.com, Stripe, Gusto)
  - **Structure contract (so the render criteria are testable):** the module exposes `layout` as a **zero-argument callable** returning a component tree, plus a pure helper `build_category_groups(connectors: list[dict]) -> list[Component]` that returns exactly one group component per distinct `category`, each with `id={"type": "connector-category", "category": <category>}`. Each connector card carries `id={"type": "connector-card", "provider": <provider>}` and a sync button `id={"type": "connector-sync", "provider": <provider>}`. Tests call the helper with fixture dicts — no browser, no live database.

- **Create `dashboard/pages/audit_log.py`** (currently an empty-`Div` placeholder; replace its body):
  - Filterable table: timestamp, actor (`system` | user_id), action, resource type, resource_id, category, diff summary
  - Filters: date range, action type, resource type, category, actor
  - Append-only display — no edit or delete UI actions
  - Paginated, sorted by timestamp desc
  - **Structure contract:** the module exposes `layout` as a zero-argument callable, a `dash_table.DataTable` with `id="audit-log-table"`, one `dcc` filter control per filter with ids `audit-filter-date-range`, `audit-filter-action`, `audit-filter-resource`, `audit-filter-category`, `audit-filter-actor`, and a pure `apply_filters(rows: list[dict], **filters) -> list[dict]` helper that performs the filtering. Tests exercise `apply_filters` directly on fixture rows.

- **Create `api/routers/connectors.py`** (currently a docstring-only stub) and **register it in `api/main.py`**:
  - `GET /connectors/` — list connected systems for the active tenant. `200`, JSON list; each item has exactly the keys `provider`, `category`, `connected`, `last_sync`, `last_sync_status`, `last_sync_error`. **`entity_count` is deliberately absent** (Owner Decision 5).
  - `POST /connectors/{provider}/sync` — trigger manual sync. Returns `202` with body `{"provider": <provider>, "status": "accepted"}`, sets `connectors.last_sync = now()` and `last_sync_status = 'accepted'` for that `(tenant_id, provider)` row, and enqueues an `audit_log` row with `action = "connector.sync"`, `resource = "connectors"`, `resource_id = <provider>`. A provider with no row for the resolved tenant returns `404`.
  - `GET /connectors/{provider}/status` — `200` with `last_sync`, `last_sync_status`, `last_sync_error`; `404` for an unknown provider.
  - Neither shipped connector class exposes a `sync()` primitive — `ConnectorInterface` (locate the ABC by symbol in `connectors/base.py`) has `authenticate` / `read_entities` / ... and no sync entry point. A manual sync composes `authenticate()` + `read_entities()`; the endpoint is accepted-and-recorded, not synchronous ingestion.

- **Create the connector-sync-status migration** (NEW), named `<NNN>_connector_sync_status.sql` in `db/migrations/`, where `<NNN>` is the **next unused numeric prefix** at build time — do not hardcode a number; enumerate `db/migrations/*.sql`, take one above the highest prefix present, and take the next free one if a feature landing first has claimed it. Body: `ALTER TABLE connectors ADD COLUMN IF NOT EXISTS last_sync_status TEXT; ALTER TABLE connectors ADD COLUMN IF NOT EXISTS last_sync_error TEXT;`. Mirror both columns into `db/schema.sql`'s `connectors` DDL. **Append the filename to the end of `db/migrations/postgres.manifest`** (the file feature 10a creates; 10a's runner applies exactly the manifest, in manifest order, and ignores anything not listed) — authoring the file without listing it means it never runs. Do **not** author a `_sqlite` sibling: 10a's manifest is asserted to contain no `_sqlite` entry, and `connectors` has no SQLite mirror (Owner Decision 4). Authored here; **applied by feature 10a's migration runner** (this feature does not build a runner).

- **Create `api/middleware/audit.py`** (currently a docstring-only stub):
  - Middleware that logs every API action to the Postgres `audit_log` table over `core.graph.pg.connect()`
  - Fields: tenant_id, actor_id, action, resource, resource_id, category, diff (JSONB), timestamp. `actor_id` is NULL for every row this feature writes — consistent with 10a's rule that the column stays UUID-typed and the in-tree actor string is never coerced into it.
  - **Audit writes are BACKGROUND, never inline on the request path.** The middleware builds the row dict and hands it to a module-level `AuditQueue` (a `queue.Queue`) via `enqueue(row)`, then returns the response. A single daemon worker thread drains the queue and performs the INSERTs. The request handler performs **zero** database round trips for auditing. `enqueue` must never block or raise into the request: it uses `put_nowait` and, on `queue.Full`, drops the row and logs a warning.
  - **`AuditQueue` public surface, because the tests depend on it:** `enqueue(row)`, `drain()` (synchronously writes all pending rows and returns; the mechanism every test uses instead of sleeping on the worker thread), `failed_writes` (an integer counter), and `start_worker()` / `stop_worker()`. No test in this feature may assert timing or sleep for the worker.
  - Append-only — the writer issues INSERT only and must contain no UPDATE or DELETE against `audit_log`
  - **The worker must not mask a missing tenant.** In `db/schema.sql`, the `audit_log` table's `tenant_id` column is declared `UUID NOT NULL REFERENCES tenants(id)` (grep the `CREATE TABLE ... audit_log` block; do not cite a line number), so an INSERT with an unprovisioned tenant raises `ForeignKeyViolation`. That exception takes a **separate, loud** path from the `queue.Full` drop: logged at `ERROR` naming the offending tenant_id and pointing at feature 10a's provisioning step, and counted on `AuditQueue.failed_writes`. It is never folded into the generic warning-and-drop branch, and it never triggers a create-or-resolve call.

- **Create `api/middleware/tenant.py`** (currently a docstring-only stub) as a **single-tenant placeholder — NOT authentication, NOT isolation, NOT access control**:
  - Resolve a tenant identity for each request in this precedence order: the `X-Nexus-Tenant` request header if present, else the `NEXUS_TENANT_ID` environment variable, else the module constant `DEFAULT_TENANT_ID`. The resolved value is attached to `request.state.tenant_id` for downstream handlers and the audit writer.
  - **`DEFAULT_TENANT_ID` is a binding, not a value — see "The tenant-identifier seam" below.** The module's only definition of it is `from core.graph.pg import BOOTSTRAP_TENANT_ID` followed by `DEFAULT_TENANT_ID = BOOTSTRAP_TENANT_ID`. No UUID literal originating in this feature is acceptable anywhere in `api/`, `dashboard/`, or `tests/`.
  - **Startup precondition, `api/main.py`:** when `DATABASE_URL` is set, a startup hook calls `require_tenant_row(conn, tenant_id)` — `SELECT 1 FROM tenants WHERE id = %s` — and on zero rows raises `RuntimeError` naming the missing UUID and the 10a bootstrap step, aborting startup. It never INSERTs the row, never calls `core.graph.tenants.resolve_or_create_tenant`, never falls back to another tenant, and never downgrades to a warning or a skip. When `DATABASE_URL` is unset the hook is a no-op, so the shell and the pages remain importable and testable on a machine with no database.
  - The resolved tenant_id is **stamped onto writes and used as a query filter**, giving the data a correct shape for a future multi-tenant world. It provides **zero security guarantee**: the header is unauthenticated, any caller may set it to any value, any caller may read another tenant's connectors by guessing its UUID, and no request is ever rejected for supplying a tenant it does not own. It is a data-shaping convenience, and calling it isolation would be a false claim about the system's security posture.
  - The module docstring and the endpoint docs MUST say this in those terms. No code, test, comment, or success criterion in this feature may describe the result as tenant isolation, RLS, multi-tenancy, or access control.
  - No JWT parsing, no Supabase import, no `CREATE POLICY`, no session handling.

- **The tenant-identifier seam — enforced in one place plus two assertions, never by prose.** The seam is that 16's default tenant identifier and the tenant 10a provisions at bootstrap must be the same value. Three copies of that value could otherwise drift: 10a's Python constant, 10a's bootstrap SQL, and 16's resolver default. This feature closes it as follows and the build may not substitute a comment for any of the three:
  1. **Single source, imported not retyped.** `DEFAULT_TENANT_ID` is assigned directly from `core.graph.pg.BOOTSTRAP_TENANT_ID`. There is no second literal, no re-parse, no environment default baked into the code. `BOOTSTRAP_TENANT_ID` must be importable without a live database (10a defines it as a module-level literal), so this import is safe on the no-database contributor path.
  2. **Static assertion (no database).** A test imports both names and asserts `api.middleware.tenant.DEFAULT_TENANT_ID is core.graph.pg.BOOTSTRAP_TENANT_ID` — identity, not string equality, which is what distinguishes a re-export from a coincidentally-equal copy — and greps `api/`, `dashboard/` and `tests/` for any bare UUID-shaped literal, asserting none is found.
  3. **Live assertion (integration).** Against a migrated database, a test asserts `SELECT id FROM tenants WHERE id = %s` with the imported constant returns a row — i.e. the value 10a's *SQL* seeded and the value 10a's *Python* exports agree, checked from 16's side. This is the assertion that catches drift between 10a's two copies; it is the reason 16 checks the seam at all rather than trusting it.

  If any of the three fails, this feature is not shippable — the first request against a real database would FK-fail on an audit insert, and the failure would surface in a background thread rather than in the response.

- **Test suite:** `tests/test_connectors_api.py`, `tests/test_audit.py`, `tests/test_dashboard_shell.py`
  - Assert: `GET /connectors/` returns connectors filtered by the resolved tenant_id
  - Assert: the placeholder resolver honours header → env → `DEFAULT_TENANT_ID` precedence, and that an absent header yields the documented default rather than a rejection
  - Assert: audit rows created for every API action, carrying the resolved tenant_id
  - Assert: the audit writer performs INSERT only — grep-level assertion that no UPDATE/DELETE statement targets `audit_log`
  - Assert the Dash shell contract, including the badge slots feature 11 will target
  - Postgres-touching tests use `@pytest.mark.integration` and 10a's `pg_conn` fixture; they skip when `DATABASE_URL` is unset. Dash structure, filter-helper, and header-precedence tests require no database and are not integration-marked.
  - **The `integration` marker is registered by 10a's `pytest.ini` under `--strict-markers`.** This feature creates and edits no pytest configuration. Because an unregistered or non-matching marker deselects everything and still exits 0, **every criterion below that names an integration run asserts a collected count greater than zero parsed from the run's own output** — a bare exit code is never the assertion.

### Out of Scope

- **Authentication of any kind** — see Owner Decision 1. No login, no signup, no JWT issuance or verification, no Supabase client. Owned by feature 17.
- Real multi-tenant isolation, row-level security, `CREATE POLICY`, or cross-tenant rejection. None of it is achieved by this feature.
- **Anything 10a owns** — the driver pin, `core/graph/pg.py`, the migration runner, `db/migrations/postgres.manifest`'s creation, `pytest.ini`, `tests/conftest.py`, `tenants` provisioning, `core/graph/audit.py`. See the ownership table above.
- **Moving the graph store to Postgres**, or opening a SQLite connection from `api/`. `api/` gains exactly one engine in this feature.
- OAuth flow implementation (handled in QB/RUDDR connector features and feature 17)
- Webhook receiver endpoints
- Connector health monitoring / alerting (Sentry handles errors)
- Connector configuration UI (API keys, custom field mapping)
- Any Dash work beyond the minimal shell — no theming, no shared component library, no cross-page state, and **no callbacks**, including the approval-queue badge callback whose slot this feature reserves (feature 11 writes it).

---

## Owner Decisions (2026-08-22)

**1. No auth in feature 16; single-tenant placeholder instead.** This feature assumes a single tenant and uses the explicit placeholder identity described above (`X-Nexus-Tenant` header → `NEXUS_TENANT_ID` env → `DEFAULT_TENANT_ID` constant). The prior "Supabase Auth configured — JWT tokens for tenant extraction" dependency was false — `supabase` is pinned in `requirements.txt` but never imported, and `api/routers/auth.py` is a stub. Real login and JWT arrive with **feature 17 (signup-onboarding)**, whose brief already claims `POST /auth/login` and Supabase Auth.

> **FOLLOW-UP 16-A — retire the placeholder tenant (blocked on feature 17).** When 17 lands: replace the header/env/constant resolver in `api/middleware/tenant.py` with JWT claim extraction; make a missing or invalid token a rejection rather than a fallback to `DEFAULT_TENANT_ID`; delete `DEFAULT_TENANT_ID` and the `X-Nexus-Tenant` header path so no unauthenticated caller can assert a tenant; add the cross-tenant rejection tests that this feature deliberately does not write; and only then may any brief or doc describe the system as tenant-isolated.

**2. The minimal Dash shell is in scope, and feature 11 depends on it.** Feature 16 creates `dashboard/app.py` because without an application object its page-render criteria are unverifiable. Kept to registration plus navigation, nothing more — but the sidebar link and badge ids are treated as a published contract rather than an implementation detail, because feature 11's "live badge count in sidebar navigation" requirement has no other anchor and would otherwise force 11 to edit the shell. Estimated Complexity raised M → L to reflect the shell.

**3. Audit and connector data land in Postgres, not SQLite.** `audit_log` and `connectors` are declared in the Postgres schema only — `grep "CREATE TABLE.*\b\(audit_log\|connectors\)\b" db/schema.sql db/migrations/001_canonical_schema.sql` finds both in each, and the same grep against `db/schema_sqlite.sql` finds **neither** — and they have never been created or queried at runtime. The claim is *absence of these two tables from the `[BUILT]` SQLite store*, not a count of how many tables that store has: it grows (8b added `transactions`), so grep the DDL, never assume a fixed list. Rather than mirroring them into SQLite, this feature writes to the real Postgres path that **feature 10a stands up**. That makes feature 10a a hard prerequisite — see Dependencies.

**4. Connector `error` state is scoped in as two new columns.** The `connectors` DDL in `db/schema.sql` has a `last_sync` column but no error column — assert by parsing the `CREATE TABLE ... connectors` block and checking `"last_sync" in cols and "last_sync_status" not in cols`, not by line range — so the original "last sync time, entity count, errors" criterion had no data source for two of its three fields. Error state is scoped in as two new nullable columns on `connectors`, `last_sync_status TEXT` and `last_sync_error TEXT`, written by this feature's `POST /connectors/{provider}/sync` handler. This feature authors the `<NNN>_connector_sync_status.sql` migration and mirrors the columns into `db/schema.sql`; **feature 10a owns the runner that applies it**, so 16 ships the DDL file and lists it in the manifest. `connectors` is absent from `tests/test_schema_parity.py`'s `SHARED_TABLES` list — assert `"connectors" not in SHARED_TABLES` by importing the module; the list's membership is what matters, never its length, and it has already grown once (8b added `transactions` to it) — so no SQLite mirror is required and schema parity is unaffected. The third field, `entity_count`, is resolved in Owner Decision 5.

**5. `entity_count` is DEFERRED, not derived. Corrected 2026-08-22: the reason has changed, the conclusion has not.** The prior revision derived entity count at query time as `SELECT COUNT(*) FROM system_references WHERE tenant_id = :t AND source = :provider` against **Postgres**, and the revision before this one justified deferring it on the grounds that nothing populated `system_references` at all. That justification is now stale: **feature 10 has SHIPPED**, and `core/graph/resolution.py`'s `create_new_entity` writes one `system_references` row per supplied source. The table is populated.

It is populated **in SQLite**. Feature 10's brief is explicit that its write targets are SQLite graph tables and that it introduces no Postgres path; feature 10a leaves the graph store — `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references` — on SQLite and adds Postgres for approval/audit records only. Nothing in the plan writes a Postgres `system_references` row, so a Postgres-side `COUNT(*)` still returns zero on every real page load while passing against a seeded integration fixture — the exact false pass this phase exists to catch.

**Resolution chosen (unchanged): narrow the displayed metric to what Postgres genuinely knows (connector configuration + sync state) and defer `entity_count` entirely** — dropped from the page, from all three endpoint payloads, and from the success criteria. Reasoning, one line: the count lives in the SQLite graph store, `api/` has no SQLite connection path and this feature is forbidden from opening one, so a mixed-store read would introduce a second engine into `api/` purely to render one number — deferring is the only option honest about what the page can show today.

> **FOLLOW-UP 16-B — restore connector `entity_count` (blocker updated: feature 10 has SHIPPED; what remains is a decided read path).** The data now exists in SQLite. Decide deliberately between (a) a read-only mixed-store query against the SQLite graph store from `api/routers/connectors.py`, accepting a second engine in `api/`, or (b) the graph store moving to Postgres, at which point the original derived `COUNT(*)` becomes correct. Note that `system_references` carries no `tenant_id` column of its own — tenant scoping there is a join through `canonical_entities`, so option (a) is not a one-line query. Only then add `entity_count` back to the payload, the page, and the criteria. Until then, no surface in this feature may display or return an entity count.

**6. The `tenants` row is provisioned by feature 10a, not by this feature — and its absence must fail loudly here.** Verified by symbol, not by line: the `tenant_id` column of both the `connectors` and the `audit_log` `CREATE TABLE` blocks in `db/schema.sql` is declared `UUID NOT NULL REFERENCES tenants(id)`, and a `CREATE TABLE ... tenants` block exists with no seeded row anywhere outside feature 10a's own bootstrap migration (`grep -rn "INSERT INTO tenants" .`). Without a matching `tenants` row, every audit INSERT and every `connectors` query FK-fails or returns empty on the first run against a real database.

**Ownership: feature 10a owns tenant provisioning** — it stands up the Postgres path and its own brief identifies this FK as "the most likely first-run failure." This feature does **not** duplicate that work: it writes no `INSERT INTO tenants`, no seed script, no get-or-create helper, no call into `core/graph/tenants.py`, and no test fixture that creates the row as a side effect. It **consumes** the row and **asserts** it, in both the static and live forms described under the tenant-identifier seam. This is a stated assumption (see Dependencies), not an unexamined one: if 10a ships without provisioning the tenant, feature 16 must fail at startup with a named error rather than at a random INSERT in a background thread.

---

## Success Criteria

- [ ] `import dashboard.app` succeeds; `isinstance(dashboard.app.app, dash.Dash)` is True; `app.config.use_pages` is True; and **every path the registry actually reports** returns 200 — i.e. `for entry in dash.page_registry.values(): assert app.server.test_client().get(entry["path"]).status_code == 200`. The path set is **derived from `dash.page_registry`, never enumerated in the test**, so a page added by any feature landing before this one is covered rather than breaking the assertion. Additionally assert membership (not cardinality) of the two paths this feature owns: `{"/connectors", "/audit-log"} <= {e["path"] for e in dash.page_registry.values()}`. "Runs" is defined as exactly this — no manual browser check.
- [ ] **The sidebar contract feature 11 depends on holds, derived from the registry.** `build_sidebar` is importable from `dashboard.app` and is pure (calling it twice with the same fixture registry yields equal trees, and it performs no database access). For the real registry: the returned tree contains a container `id="app-sidebar"`; and for **every** entry in `dash.page_registry` there is exactly one `{"type": "nav-link", "path": <that path>}` id and exactly one `{"type": "nav-badge", "path": <that path>}` id — both sides derived from the registry, neither enumerated. Assert that `{"type": "nav-badge", "path": "/approval-queue"}` is among them, so feature 11's callback target provably exists. Assert `dashboard/app.py` registers no callbacks (`grep -n "@app.callback\|@callback\|clientside_callback" dashboard/app.py` returns nothing) — the shell reserves the slot and computes nothing.
- [ ] **Existing pages keep their paths; this feature only adds.** Derive both sides at runtime: build `registered = {module_basename: path}` from `dash.page_registry` after importing `dashboard.app`, and build `expected = {basename: path}` by scanning every `dashboard/pages/*.py` for its `dash.register_page(..., path=...)` argument. Assert `registered == expected` — every module on disk is registered, at the path its own source declares, with no extras. Then assert that the entries for `connectors` and `audit_log` are present, and that **every other module in `expected` maps to the same path it maps to under `git show HEAD:dashboard/pages/<name>.py`** for the modules that exist in `HEAD` (modules absent from `HEAD` are new arrivals from upstream features and are exempt). No count of sibling pages is asserted anywhere.
- [ ] `connectors.build_category_groups(FIXTURE)` where `FIXTURE` has connectors in N distinct categories returns a list of length N, and the set of `category` values in the returned components' pattern-matching ids equals the set of distinct categories in `FIXTURE`; every fixture provider appears in exactly one group as a `{"type": "connector-card", "provider": ...}` id.
- [ ] `audit_log.layout()` returns a tree containing a `dash_table.DataTable` with `id == "audit-log-table"` whose `columns` ids equal, **in order**, `["created_at","actor_id","action","resource","resource_id","category","diff"]`. **This list stays exact because this feature owns it** — it is the page's own display surface, defined here, not a count of something upstream controls; if a later feature adds a column to `audit_log`, the page continues to display these and the criterion stays true. It is guarded, not pinned, by a second assertion that derives the upstream side: **every id in that list must be a real column of the `audit_log` `CREATE TABLE` block in `db/schema.sql`** (parse the block, assert `set(displayed) <= set(ddl_columns)`) — so a rename upstream fails loudly while an addition upstream does not. No assertion is made about how many columns `audit_log` has. All five `audit-filter-*` control ids are present; `apply_filters(rows, action="connector.sync")` returns only rows with that action, and each of the other four filters is asserted the same way.
- [ ] `GET /connectors/` returns `200` and every item's key set equals exactly `{"provider","category","connected","last_sync","last_sync_status","last_sync_error"}`; `last_sync_status`/`last_sync_error` read the columns added by this feature's `*_connector_sync_status.sql` migration (located by glob, not by number). Asserted negatively too: `"entity_count" not in item` for every item, and `grep -rn "entity_count" api/ dashboard/ tests/` returns no matches — the deferral in Owner Decision 5 is enforced, not merely intended. (integration)
- [ ] `POST /connectors/quickbooks/sync` returns `202` with body `{"provider": "quickbooks", "status": "accepted"}`, and after `AuditQueue.drain()` the seeded row satisfies `last_sync IS NOT NULL AND last_sync_status = 'accepted'` and exactly one `audit_log` row exists with `action = 'connector.sync' AND resource_id = 'quickbooks'`. `POST /connectors/nope/sync` returns `404`. (integration)
- [ ] `api/main.py` registers the connectors router and both middleware — asserted by deriving the mounted route set and the middleware stack from `api.main.app` at runtime, not by reading the file — and `/health` still returns 200.
- [ ] **Audit coverage is enumerated, not asserted in the abstract.** The rule: *every request that matches a route registered from `api/routers/*` produces exactly one `audit_log` row; `/health` is the single documented exemption (liveness probe, not an action); a request matching no route produces none.* The actions in scope for this feature are exactly these, and the action string is fixed:

  | Route | `action` | `resource` | `resource_id` | `category` |
  |---|---|---|---|---|
  | `GET /connectors/` | `connector.list` | `connectors` | `null` | `null` |
  | `POST /connectors/{provider}/sync` | `connector.sync` | `connectors` | `<provider>` | the row's `connectors.category` |
  | `GET /connectors/{provider}/status` | `connector.status` | `connectors` | `<provider>` | the row's `connectors.category` |

  Every `resource` value is `connectors` and every `resource_id` is a provider name, per the audit-row namespacing rule above. Assertion mechanism, in `tests/test_audit.py`: (a) a `@pytest.mark.parametrize` over the rows above drives each route through the FastAPI `TestClient`, calls `AuditQueue.drain()`, and asserts exactly one `audit_log` row whose `action`, `resource`, `resource_id`, `category` and `tenant_id` equal the table's values — `category` asserted non-null for the two provider routes, `actor_id` asserted NULL for all; (b) `GET /health` and `GET /no-such-route` each produce zero rows; (c) a **completeness** assertion iterates `api.main.app.routes`, and asserts the set of `(method, path)` pairs contributed by `api/routers/*` minus the exemption set equals the set of keys in the middleware's action map — so registering a new route without an action mapping fails this test rather than silently going unaudited. (integration)
- [ ] The audit writer contains no UPDATE or DELETE targeting `audit_log` — `grep -nE "\b(UPDATE|DELETE)\b" api/middleware/audit.py` returns no match.
- [ ] **Audit writes are off the request path (replaces "no performance degradation").** Structural assertion, no timing: with the audit queue's worker not started and `core.graph.pg.connect` monkeypatched to a callable that raises on invocation, `GET /health` and `GET /connectors/` still return their normal status codes, and that callable records **zero** calls during request handling — proving no INSERT happens inline. A second assertion restores the real factory, calls `AuditQueue.drain()`, and confirms the pending rows are then written. `AuditMiddleware.dispatch` must additionally contain no call into the connection factory (asserted by inspecting `inspect.getsource`). No test sleeps or polls for the worker thread.
- [ ] **Missing `tenants` row fails loudly at startup — never silently, never auto-provisioned.** With `DATABASE_URL` set and no `tenants` row whose `id` equals the resolved tenant, `api/main.py`'s startup hook raises `RuntimeError` whose message names the missing UUID and directs the reader to feature 10a's tenant provisioning; the app does not start and no request is served. Asserted by an integration test that removes the `tenants` row inside the rolled-back `pg_conn` transaction and wraps app startup in `pytest.raises(RuntimeError, match=<the imported constant>)`. The test additionally asserts the diff contains no self-healing: `grep -rn "INSERT INTO tenants\|resolve_or_create_tenant" api/ dashboard/ tests/` returns no matches. A skip, a warning-only log, or a fallback tenant fails this criterion. (integration)
- [ ] **A tenant FK violation on an audit INSERT is loud, not dropped.** With the resolved tenant absent from `tenants`, `AuditQueue.drain()` logs at `ERROR` (captured via `caplog`) naming the offending tenant_id, and `AuditQueue.failed_writes` increases by one from the value captured before the call; the `queue.Full` warning-and-drop branch is asserted **not** to have been taken. (integration)
- [ ] Tenant placeholder resolves header → env → `DEFAULT_TENANT_ID`, attaches the value to `request.state.tenant_id`, and **never rejects a request** for tenant reasons; its docstring states plainly that this is not authentication and provides no isolation. Asserted also as a negative grep: no file in this feature's diff describes the resolver as isolation, RLS, multi-tenant, or access control.
- [ ] **Seam, static half: `DEFAULT_TENANT_ID` is 10a's constant by reference, not a literal invented here.** `api.middleware.tenant.DEFAULT_TENANT_ID is core.graph.pg.BOOTSTRAP_TENANT_ID` — object identity, asserted by importing both, never by comparing against a UUID typed into the test. `grep -rnE "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}" api/ dashboard/ tests/` returns no bare UUID literal standing in for the tenant id. (no database required)
- [ ] **Seam, live half: 10a's SQL seed and 10a's Python constant agree, checked from here.** Against a migrated database, `SELECT id FROM tenants WHERE id = %s` bound to the imported `BOOTSTRAP_TENANT_ID` returns a row, and an audit INSERT carrying `DEFAULT_TENANT_ID` commits without a `ForeignKeyViolation`. The bound parameter comes from the import; no UUID appears in the test source. (integration)
- [ ] **The connector-sync-status migration is discoverable and wired.** Exactly one file matches `db/migrations/*_connector_sync_status.sql`; its numeric prefix collides with no other prefix present in `db/migrations/`; its bare filename appears as a line in `db/migrations/postgres.manifest`; and no `_sqlite` sibling of it exists. Asserted by globbing the directory and reading the manifest — no filename is hardcoded in the test beyond the `*_connector_sync_status.sql` suffix.
- [ ] **10a's ownership is not duplicated.** `git diff --name-only` for this feature contains none of `pytest.ini`, `tests/conftest.py`, `core/graph/pg.py`, `core/graph/tenants.py`, `core/graph/audit.py`, `scripts/migrate_pg.py`; and `requirements.txt` gains no line. The only change to `db/migrations/postgres.manifest` is appended lines — `git diff -U0 db/migrations/postgres.manifest` shows no removed line.
- [ ] No JWT, Supabase, or `CREATE POLICY` code appears anywhere in the diff.
- [ ] `.venv/bin/python -m pytest tests/test_connectors_api.py tests/test_audit.py tests/test_dashboard_shell.py -x --tb=short` exits 0 with `DATABASE_URL` unset, **and** its reported collected count is greater than zero, parsed from that run's own output — integration-marked tests skip (a skip is a reported outcome, not a deselection), and the non-database tests actually run.
- [ ] `DATABASE_URL=... .venv/bin/python -m pytest tests/test_connectors_api.py tests/test_audit.py -m integration -x --tb=short` exits 0 against a freshly migrated database **and** its collected count is greater than zero, asserted from the same run's output. Exit code alone is not the assertion: under a non-matching or unregistered marker the run deselects everything and still exits 0, which is the silent pass this criterion exists to catch.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` exits 0 with no regression to the shipped suite, and its collected count is greater than or equal to the baseline captured on the same checkout immediately before this feature's first commit. Capture the baseline at build time; write no literal into the test.

> Use `.venv/bin/python -m pytest ...` throughout. Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` / `from api...` import fails to collect.

---

## Dependencies

- [ ] Both connectors shipped (features 5, 6) — SHIPPED. Need real connectors to display status. Neither exposes a `sync()`; the manual-sync endpoint composes `authenticate()` + `read_entities()` from the `ConnectorInterface` ABC.
- [ ] **Feature 10a (Postgres stand-up) — HARD DEPENDENCY. NOT YET BUILT (queue status BLOCKED).** Feature 10 has been split: feature 10 (SHIPPED) keeps only the SQLite Stage 6 resolution work, and feature **10a** owns standing up Postgres — the driver pin, `DATABASE_URL`, `core/graph/pg.py`, the manifest-driven migration runner, `pytest.ini` and the registered `integration` marker, `tests/conftest.py`'s `pg_conn` fixture, tenant provisioning, and the `approval_decisions` / `audit_log` writers. Feature 16's audit and connector data lands in Postgres, so **10a — not 10 — is 16's real prerequisite**: there is no table to write to, no connection path to write over, and no registered test marker until 10a lands. 16 must be ordered after 10a. See the ownership table in Scope for the full boundary.
- [ ] **STATED ASSUMPTION — 10a provisions the `tenants` row, and 16 checks it rather than absorbing it.** The `tenant_id` columns of the `connectors` and `audit_log` tables in `db/schema.sql` are both declared `UUID NOT NULL REFERENCES tenants(id)`, and no row exists in `tenants` today. Feature 16 assumes 10a's bootstrap seeds the row identified by its exported `core.graph.pg.BOOTSTRAP_TENANT_ID`, and binds `DEFAULT_TENANT_ID` to that constant **by import**, not to an independently chosen UUID. **10a owns this; 16 must not duplicate it** (Owner Decision 6). If the assumption is false, 16 does not degrade quietly — the static seam assertion fails at unit-test time and the startup precondition aborts the app with a named `RuntimeError`. This is the single assumption on 10a whose failure is checked rather than absorbed.
- [ ] **Feature 11 (approval-queue) is a downstream consumer, not a dependency.** It relies on `dashboard/app.py` existing and on the `{"type": "nav-badge", "path": ...}` slot for its live count. 16 must land before 11 builds its badge callback; 16 writes no part of that callback.
- [ ] **NOT a dependency — Postgres `system_references`.** Feature 16 no longer reads it. Feature 10 (SHIPPED) writes that table in SQLite only and 10a leaves the graph store on SQLite, so the Postgres copy stays permanently empty; `entity_count` is deferred to FOLLOW-UP 16-B rather than derived from a table nothing populates on that engine.
- [ ] ~~Canonical schema (feature 2) — audit_log and connectors tables~~ **FALSE as stated.** Feature 2 delivered `db/schema_sqlite.sql`, which declares neither table (grep it for `CREATE TABLE ... audit_log` / `... connectors`; its table list grows as features land and is not asserted here). The Postgres DDL it also wrote has never been executed. Superseded by the feature 10a dependency above.
- [ ] ~~Supabase Auth configured — JWT tokens for tenant extraction~~ **REMOVED.** No auth exists; feature 16 does not build it. See Owner Decision 1 and FOLLOW-UP 16-A.

---

## Estimated Complexity

**Rating:** L (raised from M on 2026-08-22)

**Rationale:** Larger than the original M because none of it is an extension. Two dashboard pages written from empty placeholders, **plus a Dash application shell that does not exist today** and without which the pages cannot be rendered or tested — and whose sidebar ids are a published contract another feature builds against, so getting them wrong costs a second feature's rework. Three API endpoints in a stub router; two middleware components created from docstring-only stubs; the first registration of a router in `api/main.py`; and the first *API-side* code in the tree to write to Postgres, landing on feature 10a's freshly-built connection path and inheriting its `DATABASE_URL`-gated integration tier, its `pg_conn` fixture, and its `--strict-markers` marker registration. The audit middleware intercepts every request but performs no inline database work: it enqueues onto an in-process queue drained by a background worker thread, which every criterion asserts structurally — via an explicit `drain()` — rather than by timing or sleeping. The load-bearing risk is the tenant-identifier seam with 10a: the value has three potential homes (10a's Python constant, 10a's bootstrap SQL, 16's resolver default), and a drift between any two of them surfaces as an FK violation inside a daemon thread rather than in a response, which is why it is enforced by a re-export plus both a static identity assertion and a live database assertion.

---

## PROJECT CONTEXT

### Dashboard Navigation (from spec Section 14)

```
Sidebar                          ◄── shell created by THIS FEATURE (dashboard/app.py)
├── Overview (feature 14)
├── Entity Graph (feature 14)
├── Approval Queue (feature 11)   ◄── badge slot reserved here, filled by 11
├── Modules
│   └── AR Reconciliation (feature 15)
└── System
    ├── Connectors ◄── THIS FEATURE
    └── Audit Log ◄── THIS FEATURE
```

This tree is the spec's *intent*, not an assertion about the registry's contents. The shell renders links by iterating `dash.page_registry`; pages other features add before or after this one appear too and are not a failure. The grouping shown here is presentational only — the flat registry iteration is the contract.

### Audit Log Schema

Postgres DDL — the `CREATE TABLE IF NOT EXISTS audit_log` block in `db/schema.sql`, mirrored in `db/migrations/001_canonical_schema.sql`. Created by feature 10a's migration runner; **absent from `db/schema_sqlite.sql`**. The shape below is reproduced for reading convenience; the authority is the `CREATE TABLE` block itself, and criteria parse it rather than quoting this snippet.

```sql
audit_log (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),  -- FK; row provisioned by feature 10a
    actor_id UUID,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    resource_id TEXT,
    category TEXT,  -- system category (accounting, psa, etc.)
    diff JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

`tenant_id` is populated from the placeholder resolver and **must reference an existing `tenants` row provisioned by feature 10a** (Owner Decision 6); `actor_id` is NULL for every row this feature writes — there are no users until feature 17, and 10a's rule that the UUID column never receives a free-form actor string applies equally here. The `connectors` table's `tenant_id` column carries the identical `REFERENCES tenants(id)` FK.

**Two writers share this table.** 10a's `core/graph/audit.py::log_resolution` writes Stage 6 resolution rows whose `resource_id` is a canonical identifier and whose `category` is a category *pair*; this feature's middleware writes API-action rows whose `resource` is always `connectors`, whose `resource_id` is always a provider name, and whose `category` is a single connector category. The audit-log page displays both without distinguishing them; any query that needs one kind filters on `resource`.

### V1 Hard Constraints

- Audit log: append-only, no UPDATE/DELETE — enforced here by writer discipline (INSERT-only) and a grep test, not by a database trigger or REVOKE. The "append-only" wording in `db/schema.sql` is a SQL comment; do not cite it as an enforced constraint.
- Every database query RLS-scoped to tenant_id — **`[PLANNED]`, not achieved by this feature.** Queries here carry a placeholder tenant_id as a filter; no row-level security exists, and the tenant value is caller-supplied and unauthenticated.
- OAuth tokens encrypted at rest with customer-specific keys — **`[PLANNED]`**, and out of scope here.

### Relevant Spec Sections

- Section 14: Product UI — Connectors page, Audit Log
- Section 8: System Architecture (principles 4, 5: idempotency, audit trail)
- Rules `.claude/rules/01-nexus-finance-v1.md` §0 (BUILT vs PLANNED), §10 (data security posture, per-line status)
