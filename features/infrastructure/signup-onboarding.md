# Feature Brief: Self-Serve Signup + OAuth Onboarding Flow

**Author:** Neal Iyer
**Date:** 2026-05-10 (reality-corrected 2026-08-22)
**Status:** Approved
**Complexity:** XL (raised from L — see Estimated Complexity)
**FP&A Phase:** Infrastructure
**Feature #:** 17

---

## Problem Statement

The GTM motion requires self-serve: a customer signs up, connects QB via OAuth, connects RUDDR via API key, and receives their cross-category entity registry within 48 hours. Without this flow, every customer requires manual onboarding. This is the last V1 feature — it connects all prior features into a deployable product.

**Starting state (verified in tree 2026-08-22).** Per `.claude/rules/01-nexus-finance-v1.md` §0, anything not marked `[BUILT]` must be treated as NOT EXISTING. What that means for this feature specifically:

- **There is no authentication anywhere in the repo.** `api/routers/auth.py` is a docstring-only "Not yet implemented" stub with no executable body. `api/main.py` is a bare FastAPI app exposing `/health` and registering no routers. `supabase` is pinned in `requirements.txt` but is imported by nothing — grep-assert `grep -rn "import supabase\|from supabase" --include='*.py' .` (excluding `.venv`) returns nothing. **This feature is the first authentication surface in the product**; it does not extend one.
- **There is no tenant isolation.** `api/middleware/tenant.py` is a docstring-only stub today. Feature 16 replaces it with a request-scoped tenant **resolver placeholder** (`X-Nexus-Tenant` header → `NEXUS_TENANT_ID` env → `DEFAULT_TENANT_ID` constant) that its own brief states in terms is *not authentication and provides no isolation*: the header is unauthenticated, any caller may assert any tenant, and no request is ever rejected for tenant reasons. **Retiring that placeholder is in scope for this feature** — it is feature 16's FOLLOW-UP 16-A, explicitly blocked on 17.
- **There is no Dash application shell.** `dashboard/app.py` does not exist; the modules under `dashboard/pages/` call `dash.register_page` against an application object that is not there. **Feature 16 owns creating that shell** — this feature registers a page into it and must not create it.
- **There is no persistent credential store and no crypto.** Both connector modules define their own `TokenStore` Protocol and an `InMemoryTokenStore` that holds `AuthToken`s in a plain dict keyed by `(tenant_id, provider)`. No encryption library is pinned in `requirements.txt` and no KMS/crypto call exists in the tree (rules §10 marks encrypted-at-rest tokens `[PLANNED]`).
- **There is no task queue and no `workers/` package.** `celery` and `redis` are present in `requirements.txt` only as commented-out lines.
- **There is no billing, email, or deployment integration.** Grep the tree for `stripe`, `resend`, and `railway`: no dependency pin, no module, no config key.

---

## Scope

### Ownership boundary: who creates a tenant record (read first)

Feature 10a (`postgres-store-bootstrap`) and this feature both write `tenants` rows. The split is:

- **Feature 10a owns bootstrap-time provisioning.** It ships `db/migrations/<NNN>_bootstrap_tenant.sql`, which seeds exactly one fixed row — the UUID exported as the module-level literal `core.graph.pg.BOOTSTRAP_TENANT_ID` — via `INSERT ... ON CONFLICT (id) DO NOTHING`, applied by `scripts/migrate_pg.py` in manifest order. That row exists so the `NOT NULL REFERENCES tenants(id)` foreign keys on the Postgres tables have something to point at before any customer exists. 10a also ships the get-or-create helper `core/graph/tenants.py::resolve_or_create_tenant(conn, tenant_id, name=None, slug=None) -> UUID`.
- **This feature owns runtime signup-time provisioning** — the tenant record created when a real customer completes signup. It creates that row by **calling 10a's `resolve_or_create_tenant`**, not by writing its own `INSERT INTO tenants`. Grep-assert: `grep -rn "INSERT INTO tenants" api/ dashboard/ workers/ tests/` returns no match after this feature lands; the only such statement in the tree stays 10a's bootstrap migration.
- **Neither feature deletes or repurposes the other's row.** `BOOTSTRAP_TENANT_ID` remains a legitimate non-customer tenant after this feature lands; signup never reuses it, and no signup path may produce it as an id. Assert that explicitly (criterion below).

The `tenants` DDL is the authority on the shape a signup row must satisfy — parse the `CREATE TABLE ... tenants` block in `db/schema.sql` rather than quoting it. Two properties of that block are load-bearing here and must be derived by parsing, not assumed: `slug` carries a `UNIQUE` constraint, and `name` is `NOT NULL`. Slug collision is therefore a real runtime failure mode on signup and must be handled deliberately (see OPEN QUESTION 17-B).

### In Scope

- **Authentication, first surface in the product.** Replace the `api/routers/auth.py` stub:
  - `POST /auth/signup` — create the tenant via `resolve_or_create_tenant`, create the user identity, return a session credential
  - `POST /auth/login` — verify credentials, issue a session credential
  - `GET /auth/callback/quickbooks` — OAuth2 authorization-code callback; exchange the code and persist the resulting credential
  - `POST /auth/connect/ruddr` — accept, validate, and persist a RUDDR API key (a form POST, not an OAuth redirect callback — RUDDR issues no authorization code, so the `GET .../callback/ruddr` shape in the prior revision described a redirect that never happens)
  - Register the router in `api/main.py`, which registers no routers today.

  **The identity provider is not settled — see OPEN QUESTION 17-A.** The prior revision named Supabase Auth as a fact. It is not one: the pin is unimported and no Supabase project, key, or config path exists in the tree. Nothing below assumes a particular provider; every criterion is phrased against the *contract* (a signup creates a tenant and a user identity; a login yields a credential; a request carrying that credential resolves to exactly one tenant), so it holds whichever provider is chosen.

- **Retire feature 16's tenant placeholder — feature 16's FOLLOW-UP 16-A, discharged here.** This is the only point in the plan where the resolver becomes security, and 16's brief forbids anyone describing the system as tenant-isolated until it happens. In `api/middleware/tenant.py`:
  - Replace the header/env/constant precedence chain with tenant extraction from the verified session credential.
  - **Delete `DEFAULT_TENANT_ID` and the `X-Nexus-Tenant` header path** so no unauthenticated caller can assert a tenant. Grep-assert both are absent from `api/` after this feature.
  - A missing or invalid credential is a **rejection** (`401`), never a fallback to a default tenant.
  - Add the cross-tenant rejection tests feature 16 deliberately did not write: a credential for tenant A requesting a resource belonging to tenant B is refused.
  - Update the module docstring — 16's version states plainly that it is not authentication and provides no isolation. That wording must change here, and it must not change anywhere else before here.
  - **Still not RLS.** No `CREATE POLICY` is added by this feature. Enforcement is application-level filtering plus rejection. Rules §10 keeps RLS `[PLANNED]`; do not describe the result as row-level security.

- **A persistent, encrypted credential store.** Today's `InMemoryTokenStore` loses every credential on process exit and satisfies neither the `AuthToken` docstring's claim ("encrypted at rest with customer-specific keys") nor rules §10. This feature ships the first real implementation:
  - It satisfies the **existing `TokenStore` Protocol** already declared in both connector modules — `get(self, tenant_id: str, provider: str) -> Optional[AuthToken]` and `put(self, token: AuthToken) -> None`. Derive the exact Protocol by reading the connector modules at build time; do not re-declare a competing shape, and do not change the Protocol.
  - It round-trips every field of the `AuthToken` dataclass declared in `connectors/base.py` — derive the field set at build time via `dataclasses.fields(AuthToken)` rather than listing them here, and assert the round-trip against that derived set so a field added later is covered. The `extra` dict is the carrier for provider-specific context the QB connector needs (its constructor takes a `realm_id`, which the OAuth callback is the only thing that learns).
  - Backing store: the `connectors` table in `db/schema.sql`, whose `credentials JSONB` column and `UNIQUE (tenant_id, provider)` constraint are exactly this shape. Parse the `CREATE TABLE ... connectors` block to confirm both before building.
  - **Encryption is in scope and has no existing dependency.** This feature pins the crypto library and owns key handling. See OPEN QUESTION 17-C.
  - Both connectors keep working unmodified: they accept the store by injection (`token_store=`), so this feature adds an implementation and changes no connector code.

- **Onboarding wizard, as a page inside feature 16's Dash shell:**
  - Step 1: Sign up (email, password, company name)
  - Step 2: Connect QuickBooks (OAuth redirect)
  - Step 3: Connect RUDDR (API key input)
  - Step 4: Confirm connections, trigger historical data pull
  - Step 5: "Processing — your entity registry will be ready within 48 hours" status page
  - **Forward dependency, by contract not implementation:** `dashboard/app.py` exposing a module-level `app` built with `use_pages=True` and a `pages_folder` of `pages`, so that a new module under `dashboard/pages/` is picked up by `dash.register_page` with no edit to the shell. Feature 16 owns creating it. This feature adds its page module and **edits `dashboard/app.py` for auth gating only** — 16's shell explicitly excludes auth gating and leaves it to this feature.
  - Follow the structure contract 16 establishes for its own pages: expose `layout` as a **zero-argument callable** returning a component tree, and keep step logic in pure helpers that tests can call with fixture dicts, no browser and no live database. (Today's page placeholders assign `layout` as a plain `html.Div` value; 16 converts its own to callables.)

- **Background ingestion trigger.** After both connections are confirmed, run the historical seeding pipeline.
  - **Forward dependency, by contract:** feature 13 (`historical-cold-start`) exposes `core/ingestion/historical.py::seed_from_history(qb_connector, ruddr_connector, tenant_id)`. That signature — connector instances plus a tenant id, not a config blob — is what this feature calls. Verify it against 13's shipped module at build time; if it has drifted, adapt here rather than editing 13's module.
  - Status tracking: `queued → processing → complete → ready_for_review`, persisted per tenant so the Step 5 status page reads it rather than inferring it.
  - Error handling: retry on transient failures, mark permanent failures loudly and surface them on the status page rather than leaving a tenant stuck in `processing`.
  - **Execution substrate is undecided — see OPEN QUESTION 17-D.** The prior revision specified `workers/ingestion_worker.py`, which presumes a worker process and a queue. Neither exists: there is no `workers/` package, and the task-queue pins in `requirements.txt` are commented out. The module path is therefore not fixed by this brief; the *contract* is: signup confirmation enqueues exactly one seeding job per tenant, the job is observable by status, and it does not run inline on the HTTP request that confirms the connections.

- **Test suite:** `tests/test_onboarding.py`
  - Assert: signup creates a `tenants` row via `resolve_or_create_tenant` and a user identity bound to it
  - Assert: signup never returns or reuses `core.graph.pg.BOOTSTRAP_TENANT_ID`
  - Assert: the QB callback exchanges an authorization code and persists a credential that survives a fresh store instance
  - Assert: the RUDDR key is validated against the live-read contract and persisted
  - Assert: stored credentials are not readable as plaintext from the backing store
  - Assert: a request with no credential is rejected, not defaulted to a tenant
  - Assert: a credential for one tenant cannot read another tenant's rows
  - Assert: seeding is enqueued exactly once after both connections are confirmed, and not before
  - Postgres-touching assertions follow feature 10a's pattern: `@pytest.mark.integration`, skipped when `DATABASE_URL` is unset. Route, wizard-helper, and credential-shape tests require no database.

### Out of Scope

- SSO / SAML authentication — enterprise tier
- Team member invitations — V2
- Plan upgrades (Starter → Professional) — V2
- Custom domain / white-labeling
- RUDDR OAuth (they use API keys, not OAuth)
- **Row-level security.** No `CREATE POLICY` is added. Tenant enforcement here is application-level; rules §10 keeps RLS `[PLANNED]`.
- **QB token refresh.** The QB connector already implements the refresh-token grant against Intuit's token endpoint and drives it from `authenticate()`. This feature owns only the **authorization-code exchange** that produces the first credential; it does not reimplement or duplicate refresh.
- **The Dash application shell** (`dashboard/app.py`) — feature 16 creates it. This feature adds a page and the auth gate.
- **The migration runner.** Any DDL this feature needs ships as a migration file listed in `db/migrations/postgres.manifest`; feature 10a's `scripts/migrate_pg.py` applies it. Take the next unused numeric prefix at build time (`ls db/migrations/` and take max+1) — do not hardcode a number, since features landing before this one also add migrations.

---

## Success Criteria

- [ ] Complete signup → connect QB → connect RUDDR → processing flow works end-to-end against fixture-mode connectors
- [ ] Signup creates a tenant by calling `core.graph.tenants.resolve_or_create_tenant`; `grep -rn "INSERT INTO tenants" api/ dashboard/ workers/ tests/` returns no match, and the only such statement in `db/migrations/` remains feature 10a's bootstrap file (located by glob on `*_bootstrap_tenant.sql`, not by name)
- [ ] A signup response's tenant id is never equal to `core.graph.pg.BOOTSTRAP_TENANT_ID` — asserted by importing the constant, never by comparing against a UUID typed into the test
- [ ] Two signups with company names that normalize to the same `slug` both succeed, and the resulting tenant ids differ — the `UNIQUE` constraint on `slug` is satisfied by the collision strategy chosen in OPEN QUESTION 17-B, not by a 500
- [ ] OAuth tokens persisted per tenant and encrypted at rest: a test reads the raw backing-store value and asserts the plaintext access token does **not** appear in it, then asserts `TokenStore.get(tenant_id, provider)` on a **fresh store instance** returns an `AuthToken` equal to what was written, compared field-by-field over the set derived from `dataclasses.fields(AuthToken)` — no field list is written into the test
- [ ] RUDDR API key persisted under the same store and the same assertions
- [ ] RUDDR key validation performs a live read against the connector and rejects a key that fails it — a presence check is not validation; today's `authenticate()` only raises when the key is *absent*
- [ ] `api/middleware/tenant.py` contains no `DEFAULT_TENANT_ID` and no `X-Nexus-Tenant` handling after this feature; grep-assert both, in `api/` and `tests/`
- [ ] A request with no credential, and one with a tampered credential, each return `401` — never a fallback tenant. A credential issued for tenant A is refused when it addresses a resource whose `tenant_id` is B
- [ ] `api/middleware/tenant.py`'s module docstring no longer states that it provides no isolation, and no module in the diff describes the result as RLS or row-level security; `grep -rn "CREATE POLICY" .` (excluding `.venv`) returns no match
- [ ] `/health` still returns 200 and remains unauthenticated; every other registered route requires a credential — asserted by iterating `api.main.app.routes` and driving each `(method, path)` without one, so a route added later without a gate fails this test rather than shipping open
- [ ] Seeding is triggered exactly once after both connections are confirmed: the seeding entrypoint is monkeypatched to a recording double, and the recorded call count goes from zero (one connection confirmed) to one (both confirmed), and stays at one across a repeated confirmation
- [ ] The seeding call does not run inline on the request path — the recording double asserts it was not invoked during request handling, mirroring feature 16's structural (non-timing) assertion pattern for background work
- [ ] Status page reflects the persisted status value for the tenant across the full `queued → processing → complete → ready_for_review` progression, including the permanent-failure terminal state
- [ ] The completion notification's entity/variant figures are **computed from the seeded graph at send time** and are not literals in a template — grep the notification module for digits standing in for entity or variant counts and assert none. (The figures in PROJECT CONTEXT below are illustrative copy from the spec, not values to hardcode.)
- [ ] Shadow Ledger enforced: the OAuth scopes requested at signup are read-only. Assert against the scope string the authorization-URL builder emits, and assert no write-capable scope appears in it
- [ ] `import dashboard.app` succeeds and `dash.page_registry` contains this feature's onboarding path — membership asserted, never the size of the registry
- [ ] `.venv/bin/python -m pytest tests/test_onboarding.py -x --tb=short` exits 0 **and its collected count is greater than zero**, parsed from the same run's output. The exit code alone is not the assertion — under feature 10a's `pytest.ini`, a marker or path that selects nothing still exits 0, which is the silent-pass failure this criterion exists to catch
- [ ] `DATABASE_URL=... .venv/bin/python -m pytest tests/test_onboarding.py -m integration -x --tb=short` exits 0 against a freshly migrated database and its collected count is greater than zero
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` exits 0 with `DATABASE_URL` unset, and its collected count is greater than or equal to the baseline captured on the same checkout immediately before this feature's first commit — no shipped test drops out of collection. Capture the baseline at build time; do not write a literal into the test

> Use `.venv/bin/python -m pytest ...` throughout. Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` / `from connectors...` import fails to collect.

---

## Dependencies

- [ ] **All prior features shipped** — this is the integration point. The queue row is the authority on the exact list; verify it at build time rather than restating it here.
- [ ] **Feature 10a (`postgres-store-bootstrap`) — needed by contract:** `core/graph/pg.py` exposing `get_dsn()`, `connect()`, `is_available()`, and the module-level literal `BOOTSTRAP_TENANT_ID`; `core/graph/tenants.py` exposing `resolve_or_create_tenant(conn, tenant_id, name=None, slug=None) -> UUID`; `scripts/migrate_pg.py` applying `db/migrations/postgres.manifest` in order; `tests/conftest.py`'s `pg_conn` fixture; and `pytest.ini` registering the `integration` marker under `--strict-markers`. This feature adds no driver pin, no second connection path, and no migration runner.
- [ ] **Feature 16 (`connectors-audit-infra`) — needed by contract:** `dashboard/app.py` exposing a module-level `dash.Dash` app with `use_pages=True`, into which this feature's page registers; and `api/middleware/tenant.py` in its placeholder form, which this feature replaces. Feature 16's brief records this as FOLLOW-UP 16-A and states the placeholder must not be described as isolation until this feature lands.
- [ ] **Feature 13 (`historical-cold-start`) — needed by contract:** `core/ingestion/historical.py::seed_from_history(qb_connector, ruddr_connector, tenant_id)`. Confirm the signature against the shipped module at build time.
- [ ] **Features 5 and 6 (connectors) — SHIPPED.** Their `TokenStore` Protocol and injectable `token_store=` constructor parameter are the seam this feature's persistent store plugs into. No connector code changes.
- [ ] **An identity provider decision** — see OPEN QUESTION 17-A. Not a configured Supabase project; the provider itself is unchosen.
- [ ] **A billing decision** — see OPEN QUESTION 17-E. No billing dependency is pinned and no billing code exists.
- [ ] **A transactional email decision** — see OPEN QUESTION 17-F. Nothing in the tree sends mail.
- [ ] **A deployment target decision** — see OPEN QUESTION 17-G. No deployment configuration exists in the tree.
- [ ] **A reachable Postgres instance** for the integration tier, per feature 10a. Absent one, integration criteria skip — and a skip is not a pass. This feature cannot be marked SHIPPED on skips alone.

---

## OPEN QUESTIONS — require an owner decision before build

These are not implementation details to resolve at build time. Each is a product commitment nobody has made yet, and each was stated as settled fact in the prior revision.

**17-A — Identity provider.** The prior revision named Supabase Auth. The pin exists and is imported by nothing; no project, key, or config path exists in the tree. Options: adopt Supabase Auth for real (new config surface, hosted dependency, and it makes Supabase load-bearing in a product whose only runtime store is SQLite plus 10a's Postgres); or implement password auth and session tokens in-repo (no vendor, but this feature then owns password hashing, reset, and session revocation — meaningfully more scope). Nothing below the API surface should be built until this is answered.

**17-B — Slug collision on signup.** `tenants.slug` is `UNIQUE` and `name` is `NOT NULL`. Two customers named "Acme" produce the same slug and the second signup fails on the constraint. Decide: suffix-disambiguate, derive the slug from something already unique, or let the customer choose it and surface the conflict in the wizard. Whichever is chosen, the criterion above must hold.

**17-C — Credential encryption keys.** Rules §10 and the `AuthToken` docstring both promise "encrypted at rest with customer-specific keys." No crypto library is pinned and no key material exists anywhere. This feature must pin one and answer: where does the key live, is it truly per-customer or a single application key (the docstring says the former; the latter is much simpler and is what most V1s ship), and what is the rotation and recovery story? Shipping plaintext credentials to a real customer's QuickBooks is the highest-severity item in this brief.

**17-D — Background execution substrate.** No worker process, no queue, no scheduler. Seeding runs for up to 48 hours and cannot run on the request path. Options: pin the commented-out `celery`/`redis` stack; use a simpler in-process thread with the durability limits that implies (a restart loses in-flight seeding); or defer to whatever the deployment target offers once 17-G is answered. The module path `workers/ingestion_worker.py` from the prior revision presumed this answer.

**17-E — Billing.** The prior revision specified Stripe customer creation, a Starter subscription, and a webhook handler as in-scope. No Stripe dependency, key, or code exists. Two separate decisions are buried here: (a) is billing in V1 at all, or does the first cohort get invoiced manually — for a design-partner motion, manual is common and removes an entire integration from the last feature in the queue; and (b) if it is in V1, does signup take payment *before* provisioning, which changes the wizard's step order and the failure semantics of Step 1. Note also that the rules file lists `stripe` under the illustrative, explicitly out-of-scope-for-V1 `payments` category — adding it as a billing integration is not the same thing as adding it as a connector, but the collision is worth an explicit ruling.

**17-F — Transactional email.** The prior revision named Resend as a dependency and the flow's completion notification depends on sending mail. Nothing in the tree sends mail. Decide the provider, and decide whether the 48-hour completion email is V1-blocking or whether the status page alone is sufficient to ship.

**17-G — Deployment target.** The prior revision named Railway. No deployment configuration of any kind exists in the tree — no Dockerfile, no service definition, no CI deploy step. Deployment may be a separate feature rather than a clause inside this one; it is the only item here that has no code surface in `api/`, `core/`, or `dashboard/`.

**17-H — The 48-hour and 90-day promises.** "Registry ready within 48 hours" is customer-facing copy on the Step 5 status page, and "read-only for the first 90 days" gates when write permissions are requested. Neither has been measured against a real customer's data volume, and neither has an enforcement mechanism in the plan — nothing tracks a per-tenant 90-day clock. Confirm both as product commitments, and decide whether the 90-day gate needs a stored per-tenant start date in V1 or is operationally tracked.

---

## Estimated Complexity

**Rating:** XL (raised from L on 2026-08-22)

**Rationale:** The prior L assumed OAuth, Stripe, a worker, and a wizard sat on top of existing auth and tenancy. Neither exists. This feature is the first authentication surface in the product, the first tenant enforcement of any kind, the first persistent and encrypted credential store, and the first background execution substrate — none of it extends existing code, so none of it has a pattern to copy. It also carries a cross-feature ownership edit: it replaces feature 16's tenant middleware wholesale and adds an auth gate to feature 16's Dash shell, both of which 16 scopes out by design and hands here. Sizing further depends on unresolved product decisions (identity provider, billing-in-V1, encryption key model, execution substrate) that swing the estimate by more than the L/XL boundary on their own — which is why they are listed as open questions rather than priced in.

---

## PROJECT CONTEXT

### Onboarding Timeline (from spec)

- Minute 0: Signup
- Minute 5: QB connected via OAuth
- Minute 10: RUDDR connected via API key
- Hour 1–48: Historical data pulled, entities clustered, graph seeded
- Hour 48: Email notification — "Your entity registry is ready. N name variants across QB and RUDDR for M unique clients, unified for the first time."

The figures in that copy are **computed from the seeded graph at send time**, never templated as literals — see the corresponding success criterion. The spec's illustrative version of this sentence carries example numbers; they are copy, not a contract.

### Shadow Ledger Enforcement

- No write permissions requested at signup — enforced by the scopes in the authorization URL this feature builds, and asserted against that string
- All connections read-only for first 90 days
- Write permissions requested after 90 days of proven accuracy — no mechanism in the plan tracks this clock; see OPEN QUESTION 17-H

### Pricing

- Starter: $500/month — 2 system categories, up to 500 entities
- Professional: $1,500/month — 5 categories, unlimited entities, forecasting

Product pricing, not an implemented entitlement. Nothing in the tree enforces a category or entity ceiling, and this feature does not add one. Whether V1 collects money at all is OPEN QUESTION 17-E.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[PLANNED] → this feature` Auth. `api/routers/auth.py` is a stub and `supabase` is unimported. This feature is the first auth surface; the provider is unchosen (17-A).
- `[PLANNED] → partially this feature` Tenant scoping. Feature 16 ships an unauthenticated resolver placeholder that is explicitly not isolation; this feature makes it enforcement. RLS itself stays `[PLANNED]` — no `CREATE POLICY` is added.
- `[PLANNED] → this feature` Encrypted credentials at rest. Today's stores are plain in-memory dicts. Key model is 17-C.
- `[BUILT]` Connector auth seams — the `TokenStore` Protocol, the injectable `token_store=` parameter, the `AuthToken` dataclass, and QB's refresh-token grant all exist and are consumed, not modified, here.
- `[BUILT]` Shadow Ledger — `execute_write` returns a preview and is never a live mutation. This feature requests no write scope.

### Relevant Spec Sections

- Section 15: Go-to-Market Strategy (Shadow Ledger motion, pricing)
- Section 4: V1 Product Definition (self-serve signup)
- Rules `.claude/rules/01-nexus-finance-v1.md` §0 (BUILT vs PLANNED), §10 (data security posture, per-line status)
