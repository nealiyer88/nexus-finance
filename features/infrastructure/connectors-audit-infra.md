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

**Starting state (verified in tree 2026-08-22).** Nothing this feature touches exists yet. `dashboard/pages/connectors.py` and `dashboard/pages/audit_log.py` are 9-line `dash.register_page` placeholders with an empty `html.Div` and a TODO. `api/routers/connectors.py`, `api/middleware/audit.py` and `api/middleware/tenant.py` are 4-line "Not yet implemented" docstring stubs. `api/main.py` is a bare FastAPI app with a single `/health` route and **no router registration**. There is **no Dash `app` object anywhere in the tree** — the page files register against an application that does not exist, so nothing renders them. Per `.claude/rules/01-nexus-finance-v1.md` §0, every item not marked `[BUILT]` must be treated as NOT EXISTING; §10 marks tenant RLS `[PLANNED]`, the audit log `[PARTIAL]` (Postgres DDL only, stub writer), and auth `[PLANNED]`. This brief is written against that reality: the work below is **creation**, not extension.

---

## Scope

### In Scope

- **Create the minimal Dash application shell** (`dashboard/app.py`, NEW — does not exist today). Without it none of the render criteria below are verifiable, because `dash.register_page` calls in `dashboard/pages/*.py` have no application to register into. Keep it minimal and nothing more:
  - `dash.Dash(__name__, use_pages=True, pages_folder="pages")`
  - A top-level layout containing a plain sidebar `dcc.Link` list mirroring the navigation tree below, plus `dash.page_container`
  - A `if __name__ == "__main__": app.run(debug=True)` entrypoint
  - **Explicitly NOT in this shell:** theming, CSS/assets, auth gating, callbacks, state stores, or any page-specific logic. Existing sibling pages (`overview.py`, `entity_graph.py`, `approval_queue.py`, `ar_reconciliation.py`) will be picked up automatically by `use_pages` and must keep rendering their current placeholder layouts unchanged.

- **Create `dashboard/pages/connectors.py`** (currently a 9-line empty-`Div` placeholder; replace its body):
  - Connected systems grouped by category (Accounting: QB ✓ | PSA: RUDDR ✓)
  - Per-connector status: connected, last sync timestamp, entity count, error count
  - Connect / Disconnect buttons that link out to the OAuth entrypoints owned by feature 17 (this feature renders the controls; it does not implement the flow)
  - Manual sync trigger button per connector
  - Future connector slots shown as "Coming Soon" (Bill.com, Stripe, Gusto)

- **Create `dashboard/pages/audit_log.py`** (currently a 9-line empty-`Div` placeholder; replace its body):
  - Filterable table: timestamp, actor (`system` | user_id), action, resource type, resource_id, category, diff summary
  - Filters: date range, action type, resource type, category, actor
  - Append-only display — no edit or delete UI actions
  - Paginated, sorted by timestamp desc

- **Create `api/routers/connectors.py`** (currently a 4-line stub) and **register it in `api/main.py`**, which today registers no routers at all:
  - `GET /connectors/` — list connected systems for the active tenant with status
  - `POST /connectors/{provider}/sync` — trigger manual sync
  - `GET /connectors/{provider}/status` — last sync details, error log

- **Create `api/middleware/audit.py`** (currently a 4-line stub):
  - Middleware that logs every API action to the Postgres `audit_log` table
  - Fields: tenant_id, actor_id, action, resource, resource_id, category, diff (JSONB), timestamp
  - Append-only — the writer issues INSERT only and must contain no UPDATE or DELETE against `audit_log`

- **Create `api/middleware/tenant.py`** (currently a 4-line stub) as a **single-tenant placeholder — NOT authentication and NOT isolation**:
  - Resolve a tenant identity for each request in this precedence order: the `X-Nexus-Tenant` request header if present, else the `NEXUS_TENANT_ID` environment variable, else the module constant `DEFAULT_TENANT_ID` (a fixed, documented UUID literal). The resolved value is attached to `request.state.tenant_id` for downstream handlers and the audit writer.
  - The resolved tenant_id is **stamped onto writes and used as a query filter**, giving the data a correct shape for a future multi-tenant world. It provides **zero security guarantee**: the header is unauthenticated, any caller may set it to any value, and no request is ever rejected for supplying a tenant it does not own.
  - The module docstring and the endpoint docs MUST say this in those terms. No code, test, comment, or success criterion in this feature may describe the result as tenant isolation, RLS, or access control.
  - No JWT parsing, no Supabase import, no `CREATE POLICY`, no session handling.

- **Test suite:** `tests/test_connectors_api.py`, `tests/test_audit.py`
  - Assert: `GET /connectors/` returns connectors filtered by the resolved tenant_id
  - Assert: the placeholder resolver honours header → env → `DEFAULT_TENANT_ID` precedence, and that an absent header yields the documented default rather than a rejection
  - Assert: audit rows created for every API action, carrying the resolved tenant_id
  - Assert: the audit writer performs INSERT only — grep-level assertion that no UPDATE/DELETE statement targets `audit_log`
  - Assert: `dashboard/app.py` imports, exposes `app`, and `dash.page_registry` contains the `/connectors` and `/audit-log` paths
  - Postgres-touching assertions follow feature 10's pattern: `@pytest.mark.integration`, skipped when `DATABASE_URL` is unset. Dash and header-precedence tests require no database.

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

**3. Audit and connector data land in Postgres, not SQLite.** `audit_log` and `connectors` exist in the Postgres schema only (`db/schema.sql:106`, `:14`; `db/migrations/001_canonical_schema.sql:114`, `:28`) and have never been created or queried at runtime. The `[BUILT]` SQLite store `db/schema_sqlite.sql` has exactly four tables and contains **neither**. Rather than mirroring them into SQLite, this feature writes to the real Postgres path that **feature 10 has been scoped to stand up** (`psycopg[binary]`, `DATABASE_URL`, `core/graph/pg.py`, `scripts/migrate_pg.py` applying migration 001). That makes feature 10 a hard prerequisite — see Dependencies.

---

## Success Criteria

- [ ] `dashboard/app.py` exists, exposes a `dash.Dash` object named `app` with `use_pages=True`, and runs
- [ ] `dash.page_registry` contains `/connectors` and `/audit-log`; the four pre-existing sibling pages still register and render unchanged
- [ ] `dashboard/pages/connectors.py` renders connected systems grouped by category
- [ ] `dashboard/pages/audit_log.py` renders a filterable audit log table
- [ ] Connector status shows last sync time, entity count, errors
- [ ] Manual sync trigger works via API endpoint
- [ ] `api/main.py` registers the connectors router and both middleware; `/health` still returns 200
- [ ] Audit middleware writes a row to the Postgres `audit_log` for every API action, including the category field
- [ ] The audit writer contains no UPDATE or DELETE targeting `audit_log`
- [ ] Tenant placeholder resolves header → env → `DEFAULT_TENANT_ID`, attaches the value to `request.state.tenant_id`, and **never rejects a request** for tenant reasons; its docstring states plainly that this is not authentication and provides no isolation
- [ ] No JWT, Supabase, or `CREATE POLICY` code appears anywhere in the diff
- [ ] `.venv/bin/python -m pytest tests/test_connectors_api.py tests/test_audit.py` passes with `DATABASE_URL` unset (integration-marked Postgres tests skip)
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` passes with no regression to the shipped suite

> Use `.venv/bin/python -m pytest ...` throughout. Bare `pytest` is not on PATH, and the console script does not put the repo root on `sys.path`.

---

## Dependencies

- [ ] Both connectors shipped (features 5, 6) — SHIPPED. Need real connectors to display status.
- [ ] **Feature 10 (resolution-graph-update) — NEW HARD DEPENDENCY.** Feature 10 owns standing up the Postgres path (driver pin, `DATABASE_URL`, `core/graph/pg.py`, `scripts/migrate_pg.py`) and is the feature that first applies migration 001, which creates both `audit_log` and `connectors`. Feature 16 has no table to write to until 10 lands. **Feature 10 is currently BLOCKED, and the queue row for 16 lists dependencies `5, 6` only — it must be updated to `5, 6, 10`, and 16 must be ordered after 10.**
- [ ] ~~Canonical schema (feature 2) — audit_log and connectors tables~~ **FALSE as stated.** Feature 2 delivered `db/schema_sqlite.sql`, which has neither table. The Postgres DDL it also wrote has never been executed. Superseded by the feature 10 dependency above.
- [ ] ~~Supabase Auth configured — JWT tokens for tenant extraction~~ **REMOVED.** No auth exists; feature 16 does not build it. See Owner Decision 1 and FOLLOW-UP 16-A.

---

## Estimated Complexity

**Rating:** L (raised from M on 2026-08-22)

**Rationale:** Larger than the original M because none of it is an extension. Two dashboard pages written from empty placeholders, **plus a Dash application shell that does not exist today** and without which the pages cannot be rendered or tested; three API endpoints in a stub router; two middleware components created from 4-line stubs; first-ever router registration in `api/main.py`; and the first application code in the tree to write to Postgres, which lands on feature 10's freshly-built connection path and inherits its `DATABASE_URL`-gated integration test pattern. The audit middleware must intercept every request without meaningful latency — batch or background the INSERT rather than blocking the response.

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

### Audit Log Schema

Postgres DDL, `db/schema.sql:106` / `db/migrations/001_canonical_schema.sql:114`. Created by feature 10's migration runner; **absent from `db/schema_sqlite.sql`**.

```sql
audit_log (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    actor_id UUID,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    resource_id TEXT,
    category TEXT,  -- system category (accounting, psa, etc.)
    diff JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

`tenant_id` is populated from the placeholder resolver, and `actor_id` is NULL for every row this feature writes — there are no users until feature 17.

### V1 Hard Constraints

- Audit log: append-only, no UPDATE/DELETE — enforced here by writer discipline (INSERT-only) and a test, not by a database trigger or REVOKE.
- Every database query RLS-scoped to tenant_id — **`[PLANNED]`, not achieved by this feature.** Queries here carry a placeholder tenant_id as a filter; no row-level security exists.
- OAuth tokens encrypted at rest with customer-specific keys — **`[PLANNED]`**, and out of scope here.

### Relevant Spec Sections

- Section 14: Product UI — Connectors page, Audit Log
- Section 8: System Architecture (principles 4, 5: idempotency, audit trail)
- Rules `.claude/rules/01-nexus-finance-v1.md` §0 (BUILT vs PLANNED), §10 (data security posture, per-line status)
