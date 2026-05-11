# Feature Brief: Connectors Page + Audit Log + System Infrastructure

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** Infrastructure

---

## Problem Statement

The product needs system management surfaces: a connectors page showing connected systems grouped by category with sync status, and an audit log showing every action the system has taken. The audit log is a compliance requirement — every resolution, every approval, every sync must be traceable. The connectors page is the GTM entry point — the first thing a new customer interacts with after signup.

---

## Scope

### In Scope

- Update `dashboard/pages/connectors.py`:
  - Connected systems grouped by category (Accounting: QB ✓ | PSA: RUDDR ✓)
  - Per-connector status: connected, last sync timestamp, entity count, error count
  - Connect / Disconnect buttons triggering OAuth flow
  - Manual sync trigger button per connector
  - Future connector slots shown as "Coming Soon" (Bill.com, Stripe, Gusto)

- Update `dashboard/pages/audit_log.py`:
  - Filterable table: timestamp, actor (system | user_id), action, resource type, resource_id, category, diff summary
  - Filters: date range, action type, resource type, category, actor
  - Append-only display — no edit or delete UI actions
  - Paginated, sorted by timestamp desc

- Create `api/routers/connectors.py` endpoints:
  - `GET /connectors/` — list connected systems for tenant with status
  - `POST /connectors/{provider}/sync` — trigger manual sync
  - `GET /connectors/{provider}/status` — last sync details, error log

- Extend `api/middleware/audit.py`:
  - Middleware that logs every API action to audit_log table
  - Fields: tenant_id, actor_id, action, resource, resource_id, category, diff (JSONB), timestamp
  - Append-only — no UPDATE/DELETE on audit_log table

- Extend `api/middleware/tenant.py`:
  - Extract tenant_id from JWT token
  - Inject tenant_id into every database query via RLS context
  - Reject requests with missing or invalid tenant_id

- **Test suite:** `tests/test_connectors_api.py`, `tests/test_audit.py`
  - Assert: connector list returns only current tenant's connectors
  - Assert: audit log entries created for every API action
  - Assert: audit log is append-only (no UPDATE/DELETE operations)
  - Assert: tenant middleware rejects cross-tenant access

### Out of Scope

- OAuth flow implementation (handled in QB/RUDDR connector features)
- Webhook receiver endpoints
- Connector health monitoring / alerting (Sentry handles errors)
- Connector configuration UI (API keys, custom field mapping)

---

## Success Criteria

- [ ] `dashboard/pages/connectors.py` renders connected systems grouped by category
- [ ] `dashboard/pages/audit_log.py` renders filterable audit log table
- [ ] Connector status shows last sync time, entity count, errors
- [ ] Manual sync trigger works via API endpoint
- [ ] Audit middleware logs every API action automatically
- [ ] Audit log entries include category field
- [ ] Tenant middleware enforces RLS on all endpoints
- [ ] Tenant middleware rejects requests with missing tenant_id
- [ ] `pytest tests/test_connectors_api.py tests/test_audit.py` passes

---

## Dependencies

- [ ] Both connectors shipped (features 5, 6) — need real connectors to display status
- [ ] Canonical schema (feature 2) — audit_log and connectors tables
- [ ] Supabase Auth configured — JWT tokens for tenant extraction

---

## Estimated Complexity

**Rating:** M

**Rationale:** Two dashboard pages (moderate Dash), three API endpoints (straightforward), two middleware components (tenant RLS + audit logging). The audit middleware must intercept every request without performance degradation — needs async logging.

---

## PROJECT CONTEXT

### Dashboard Navigation (from spec Section 14)

```
Sidebar
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

### V1 Hard Constraints

- Audit log: append-only, no UPDATE/DELETE
- Every database query RLS-scoped to tenant_id
- OAuth tokens encrypted at rest with customer-specific keys

### Relevant Spec Sections

- Section 14: Product UI — Connectors page, Audit Log
- Section 8: System Architecture (principles 4, 5: idempotency, audit trail)
