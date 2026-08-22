# Feature Brief: Connectors Page + Audit Log + System Infrastructure

**Author:** Neal Iyer
**Date:** 2026-05-10 (reality-corrected 2026-08-22)
**Status:** Approved
**Complexity:** L
**FP&A Phase:** Infrastructure
**Feature #:** 16

---

## Problem Statement

The product needs system management surfaces: a connectors page showing connected systems grouped by category with sync status, and an audit log showing every action the system has taken. The audit log is a compliance requirement — every resolution, every approval, every sync must be traceable. The connectors page is the GTM entry point — the first thing a new customer interacts with after signup.

**Starting state (verified in tree 2026-08-22).** Nothing this feature touches exists yet. `dashboard/pages/connectors.py` and `dashboard/pages/audit_log.py` are `dash.register_page` placeholders whose body is an empty `html.Div` plus a TODO — no layout logic of any kind. `api/routers/connectors.py`, `api/middleware/audit.py` and `api/middleware/tenant.py` are "Not yet implemented" docstring stubs with no executable body. `api/main.py` is a bare FastAPI app that **registers no routers at all** and exposes `/health` (other features may add routes before this one lands; the load-bearing fact is that *the connectors router* is not registered and neither middleware is installed). There is **no Dash `app` object anywhere in the tree** — the page files register against an application that does not exist, so nothing renders them. Per `.claude/rules/01-nexus-finance-v1.md` §0, every item not marked `[BUILT]` must be treated as NOT EXISTING; §10 marks tenant RLS `[PLANNED]`, the audit log `[PARTIAL]` (Postgres DDL only, stub writer), and auth `[PLANNED]`. This brief is written against that reality: the work below is **creation**, not extension.

---

## Scope

### In Scope

- **Create the minimal Dash application shell** (`dashboard/app.py`, NEW — does not exist today). Without it none of the render criteria below are verifiable, because `dash.register_page` calls in `dashboard/pages/*.py` have no application to register into. Keep it minimal and nothing more:
  - `dash.Dash(__name__, use_pages=True, pages_folder="pages")`
  - A top-level layout containing a plain sidebar `dcc.Link` list mirroring the navigation tree below, plus `dash.page_container`
  - A `if __name__ == "__main__": app.run(debug=True)` entrypoint
  - **Explicitly NOT in this shell:** theming, CSS/assets, auth gating, callbacks, state stores, or any page-specific logic. **Every other module in `dashboard/pages/` — whatever set exists at build time, this feature does not enumerate or cap it** — is picked up automatically by `use_pages` and must keep rendering its current layout at its current path, unmodified. The sidebar link list is **built by iterating `dash.page_registry`**, not by hardcoding entries, so pages added by features landing before this one appear without editing the shell. (Non-load-bearing: at authoring time the siblings were `overview.py`, `entity_graph.py`, `approval_queue.py`, `ar_reconciliation.py`.)

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

- **Create `api/routers/connectors.py`** (currently a docstring-only stub) and **register it in `api/main.py`**, which today registers no routers at all:
  - `GET /connectors/` — list connected systems for the active tenant. `200`, JSON list; each item has exactly the keys `provider`, `category`, `connected`, `last_sync`, `last_sync_status`, `last_sync_error`. **`entity_count` is deliberately absent** (Owner Decision 5).
  - `POST /connectors/{provider}/sync` — trigger manual sync. Returns `202` with body `{"provider": <provider>, "status": "accepted"}`, sets `connectors.last_sync = now()` and `last_sync_status = 'accepted'` for that `(tenant_id, provider)` row, and enqueues an `audit_log` row with `action = "connector.sync"`, `resource = "connectors"`, `resource_id = <provider>`. A provider with no row for the resolved tenant returns `404`.
  - `GET /connectors/{provider}/status` — `200` with `last_sync`, `last_sync_status`, `last_sync_error`; `404` for an unknown provider.

- **Create the connector-sync-status migration** (NEW), named `<NNN>_connector_sync_status.sql` in `db/migrations/`, where `<NNN>` is the **next unused three-digit prefix** at build time — do not hardcode a number, `ls db/migrations/` and take max(prefix)+1, since features landing before this one also add migrations. Body: `ALTER TABLE connectors ADD COLUMN IF NOT EXISTS last_sync_status TEXT; ALTER TABLE connectors ADD COLUMN IF NOT EXISTS last_sync_error TEXT;`. Mirror both columns into `db/schema.sql`'s `connectors` DDL. **Append the filename to the end of `db/migrations/postgres.manifest`** (the file feature 10a creates; 10a's runner applies exactly the manifest, in manifest order, and ignores anything not listed) — authoring the file without listing it means it never runs. Authored here; **applied by feature 10a's migration runner** (this feature does not build a runner).

- **Create `api/middleware/audit.py`** (currently a docstring-only stub):
  - Middleware that logs every API action to the Postgres `audit_log` table
  - Fields: tenant_id, actor_id, action, resource, resource_id, category, diff (JSONB), timestamp
  - **Audit writes are BACKGROUND, never inline on the request path.** The middleware builds the row dict and hands it to a module-level `AuditQueue` (a `queue.Queue`) via `enqueue(row)`, then returns the response. A single daemon worker thread drains the queue and performs the INSERTs. The request handler performs **zero** database round trips for auditing. `enqueue` must never block or raise into the request: it uses `put_nowait` and, on `queue.Full`, drops the row and logs a warning.
  - Append-only — the writer issues INSERT only and must contain no UPDATE or DELETE against `audit_log`
  - **The worker must not mask a missing tenant.** In `db/schema.sql`, the `audit_log` table's `tenant_id` column is declared `UUID NOT NULL REFERENCES tenants(id)` (grep the `CREATE TABLE ... audit_log` block; do not cite a line number), so an INSERT with an unprovisioned tenant raises `ForeignKeyViolation`. That exception takes a **separate, loud** path from the `queue.Full` drop: logged at `ERROR` naming the offending tenant_id and pointing at feature 10a's provisioning step, and counted on `AuditQueue.failed_writes`. It is never folded into the generic warning-and-drop branch.

- **Create `api/middleware/tenant.py`** (currently a docstring-only stub) as a **single-tenant placeholder — NOT authentication and NOT isolation**:
  - Resolve a tenant identity for each request in this precedence order: the `X-Nexus-Tenant` request header if present, else the `NEXUS_TENANT_ID` environment variable, else the module constant `DEFAULT_TENANT_ID` (a fixed, documented UUID literal). The resolved value is attached to `request.state.tenant_id` for downstream handlers and the audit writer.
  - **`DEFAULT_TENANT_ID` must equal the named constant feature 10a defines: `core.graph.pg.BOOTSTRAP_TENANT_ID`.** This feature does not choose, invent, or re-type the UUID value and does not insert the row — it imports/quotes 10a's constant and asserts equality against it (`from core.graph.pg import BOOTSTRAP_TENANT_ID; assert str(DEFAULT_TENANT_ID) == str(BOOTSTRAP_TENANT_ID)`). No UUID literal originating in this feature is acceptable. See Owner Decision 6.
  - **Startup precondition, `api/main.py`:** when `DATABASE_URL` is set, a startup hook calls `require_tenant_row(conn, tenant_id)` — `SELECT 1 FROM tenants WHERE id = %s` — and on zero rows raises `RuntimeError` naming the missing UUID and the 10a bootstrap step, aborting startup. It never INSERTs the row, never falls back to another tenant, and never downgrades to a warning or a skip.
  - The resolved tenant_id is **stamped onto writes and used as a query filter**, giving the data a correct shape for a future multi-tenant world. It provides **zero security guarantee**: the header is unauthenticated, any caller may set it to any value, and no request is ever rejected for supplying a tenant it does not own.
  - The module docstring and the endpoint docs MUST say this in those terms. No code, test, comment, or success criterion in this feature may describe the result as tenant isolation, RLS, or access control.
  - No JWT parsing, no Supabase import, no `CREATE POLICY`, no session handling.

- **Test suite:** `tests/test_connectors_api.py`, `tests/test_audit.py`
  - Assert: `GET /connectors/` returns connectors filtered by the resolved tenant_id
  - Assert: the placeholder resolver honours header → env → `DEFAULT_TENANT_ID` precedence, and that an absent header yields the documented default rather than a rejection
  - Assert: audit rows created for every API action, carrying the resolved tenant_id
  - Assert: the audit writer performs INSERT only — grep-level assertion that no UPDATE/DELETE statement targets `audit_log`
  - Assert: `dashboard/app.py` imports, exposes `app`, and `dash.page_registry` contains the `/connectors` and `/audit-log` paths
  - Postgres-touching assertions follow feature 10a's pattern: `@pytest.mark.integration`, skipped when `DATABASE_URL` is unset. Dash structure, filter-helper, and header-precedence tests require no database.
  - `tests/test_dashboard_shell.py`: imports `dashboard.app`, the two page modules, and asserts the structure contracts above via a `dash.testing`-free path (`app.server` + `starlette`/`flask` test client for route status codes).

### Out of Scope

- **Authentication of any kind** — see Owner Decision below. No login, no signup, no JWT issuance or verification, no Supabase client. Owned by feature 17.
- Real multi-tenant isolation, row-level security, `CREATE POLICY`, or cross-tenant rejection. None of it is achieved by this feature.
- OAuth flow implementation (handled in QB/RUDDR connector features and feature 17)
- Webhook receiver endpoints
- Connector health monitoring / alerting (Sentry handles errors)
- Connector configuration UI (API keys, custom field mapping)
- Any Dash work beyond the minimal shell — no theming, no shared component library, no cross-page state

---

## Owner Decisions (2026-08-22)

**1. No auth in feature 16; single-tenant placeholder instead.** This feature assumes a single tenant and uses the explicit placeholder identity described above (`X-Nexus-Tenant` header → `NEXUS_TENANT_ID` env → `DEFAULT_TENANT_ID` constant). The prior "Supabase Auth configured — JWT tokens for tenant extraction" dependency was false — `supabase==2.9.0` is pinned in `requirements.txt` but never imported, and `api/routers/auth.py` is a stub. Real login and JWT arrive with **feature 17 (signup-onboarding)**, whose brief already claims `POST /auth/login` and Supabase Auth.

> **FOLLOW-UP 16-A — retire the placeholder tenant (blocked on feature 17).** When 17 lands: replace the header/env/constant resolver in `api/middleware/tenant.py` with JWT claim extraction; make a missing or invalid token a rejection rather than a fallback to `DEFAULT_TENANT_ID`; delete `DEFAULT_TENANT_ID` and the `X-Nexus-Tenant` header path so no unauthenticated caller can assert a tenant; add the cross-tenant rejection tests that this feature deliberately does not write; and only then may any brief or doc describe the system as tenant-isolated.

**2. The minimal Dash shell is in scope.** Feature 16 creates `dashboard/app.py` because without an application object its page-render criteria are unverifiable. Kept to registration plus navigation, nothing more. Estimated Complexity raised M → L to reflect it.

**3. Audit and connector data land in Postgres, not SQLite.** `audit_log` and `connectors` are declared in the Postgres schema only — `grep -c "CREATE TABLE.*\b\(audit_log\|connectors\)\b" db/schema.sql db/migrations/001_canonical_schema.sql` finds both in each, and `grep "CREATE TABLE.*\b\(audit_log\|connectors\)\b" db/schema_sqlite.sql` finds **neither** — and they have never been created or queried at runtime. The claim is *absence of these two tables from the `[BUILT]` SQLite store*, not a count of how many tables that store has (it grows: feature 8b added `transactions`). Rather than mirroring them into SQLite, this feature writes to the real Postgres path that **feature 10a has been scoped to stand up** (`psycopg[binary]`, `DATABASE_URL`, the migration runner, the integration test tier, `audit_log` + `approval_decisions`). That makes feature 10a a hard prerequisite — see Dependencies.

**4. Connector `error` state is scoped in as two new columns.** The `connectors` DDL in `db/schema.sql` has a `last_sync` column but no error column — assert by parsing the `CREATE TABLE ... connectors` block and checking `"last_sync" in cols and "last_sync_status" not in cols`, not by line range — so the original "last sync time, entity count, errors" criterion had no data source for two of its three fields. Error state is scoped in as two new nullable columns on `connectors`, `last_sync_status TEXT` and `last_sync_error TEXT`, written by this feature's `POST /connectors/{provider}/sync` handler. This feature authors the `<NNN>_connector_sync_status.sql` migration (ALTER TABLE, both columns; `<NNN>` = next unused prefix) and mirrors them into `db/schema.sql`; **feature 10a owns the migration runner that actually applies migrations**, so 16 ships the DDL file, lists it in `db/migrations/postgres.manifest`, and 10a's runner executes it. `connectors` is absent from `tests/test_schema_parity.py`'s `SHARED_TABLES` list (assert `"connectors" not in SHARED_TABLES` by importing the module — the list's membership, not its length, is what matters), so no SQLite mirror is required. The third field, `entity_count`, is resolved separately in Owner Decision 5.

**5. `entity_count` is DEFERRED, not derived — the earlier "derive it from Postgres `system_references`" answer was provably always 0.** The prior revision derived entity count at query time as `SELECT COUNT(*) FROM system_references WHERE tenant_id = :t AND source = :provider` against **Postgres**. Verified 2026-08-22, that value can never be anything but zero in production: feature 10 writes `system_references` **into SQLite only** and is explicitly forbidden from touching Postgres (`features/pipeline/resolution-graph-update.md`, Scope + Out of Scope + Anti-Goals), and feature 10a explicitly leaves the graph store — `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references` — on SQLite and adds Postgres for approval/audit records only (`features/infrastructure/postgres-store-bootstrap.md`, Out of Scope). Nothing in the plan ever writes a Postgres `system_references` row, so the criterion would have passed on a seeded integration fixture and shown `0` on every real page load — the exact false pass this phase exists to catch.

**Resolution chosen: narrow the displayed metric to what Postgres genuinely knows (connector configuration + sync state) and defer `entity_count` entirely** — dropped from the page, from all three endpoint payloads, and from the success criteria. Reasoning, one line: the count lives in the SQLite graph store, `api/` has no SQLite connection path today and nothing populates that table until feature 10's Stage 6 ships, so a mixed-store read would invent a second engine in this feature to display a number that is still empty — deferring is the only option that is honest about what the page can show today.

> **FOLLOW-UP 16-B — restore connector `entity_count` (blocked on feature 10 Stage 6 landing, and on a decided read path).** When Stage 6 is populating `system_references`, decide deliberately between (a) a read-only mixed-store query against the SQLite graph store from `api/routers/connectors.py`, or (b) the graph store moving to Postgres, at which point the original derived `COUNT(*)` becomes correct. Only then add `entity_count` back to the payload, the page, and the criteria. Until then, no surface in this feature may display or return an entity count.

**6. The `tenants` row is provisioned by feature 10a, not by this feature — and its absence must fail loudly here.** Verified 2026-08-22 in `db/schema.sql` by symbol, not by line: the `tenant_id` column of both the `connectors` and the `audit_log` `CREATE TABLE` blocks is declared `UUID NOT NULL REFERENCES tenants(id)`, and a `CREATE TABLE ... tenants` block exists with no seeded row anywhere in the tree (`grep -rn "INSERT INTO tenants" .` returns nothing outside feature 10a's own bootstrap migration). The placeholder resolver above therefore produces a `DEFAULT_TENANT_ID` that satisfies **no** foreign key: without a matching `tenants` row, every audit INSERT and every `connectors` query FK-fails or returns empty on the first run against a real database.

**Ownership: feature 10a (`postgres-store-bootstrap`) owns tenant provisioning** — it stands up the Postgres path and its own brief already identifies the FK as "the most likely first-run failure." This feature does **not** duplicate that work: it writes no `INSERT INTO tenants`, no seed script, no get-or-create helper, and no test fixture that creates the row as a side effect. It **consumes** the row and **asserts** it. This is a stated assumption (see Dependencies), not an unexamined one: if 10a ships without provisioning the tenant, feature 16 must fail at startup with a named error rather than at a random INSERT — which is what the precondition criterion below enforces.

---

## Success Criteria

- [ ] `import dashboard.app` succeeds; `isinstance(dashboard.app.app, dash.Dash)` is True; `app.config.use_pages` is True; and **every path the registry actually reports** returns 200 — i.e. `for entry in dash.page_registry.values(): assert app.server.test_client().get(entry["path"]).status_code == 200`. The path set is **derived from `dash.page_registry`, never enumerated in the test**, so a page added by any feature landing before this one is covered rather than breaking the assertion. Additionally assert membership (not cardinality) of the two paths this feature owns: `{"/connectors", "/audit-log"} <= {e["path"] for e in dash.page_registry.values()}`. "Runs" is defined as exactly this — no manual browser check. (Non-load-bearing note: at authoring time `overview.py` registered `/`, not `/overview`; the derived assertion does not depend on that.)
- [ ] **Existing pages keep their paths; this feature only adds.** Derive both sides at runtime: build `registered = {module_basename: path}` from `dash.page_registry` after importing `dashboard.app`, and build `expected = {basename: path}` by scanning every `dashboard/pages/*.py` for its `dash.register_page(..., path=...)` argument. Assert `registered == expected` — every module on disk is registered, at the path its own source declares, with no extras. Then assert that the entries for `connectors` and `audit_log` are present, and that **every other module in `expected` maps to the same path it maps to under `git show HEAD:dashboard/pages/<name>.py`** for the modules that exist in `HEAD` (modules absent from `HEAD` are new arrivals from upstream features and are exempt). No count of sibling pages is asserted anywhere.
- [ ] `connectors.build_category_groups(FIXTURE)` where `FIXTURE` has connectors in N distinct categories returns a list of length N, and the set of `category` values in the returned components' pattern-matching ids equals the set of distinct categories in `FIXTURE`; every fixture provider appears in exactly one group as a `{"type": "connector-card", "provider": ...}` id.
- [ ] `audit_log.layout()` returns a tree containing a `dash_table.DataTable` with `id == "audit-log-table"` whose `columns` ids equal, **in order**, `["created_at","actor_id","action","resource","resource_id","category","diff"]`. **This list stays exact because this feature owns it** — it is the page's own display surface, defined here, not a count of something upstream controls; if a later feature adds a column to `audit_log`, the page continues to display these and the criterion stays true. It is guarded, not pinned, by a second assertion that derives the upstream side: **every id in that list must be a real column of the `audit_log` `CREATE TABLE` block in `db/schema.sql`** (parse the block, assert `set(displayed) <= set(ddl_columns)`) — so a rename upstream fails loudly while an addition upstream does not. No assertion is made about how many columns `audit_log` has. All five `audit-filter-*` control ids are present; `apply_filters(rows, action="connector.sync")` returns only rows with that action, and each of the other four filters is asserted the same way.
- [ ] `GET /connectors/` returns `200` and every item's key set equals exactly `{"provider","category","connected","last_sync","last_sync_status","last_sync_error"}`; `last_sync_status`/`last_sync_error` read the columns added by this feature's `*_connector_sync_status.sql` migration (located by glob, not by number). Asserted negatively too: `"entity_count" not in item` for every item, and `grep -rn "entity_count" api/ dashboard/ tests/` returns no matches — the deferral in Owner Decision 5 is enforced, not merely intended.
- [ ] `POST /connectors/quickbooks/sync` returns `202` with body `{"provider": "quickbooks", "status": "accepted"}`, and after the audit worker is drained the seeded row satisfies `last_sync IS NOT NULL AND last_sync_status = 'accepted'` and exactly one `audit_log` row exists with `action = 'connector.sync' AND resource_id = 'quickbooks'`. `POST /connectors/nope/sync` returns `404`.
- [ ] `api/main.py` registers the connectors router and both middleware; `/health` still returns 200
- [ ] **Audit coverage is enumerated, not asserted in the abstract.** The rule: *every request that matches a route registered from `api/routers/*` produces exactly one `audit_log` row; `/health` is the single documented exemption (liveness probe, not an action); a request matching no route produces none.* The actions in scope for this feature are exactly these three, and the action string is fixed:

  | Route | `action` | `resource` | `resource_id` | `category` |
  |---|---|---|---|---|
  | `GET /connectors/` | `connector.list` | `connectors` | `null` | `null` |
  | `POST /connectors/{provider}/sync` | `connector.sync` | `connectors` | `<provider>` | the row's `connectors.category` |
  | `GET /connectors/{provider}/status` | `connector.status` | `connectors` | `<provider>` | the row's `connectors.category` |

  Assertion mechanism, in `tests/test_audit.py`, run by `.venv/bin/python -m pytest tests/test_audit.py -x --tb=short`: (a) a `@pytest.mark.parametrize` over the three rows above drives each route through the FastAPI `TestClient`, drains the audit queue synchronously, and asserts exactly one `audit_log` row whose `action`, `resource`, `resource_id`, `category` and `tenant_id` equal the table's values — `category` asserted non-null for the two provider routes; (b) `GET /health` and `GET /no-such-route` each produce zero rows; (c) a **completeness** assertion iterates `api.main.app.routes`, and asserts the set of `(method, path)` pairs contributed by `api/routers/*` minus the exemption set equals the set of keys in the middleware's action map — so registering a new route without an action mapping fails this test rather than silently going unaudited. (integration)
- [ ] The audit writer contains no UPDATE or DELETE targeting `audit_log`
- [ ] **Audit writes are off the request path (replaces "no performance degradation").** Structural assertion, no timing: with the audit queue's worker not started and the database connection factory monkeypatched to a callable that raises on invocation, `GET /health` and `GET /connectors/` still return their normal status codes, and that factory records **zero** calls during request handling — proving no INSERT happens inline. A second assertion drains the queue explicitly and confirms the pending rows are then written. `AuditMiddleware.dispatch` must additionally contain no `await`/call into the connection factory (asserted by inspecting `inspect.getsource`).
- [ ] **Missing `tenants` row fails loudly at startup — never silently, never auto-provisioned.** With `DATABASE_URL` set and no `tenants` row whose `id` equals the resolved tenant, `api/main.py`'s startup hook raises `RuntimeError` whose message names the missing UUID and directs the reader to feature 10a's tenant provisioning; the app does not start and no request is served. Asserted by an integration test that truncates/omits the `tenants` row and wraps app startup in `pytest.raises(RuntimeError, match=<uuid>)`. The test additionally asserts the diff contains **no** `INSERT INTO tenants` (`grep -rn "INSERT INTO tenants" api/ dashboard/ tests/` returns no matches) — feature 16 must not self-heal the precondition it is checking. A skip, a warning-only log, or a fallback tenant fails this criterion. (integration)
- [ ] **A tenant FK violation on an audit INSERT is loud, not dropped.** With the worker running and the resolved tenant absent from `tenants`, the queue worker logs at `ERROR` (captured via `caplog`) naming the offending tenant_id, and `AuditQueue.failed_writes == 1`; the `queue.Full` warning-and-drop branch is asserted **not** to have been taken. (integration)
- [ ] Tenant placeholder resolves header → env → `DEFAULT_TENANT_ID`, attaches the value to `request.state.tenant_id`, and **never rejects a request** for tenant reasons; its docstring states plainly that this is not authentication and provides no isolation
- [ ] **`DEFAULT_TENANT_ID` is 10a's constant, not a literal invented here.** `str(api.middleware.tenant.DEFAULT_TENANT_ID) == str(core.graph.pg.BOOTSTRAP_TENANT_ID)`, asserted by importing both — never by comparing against a UUID typed into the test. `grep -rnE "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}" api/ tests/` returns no bare UUID literal standing in for the tenant id. (no database required)
- [ ] **The connector-sync-status migration is discoverable and wired.** Exactly one file matches `db/migrations/*_connector_sync_status.sql`; its numeric prefix is strictly greater than every other prefix present in `db/migrations/` at the time it was added (i.e. it collides with nothing); and its bare filename appears as a line in `db/migrations/postgres.manifest`. Asserted by globbing the directory and reading the manifest — no filename is hardcoded in the test beyond the `*_connector_sync_status.sql` suffix.
- [ ] No JWT, Supabase, or `CREATE POLICY` code appears anywhere in the diff
- [ ] `.venv/bin/python -m pytest tests/test_connectors_api.py tests/test_audit.py` passes with `DATABASE_URL` unset (integration-marked Postgres tests skip)
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` passes with no regression to the shipped suite

> Use `.venv/bin/python -m pytest ...` throughout. Bare `pytest` is not on PATH, and the console script does not put the repo root on `sys.path`.

---

## Dependencies

- [ ] Both connectors shipped (features 5, 6) — SHIPPED. Need real connectors to display status.
- [ ] **Feature 10a (Postgres stand-up) — HARD DEPENDENCY. NOT YET BUILT.** Feature 10 has been split: feature 10 keeps only the SQLite Stage 6 resolution work, and the new feature **10a** owns standing up Postgres — the `psycopg[binary]` driver pin, `DATABASE_URL`, the migration runner, the `@pytest.mark.integration` tier, and creation of the `approval_decisions` and `audit_log` tables. Feature 16's audit and connector data lands in Postgres, so **10a — not 10 — is 16's real prerequisite**: there is no table to write to and no connection path to write over until 10a lands. 16 must be ordered after 10a.
- [ ] **STATED ASSUMPTION — 10a provisions the `tenants` row.** The `tenant_id` columns of the `connectors` and `audit_log` tables in `db/schema.sql` are both declared `UUID NOT NULL REFERENCES tenants(id)`, and no row exists in `tenants` today. Feature 16 assumes 10a's bootstrap seeds the row identified by its exported constant `core.graph.pg.BOOTSTRAP_TENANT_ID`, and that `DEFAULT_TENANT_ID` is set **equal to that constant by reference**, not to an independently chosen UUID. **10a owns this; 16 must not duplicate it** (Owner Decision 6). If that assumption is false, 16 does not degrade quietly — the startup precondition criterion aborts the app with a named `RuntimeError`. This is the single assumption on 10a whose failure is checked rather than absorbed.
- [ ] **NOT a dependency — Postgres `system_references`.** Feature 16 no longer reads it. Feature 10 writes that table in SQLite only and 10a leaves the graph store on SQLite, so the Postgres copy is permanently empty; `entity_count` is deferred to FOLLOW-UP 16-B rather than derived from a table nothing populates.
- [ ] ~~Canonical schema (feature 2) — audit_log and connectors tables~~ **FALSE as stated.** Feature 2 delivered `db/schema_sqlite.sql`, which declares neither table (grep it for `CREATE TABLE ... audit_log` / `... connectors`; its full table list grows as features land and is not asserted here). The Postgres DDL it also wrote has never been executed. Superseded by the feature 10a dependency above.
- [ ] ~~Supabase Auth configured — JWT tokens for tenant extraction~~ **REMOVED.** No auth exists; feature 16 does not build it. See Owner Decision 1 and FOLLOW-UP 16-A.

---

## Estimated Complexity

**Rating:** L (raised from M on 2026-08-22)

**Rationale:** Larger than the original M because none of it is an extension. Two dashboard pages written from empty placeholders, **plus a Dash application shell that does not exist today** and without which the pages cannot be rendered or tested; three API endpoints in a stub router; two middleware components created from docstring-only stubs; first registration of this feature's router in `api/main.py`; and the first application code in the tree to write to Postgres, which lands on feature 10a's freshly-built connection path and inherits its `DATABASE_URL`-gated integration test pattern. The audit middleware intercepts every request but performs no inline database work: it enqueues onto an in-process queue drained by a background worker thread (Owner Decision above), which is what the corresponding success criterion asserts structurally rather than by timing.

---

## PROJECT CONTEXT

### Dashboard Navigation (from spec Section 14)

```
Sidebar                          ◄── shell created by THIS FEATURE (dashboard/app.py)
├── Overview (feature 14)
├── Entity Graph (feature 14)
├── Approval Queue (feature 11)
├── Modules
│   └── AR Reconciliation (feature 15)
└── System
    ├── Connectors ◄── THIS FEATURE
    └── Audit Log ◄── THIS FEATURE
```

This tree is the spec's *intent*, not an assertion about the registry's contents. The shell renders links by iterating `dash.page_registry`; pages other features add before this one lands appear too and are not a failure.

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

`tenant_id` is populated from the placeholder resolver and **must reference an existing `tenants` row provisioned by feature 10a** (Owner Decision 6); `actor_id` is NULL for every row this feature writes — there are no users until feature 17. The `connectors` table's `tenant_id` column carries the identical `REFERENCES tenants(id)` FK.

### V1 Hard Constraints

- Audit log: append-only, no UPDATE/DELETE — enforced here by writer discipline (INSERT-only) and a test, not by a database trigger or REVOKE.
- Every database query RLS-scoped to tenant_id — **`[PLANNED]`, not achieved by this feature.** Queries here carry a placeholder tenant_id as a filter; no row-level security exists.
- OAuth tokens encrypted at rest with customer-specific keys — **`[PLANNED]`**, and out of scope here.

### Relevant Spec Sections

- Section 14: Product UI — Connectors page, Audit Log
- Section 8: System Architecture (principles 4, 5: idempotency, audit trail)
- Rules `.claude/rules/01-nexus-finance-v1.md` §0 (BUILT vs PLANNED), §10 (data security posture, per-line status)
