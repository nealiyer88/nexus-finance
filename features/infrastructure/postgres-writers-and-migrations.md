# Feature Brief: Postgres Writers and Migration Execution — Tenants, Approvals, Audit, Integration Tier

**Author:** Neal Iyer
**Date:** 2026-08-23 (split out of feature 10a: this is the half that requires a live database)
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10c

---

## Problem Statement

Feature 10a stands up the Postgres path **offline**: a pinned driver, `core/graph/pg.py`, `pytest.ini` with a registered `integration` marker, a migration runner, the manifest, and the disposition mapping. Nothing has ever been executed against a server, and the two records the product treats as non-negotiable — a durable record of every approval decision, and an append-only audit trail of every resolution — still have nowhere to go. `approval_decisions` and `audit_log` live in the Postgres schema and only there: `grep -c 'CREATE TABLE.*\bapproval_decisions\b'` and the same for `audit_log` are non-zero in `db/schema.sql` and zero across `db/schema_sqlite.sql` and `db/migrations/*_sqlite.sql`.

This feature runs the migrations for real, seeds the bootstrap tenant, ships the writers, wires them into the shipped Stage 6 resolution path, and stands up the integration test tier that proves it. **It requires a reachable Postgres via `DATABASE_URL`, and per the F3 gate below it may not ship on a skip-only run.**

---

## The database target — decided, not to be re-litigated

**Postgres for this repo is whatever `DATABASE_URL` points at, and nothing here is allowed to care which server that is.**

- **Minimum Postgres major version 17** — a minimum major version, **never a pinned patch version** anywhere. `gen_random_uuid()` is a built-in there, so no `CREATE EXTENSION` step is required.
- **The target is supplied entirely by the environment.** No module, test, comment, fixture, docstring, printed line, or sentence of this brief may contain a host, a port, a database name, or a username; may build a DSN out of parts; or may assume the DSN carries a password component. Anything that would have to change when the DSN changes is a defect. Provisioning is not this feature's business: no Docker, `initdb`, `testcontainers`, CI service container, or server-start instructions. **All connections are opened through 10a's `connect()` and nothing else**, so the connect-timeout and DSN-handling contracts are inherited rather than restated.
- **Absence of `DATABASE_URL` means "Postgres not available" — never an error.** Integration tests skip; the default contributor path stays green with no database.

### SECRET HANDLING — hard requirement

`DATABASE_URL` is a live credential at all times, **whether or not the current value happens to carry a password**, and no code path may branch on which. Without exception it is **never logged** at any level or in any `print`; **never echoed in an error message** (driver exceptions embedding connection parameters are caught and re-raised scrubbed); and **never written to any file under `features/` or `.rocket/`**, nor into a fixture, snapshot, log, migration, or committed config. The value and every component it may carry appear **nowhere in this brief** by design.

---

## Scope

### Cross-feature ownership note (read first)

This feature **edits `core/graph/resolution.py`, which is feature 10's shipped file**. That is deliberate and in scope, and is described here so a reviewer reads it as an ownership edit rather than scope creep.

**The added call site must be token-clean.** Feature 10's shipped suite contains a grep-guard over its own modules that fails on any Postgres-related **token**. It matches tokens, not intent — so an edit that reaches Postgres **only through 10a's availability helper** passes it cleanly, while a perfectly correct edit that merely *mentions* the database in a comment fails it. Therefore:

- The call site reaches the database **exclusively via the availability helper and the writer functions**. No driver import, no environment read, and no DSN handling appears in feature 10's shipped module. Feature 10's module takes **one import from 10a**, and that is the whole of the coupling.
- **No identifier, string literal, or comment inside any file that guard covers may contain any token the guard forbids.** The forbidden token set and the covered path set are **derived at build time by reading the shipped guard's own regex and path tuple** — deliberately **not copied into this brief**, because a copy goes stale the moment the guard is widened.
- The write-ordering rationale is documented in **this feature's new modules**, never in feature 10's shipped ones.
- **Decision: that guard is NOT retired. It genuinely survives**, because indirection through the availability helper introduces none of the tokens it matches. A criterion below asserts it **still passes unmodified**.

Feature 10a already retired the requirements-diff guards in the commit carrying the driver pin. **This feature retires nothing and adds no dependency pin.**

### In Scope

- **Execute the migrations.** Run 10a's `scripts/migrate_pg.py` against the configured database and prove the resulting `schema_migrations` state matches the manifest exactly. The runner's selection rule, fail-closed behaviour, destructive-file guard, and runner-level idempotency are 10a's contract; this feature verifies them **live**.

- **Tenant-bootstrap migration.** `tenants` is declared in `db/schema.sql`; read its DDL by grep at build time rather than trusting a transcription. **Do not assert a count of referencing columns** — earlier queued features keep adding them. Derive the set instead: every column in `db/schema.sql` carrying a `REFERENCES` clause naming `tenants` and its primary key. Match on that reference, not on a fixed spelling of the whole column definition — the DDL aligns types with runs of whitespace, so any pattern assuming single spaces finds nothing. Nothing anywhere creates a `tenants` row today, so the first real insert on any of those tables FK-fails. This feature closes that:
  - A new tenant-bootstrap migration, at the **next free numeric prefix computed over every file in `db/migrations/`, `_sqlite` siblings included** — 10b's SQLite-only migration owns a prefix with no Postgres counterpart, so computing over the Postgres subset alone collides with a shipped file. **This brief writes no number and no migration filename.** Invariant: after this feature lands, **no two files in `db/migrations/` share a numeric prefix unless they are a dialect pair of the same migration** — one filename is the other's `_sqlite` sibling.
  - It is appended **last** in `db/migrations/postgres.manifest`, and inserts the bootstrap row: `INSERT INTO tenants (id, name, slug) VALUES ('<BOOTSTRAP_TENANT_ID>', 'Bootstrap Tenant', 'bootstrap') ON CONFLICT (id) DO NOTHING;` — re-running applies nothing and destroys nothing. Grep-assert at build time that `tenants` is **not** named in the canonical-schema migration's `DROP TABLE ... CASCADE` block, so this seed survives that block.

- **`core/graph/tenants.py` — `resolve_or_create_tenant(conn, tenant_id, name, slug) -> UUID`.** An `INSERT ... ON CONFLICT (id) DO NOTHING` plus `SELECT`. **DEFECT FIX: `name` and `slug` are required parameters with no defaults.** Verify against `db/schema.sql` at build time: `tenants.name` is `TEXT NOT NULL` and `tenants.slug` is `TEXT NOT NULL UNIQUE`. An earlier draft gave both as `None`-defaulted optionals, which cannot produce a valid insert — every caller must supply them, and no writer may pass `None` for either.
  - **RISK — UNIQUE slug collision.** `slug` is unique across the whole `tenants` table, so two customers whose company names normalize to the same slug collide: the second insert raises a `UniqueViolation` on a *different* `id` than the one conflict-handled by `ON CONFLICT (id)`. That violation must surface as a clear error naming the colliding slug (never the DSN) rather than being swallowed, and callers must not retry it blindly. Slug de-duplication (suffixing, per-tenant namespacing) is a **future signup/onboarding concern and is out of scope here**; this feature only guarantees the collision is loud.
  - **NULL-tenant fallback, stated explicitly.** Feature 10's `resolve_match` carries `tenant_id: Optional[str] = None` and SQLite fixtures load NULL, while the Postgres columns are `UUID NOT NULL REFERENCES tenants(id)`. Every writer here therefore **falls back to `BOOTSTRAP_TENANT_ID` when the caller's `tenant_id` is `None`**, and calls `resolve_or_create_tenant` when it is not. Without this fallback the default path FK-fails on every write. This is the most likely first-run failure.
  - **Downstream contract:** feature 16's `DEFAULT_TENANT_ID` binds to `BOOTSTRAP_TENANT_ID` by import and asserts **identity**; 10a owns that constant and keeps it importable with no database. Feature 16 may assume the row exists after the runner has run, and creates no tenants itself.

- **Writers.**
  - `core/graph/audit.py` — `log_resolution(conn, canonical_id, incoming_entity_raw, match_type, confidence, signals, category_pair, user_id, tenant_id)`. **INSERT-only** into `audit_log`, one row per resolution decision. The module contains no UPDATE and no DELETE path. V1 append-only is enforced **in code**: `db/schema.sql`'s "append-only" line is a SQL comment, not a trigger, `REVOKE`, or `RULE`.

    **`actor_id` is left NULL — mandatory, not optional.** `audit_log.actor_id` is typed `UUID`, while the in-tree actor value reaching Stage 6 is a plain string (feature 12's orchestrator passes its system-actor constant through feature 10's resolution module). Passing that string through would raise on insert. Therefore `log_resolution` **inserts NULL into `actor_id`**, and the human-readable actor, if retained at all, goes into a TEXT-typed column or is dropped. Do not coerce, do not hash into a UUID. **Widening `actor_id` is a feature-2 schema change and is out of scope.**
  - `core/graph/approvals.py` — `record_approval_decision(conn, ...)` writing `approval_decisions`, whose `disposition` column carries a `CHECK` constraint. It imports 10a's `MAPPING` and adds no vocabulary of its own. **The terminal vocabulary is 10a's decided contract** — the `status` CHECK from 10b's pending-decisions SQLite migration minus its non-terminal value, parsed at build time from the migration located by listing `db/migrations/`. No value is transcribed into this brief, into code, or into a test. **No producer-set assertion of any kind is made here.** 10a's mapping covers the full terminal set — its keys equal that set and it is non-empty — and this feature consumes it as-is: it adds no key, drops none, and invents no producer. Whether a CHECK value has an in-tree producer is irrelevant to the mapping. Nothing here edits 10b's migration, module, or tests, or `tests/test_schema_parity.py`'s shared-table literal — 10b's table is SQLite-only, gets no Postgres mirror and no manifest entry.

- **Stage 6 call site.** Both writers are called from feature 10's shipped resolution module **behind the availability helper**: when `DATABASE_URL` is unset the calls are no-ops and Stage 6 continues on SQLite exactly as it does today. Feature 12's `core/matching/engine.py` is the shipped Stage 6 dispatcher and the caller context (see Pipeline position); **this feature does not edit `core/matching/engine.py`** — the writers hang off the resolution module the engine already calls, so the engine inherits them with no change.

- **`tests/conftest.py` (NEW):** a `pg_conn` fixture that `pytest.skip`s with an explicit reason when `core.graph.pg.is_available()` is `False`, yields a connection wrapped in a transaction, and rolls back at teardown so integration tests leave no residue. **Rollback-at-teardown is mandatory, not hygienic** — the target is never assumed disposable, and a test that commits pollutes whatever database the developer or CI pointed at. The skip reason names the variable `DATABASE_URL` and never its value.

- **Split-store risk, owned here.** With this feature live, Stage 6 reads and mutates the graph in **SQLite** and writes approval/audit records to **Postgres** — two engines, no shared transaction, and a separate connection that can fail or time out independently. A crash between them can leave an approved alias with no audit row.
  - **Ordering:** write the Postgres audit row **first**, then the SQLite graph mutation. The failure mode becomes an audit row for a resolution that did not land (detectable and re-drivable) rather than a graph mutation with no record (undetectable). A connect timeout on the Postgres write must fail **before** the SQLite mutation, preserving the ordering guarantee rather than silently skipping it.
  - **Re-drive safety:** feature 10 already makes `resolve_match` idempotent on `(canonical_id, value, source)` and on `(source_node, target_node, relationship)`, so re-driving is safe. This is **not** atomicity — nothing short of a single store gives that.

- **Reconciliation — `scripts/reconcile_stores.py`. The invariant is coverage, not count equality.** An earlier draft compared SQLite alias/edge row counts against Postgres `audit_log` row counts for equality. That is **false by construction and must not be built**: one resolution decision writes one audit row but multiple graph rows (and may write none on a repeat, since both writes are idempotent upserts). Counts diverge on a perfectly healthy system. The true invariant under write-Postgres-first ordering is **coverage in one direction only**: *every canonical entity touched by a Stage 6 graph mutation for a tenant has at least one `audit_log` row for that tenant.* For a given tenant and a watermark timestamp (default: the earliest `audit_log.created_at` for that tenant, so pre-feature rows are excluded):
  - Collect the distinct canonical identifiers in SQLite `entity_aliases.canonical_id` and in `entity_edges.source_node` / `entity_edges.target_node`, restricted to rows at or after the watermark and scoped to the tenant by joining to `canonical_entities.tenant_id` — neither alias nor edge rows carry a `tenant_id` column of their own; derive the join at build time from `db/schema_sqlite.sql`. Assert each identifier appears in at least one `audit_log` row for that tenant, against the TEXT column the writer populates with the canonical identifier; confirm which column that is against the writer at build time.
  - **Normalize timestamp formats across engines.** SQLite's `created_at` is TEXT in a `CURRENT_TIMESTAMP` format; the Postgres watermark is `TIMESTAMPTZ`. Derive both formats at build time and convert to a common UTC representation before comparing — a naive string comparison across the two is wrong.
  - The **reverse direction is expected and is not an error**: audit rows with no corresponding graph mutation are exactly the benign crash outcome the ordering was chosen to produce, and feature 16 writes provider-scoped audit rows that land in the same bucket. Report as an informational line; do not fail. Exit non-zero **only** on the uncovered-mutation direction, printing each uncovered identifier. Print no DSN on any path.

- **Tests:** `tests/test_pg_bootstrap.py` (live migration execution + tenant seed) and `tests/test_audit_pg.py` (writers + reconciliation), both `@pytest.mark.integration`, using the `pg_conn` fixture. Plus no-database tests for the writers' grep-level invariants.

### Out of Scope

- **Everything feature 10a owns:** the driver pin, `core/graph/pg.py`, `pytest.ini`, the runner and manifest authoring, the disposition mapping, and the requirements-diff guard retirement. This feature adds no dependency pin and retires no guard.
- **Any database-provisioning story.** No Docker, `docker compose`, `testcontainers`, `initdb`, server-install or server-start instructions, no CI service container.
- **Migrating the graph store to Postgres.** Every table in `db/schema_sqlite.sql` keeps its reads and writes on SQLite; derive that list by grepping its `CREATE TABLE` statements rather than asserting a count. This feature adds a *second* store for approval + audit records only.
- **Mirroring 10b's pending-decisions table into Postgres**, any back-fill of pending rows into `approval_decisions`, and any edit to 10b's module, migration, tests, or the parity test's shared-table literal.
- **Any edit to `core/matching/engine.py`**, and any change to feature 10's Postgres-token grep-guard.
- **Rewriting any shipped migration** to remove its `DROP TABLE ... CASCADE` block; **widening `audit_log.actor_id`** or any other `db/schema.sql` type change; **down-migrations** (forward-only).
- **Slug de-duplication / collision resolution** for `tenants.slug` — a signup/onboarding concern.
- **Database-enforced append-only** on `audit_log` (trigger / `REVOKE` / `RULE`) — V1 enforcement is code-level; **row-level security** — no `CREATE POLICY` exists anywhere and writes carry `tenant_id` as a value; **connection pooling, ORM, async, or any vendor SDK** — `supabase==2.9.0` stays unimported.
- **Inventing an in-tree producer for any `disposition` CHECK value**, and the audit *middleware* / audit *dashboard page* — feature 16.

---

## Success Criteria

**Cross-feature ownership and the default no-database path:**

- [ ] **The Postgres-token guard survives and is not retired:** feature 10's shipped grep-guard over its own modules **still passes unmodified**. A test derives the guard's forbidden-token regex and covered-path set from the shipped guard itself and asserts no covered file matches. Neither the regex nor the path set is transcribed into this feature's code.
- [ ] With `DATABASE_URL` unset, the added Stage 6 calls are no-ops and Stage 6 behaves as feature 10 ships it: `.venv/bin/python -m pytest tests/test_resolution.py tests/test_matcher_orchestrator.py -x --tb=short` passes with a collected count greater than zero. (Confirm the orchestrator suite's filename by listing `tests/` at build time.)
- [ ] `.venv/bin/python -m pytest tests/ -m "not integration" -x --tb=short` exits 0 with `DATABASE_URL` unset **and reports a collected count greater than zero**, parsed from the same run's output. Separately, `.venv/bin/python -m pytest tests/ -x --tb=short` reports a collected count **greater than or equal to** the count from the same command on the prior commit — both captured at build time from the runs themselves, with no literal count written into any test.
- [ ] `git diff` for this feature's commit touches neither `core/matching/engine.py`, nor `requirements.txt`, nor 10b's module/migration/tests, nor the parity test — asserted from the commit's changed-path set.

**Migration execution (requires a reachable Postgres via `DATABASE_URL`):**

- [ ] `.venv/bin/python scripts/migrate_pg.py` exits 0 with `DATABASE_URL` set, and `SELECT to_regclass('public.approval_decisions')` and `SELECT to_regclass('public.audit_log')` both return non-NULL.
- [ ] **`schema_migrations` equals the manifest, derived at runtime — no hardcoded total.** The test parses the manifest (stripping blanks and `#` comments) into a list and asserts `SELECT filename FROM schema_migrations ORDER BY applied_at` returns that same list in that same order and nothing else.
- [ ] **SQLite-dialect migrations are provably not applied:** `SELECT COUNT(*) FROM schema_migrations WHERE filename LIKE '%\_sqlite%'` returns zero, and the runner's stdout mentions no file matching `*_sqlite.sql` (neither `applied` nor `skipped`) — the filename set derived by globbing, never named. This explicitly covers 10b's migration.
- [ ] **Manifest covers every Postgres migration on disk** (set comparison at test time), and the **prefix invariant** holds: no two files in `db/migrations/` share a numeric prefix unless one is the other's `_sqlite` sibling.
- [ ] **Fail-closed both ways:** a syntactically-invalid `.sql` file dropped into `db/migrations/` without a manifest entry leaves the runner exiting 0 with `schema_migrations` still equal to the parsed manifest; and with a manifest entry naming a nonexistent file the runner exits **non-zero**, names the missing filename, and applies nothing.
- [ ] **Runner-level idempotency (the only idempotency claimed):** a second immediate run exits 0, prints `skipped` for every manifest entry and `applied` for none, and leaves `SELECT COUNT(*) FROM schema_migrations` and a pre-seeded `SELECT COUNT(*) FROM canonical_entities` unchanged from the values captured after the first run (compare before/after, never against a literal). Seed a `canonical_entities` row before the second run and assert it survives.
- [ ] **Destructive-file guard:** on a database where `canonical_entities` exists but `schema_migrations` holds no row for the canonical-schema migration, the runner exits **non-zero**, prints a message naming that file, and applies nothing (row count unchanged from the value captured before the attempt).
- [ ] `scripts/migrate_pg.py --dry-run` exits 0 and creates no table.

**Tenant provisioning (requires a reachable Postgres):**

- [ ] After the runner completes, `SELECT COUNT(*) FROM tenants WHERE id = <BOOTSTRAP_TENANT_ID>` returns a row and the seeded value is byte-identical to `core.graph.pg.BOOTSTRAP_TENANT_ID`, asserted in the test rather than eyeballed.
- [ ] **A dependent insert referencing it succeeds:** an `audit_log` insert using `BOOTSTRAP_TENANT_ID` commits without an FK error, and the same row shape with a random unseeded UUID raises a `ForeignKeyViolation`.
- [ ] `resolve_or_create_tenant(conn, <fresh UUID>, name=..., slug=...)` returns that UUID, creates a `tenants` row where none existed, and a second call with the same UUID creates none and returns the same value (compare row counts before/after, not against a literal).
- [ ] **Signature matches the schema:** a no-database test asserts `resolve_or_create_tenant` has no default for `name` or `slug`, and grep-asserts against `db/schema.sql` that both columns are `NOT NULL` and that `slug` is UNIQUE.
- [ ] **Slug collision is loud:** calling `resolve_or_create_tenant` with a fresh `tenant_id` but a `slug` already held by a different row raises a `UniqueViolation` (or a wrapper naming the colliding slug) rather than returning silently, and the error message contains no DSN value. **NULL-tenant fallback:** a writer called with `tenant_id=None` inserts a row whose `tenant_id` equals `BOOTSTRAP_TENANT_ID` and does not raise.

**Writers:**

- [ ] `core/graph/audit.py` exists; `log_resolution` inserts one `audit_log` row per call — asserted as a before/after row-count delta equal to the number of calls the test makes, with `category` and `tenant_id` populated (integration). `grep -nE "\b(UPDATE|DELETE)\b" core/graph/audit.py` returns no match — append-only enforced in code.
- [ ] **No code path passes a non-UUID string into `actor_id`.** (a) a no-database test greps `core/graph/audit.py` and `core/graph/approvals.py` and confirms `actor_id` is only ever bound to `None`/SQL `NULL`; (b) an integration test calls `log_resolution` with a deliberately non-UUID actor string and asserts the insert succeeds with `actor_id` NULL.
- [ ] `record_approval_decision` inserts an `approval_decisions` row whose `disposition` satisfies the table's CHECK, for every key in 10a's `MAPPING`. A no-database unit test re-derives the CHECK set by grep over `db/schema.sql` and `TERMINAL_SET` from 10b's pending-decisions SQLite migration (located by listing `db/migrations/`, never by filename) minus the non-terminal value, then asserts `set(MAPPING.values()) <= CHECK_SET` **and** `set(MAPPING.keys()) <= TERMINAL_SET`. Both are subset assertions; **no set equality is asserted** and no set appears as a literal.
- [ ] `scripts/reconcile_stores.py` exits 0 on a healthy tenant **whose SQLite alias and edge row counts deliberately exceed its `audit_log` row count** (drive at least one resolution end to end) — proving coverage, not count equality. It exits non-zero with a per-identifier diff line when a mutated canonical identifier loses its last audit row, and exits 0 with an informational line only when an audit row exists for a resolution whose graph mutation did not land. Timestamps are compared after cross-engine normalization. (integration)

**Secret hygiene (scoped to this feature's own diff):**

- [ ] **The DSN never reaches logs or errors:** a grep over `core/graph/audit.py`, `core/graph/approvals.py`, `core/graph/tenants.py`, and `scripts/reconcile_stores.py` finds no path that logs, prints, or formats `get_dsn()`'s return value or the raw environment value into a message. A unit test monkeypatches `DATABASE_URL` to a sentinel, exercises every failure path reachable without a database, captures stdout/stderr and the exception messages, and asserts the sentinel appears in none of them.
- [ ] **The secret is not in the diff or in test output:** a check reads `DATABASE_URL` at run time and asserts the whole value, and each non-empty component it happens to carry, appears nowhere in this feature's commit diff, in any file under `features/` or `.rocket/`, or in the captured output of this feature's test runs. Components the value does not carry are skipped — the check never requires a particular component to be present, holds the secret only in memory, and writes it nowhere. When `DATABASE_URL` is unset it runs against the sentinel and still asserts absence.
- [ ] **Scoped to this feature's own diff, not the whole repo.** Across the lines **this feature adds** under `features/` and `.rocket/` — added lines only — no added line assigns a value to `DATABASE_URL`. The criterion deliberately does not grep the repo: other briefs already contain illustrative `DATABASE_URL=` text on a clean checkout. The substance is unconditional: the connection string is **never logged, never placed in an error message, and never written to any file under `features/` or `.rocket/`**.

**The integration tier must actually have run (F3 gate):**

- [ ] `DATABASE_URL=<from .env> .venv/bin/python -m pytest tests/test_pg_bootstrap.py tests/test_audit_pg.py -m integration -x --tb=short` exits 0 **and** its collected count is greater than zero, asserted from the same run's output.
- [ ] **This feature may NOT be marked SHIPPED while the number of integration-marked tests that actually EXECUTED is zero.** That number is read from the run's own collection-and-outcome output — integration-marked tests reporting a passed or failed outcome, i.e. collected-and-selected minus skipped and deselected. It is **never** compared against an expected total, a baseline, or any number written in this brief; the only assertion is *strictly greater than zero*. An all-skip run, a fully-deselected run, and a zero-collection run each fail this criterion regardless of exit code. **A skip is not a pass.**

---

## Dependencies

- [ ] **Feature 10a (postgres-store-bootstrap) — must be SHIPPED first.** It supplies the pinned driver, `core/graph/pg.py` (`get_dsn`, `connect`, `is_available`, `BOOTSTRAP_TENANT_ID`), `pytest.ini`'s registered `integration` marker, `scripts/migrate_pg.py`, `db/migrations/postgres.manifest`, and the disposition `MAPPING`. The coupling is **module imports only** — feature 10's shipped resolution module takes exactly one import from 10a (the availability helper), and this feature's writers import the mapping and the connect helper. Nothing here may rename, relocate, or database-couple `BOOTSTRAP_TENANT_ID`, `is_available`, `connect`, or the manifest's append-at-end semantics.
- [ ] **Feature 2 (canonical-schema) — SHIPPED**, in the narrow sense that it authored `db/schema.sql` and the canonical-schema migration; per rules §0 those are `[PLANNED]` and have never been executed. **This feature is the first to run them.** Feature 2 remains the owner of any type change to `db/schema.sql`, including `audit_log.actor_id`.
- [ ] **Feature 10 (resolution-graph-update) — SHIPPED.** It ships `core/graph/resolution.py`, the module this feature edits to add the Stage 6 call site, plus the idempotency guarantees that make re-drive safe. Its Postgres-token grep-guard survives unmodified.
- [ ] **Feature 12 (matcher-orchestrator) — SHIPPED, and the caller context.** It shipped `core/matching/engine.py`, the real Stage 6 dispatcher: `engine.match()` calls into feature 10's `resolve_match` / `create_new_entity`, passing its own system-actor constant and an optional `tenant_id`. That is where the plain-string actor and the `None` tenant originate, which is why `actor_id` is written NULL and why writers fall back to `BOOTSTRAP_TENANT_ID`. **This feature edits no file feature 12 owns** — the writers hang off the resolution module the engine already calls.
- [ ] **Feature 10b (pending-decision-persistence) — SHIPPED.** Its SQLite-only migration consumes a numeric prefix with no Postgres counterpart (so prefix computation spans the whole directory), and its `status` CHECK is the source of the terminal vocabulary 10a's mapping keys on. No Postgres mirror, no manifest entry, no edit to it or to the parity test's shared-table literal.
- [ ] **A reachable Postgres — PROVISIONED, by the developer, outside this feature.** Minimum major version 17 (no patch pin anywhere). Reached only via `DATABASE_URL` from a gitignored `.env`; this feature installs, starts, and containerises nothing, and adds no CI service container. Absent the variable the integration criteria skip — and per the F3 gate, a skip-only run cannot ship this feature.
- **Downstream:** feature 16 (`connectors-audit-infra`) binds `DEFAULT_TENANT_ID` to `BOOTSTRAP_TENANT_ID` by import and asserts identity, may assume the bootstrap row exists after the runner has run, appends its own migration filename to the manifest, uses the `pg_conn` fixture, and relies on the `integration` marker.

---

## Estimated Complexity

**Rating:** M

**Rationale:** The offline surface is already resolved by 10a; what remains is genuinely first-of-its-kind but narrow — the first migration ever executed, the first test tier requiring external infrastructure, and the first application code writing to a second engine whose location the code is forbidden to know. Risks priced in: more than one shipped migration is destructive on re-execution and is contained by the runner rather than rewritten, with the destructive-file guard the only safety net since the target is never assumed disposable; the split-store write path has no shared transaction and is mitigated by write ordering plus a *coverage* reconciliation; the audit/actor type mismatch is sidestepped by writing NULL; `tenants.slug` is UNIQUE, so name collisions across customers are a real failure this feature only makes loud; and a live credential is in play, so secret hygiene is a hard criterion rather than a convention.

---

## PROJECT CONTEXT

### Pipeline position

```
Stage 6 dispatch: core/matching/engine.py (feature 12, SHIPPED — unchanged by this feature)
  └─ calls feature 10's core/graph/resolution.py (SQLite graph writes)
       └─ if 10a's availability helper reports available (THIS FEATURE):
            1. Postgres: audit row          ← log_resolution()           (INSERT-only, written FIRST, actor_id NULL)
            2. Postgres: approval decision  ← record_approval_decision()
            3. SQLite:   graph mutation     ← feature 10's transaction
          else: steps 1-2 are no-ops; Stage 6 behaves exactly as features 10 and 12 ship it.
```

### Implementation Notes (constraints for the build)

1. **The database is whatever `DATABASE_URL` names.** Hardcode no host, port, database name, or username; build no DSN from parts; assume no password component. Provision nothing. Absence means *not available*, never an error. Minimum major version 17; never pin a patch. All connections go through 10a's `connect()`.
2. **Treat `DATABASE_URL` as a live secret regardless of what the value contains.** Never log it, never put it in an exception message, never write it under `features/` or `.rocket/`.
3. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`. A non-matching marker fails silently, not loudly — any criterion that "passes" by selecting zero tests is false signal, hence the collected-count assertions and the F3 gate on tests that actually *executed*.
4. **Do not put forbidden tokens into feature 10's shipped modules.** The call site reaches the database only through the availability helper and the writer functions; the forbidden-token set and covered-path set are read from the shipped guard's own regex at build time and never transcribed. Write-ordering rationale goes in this feature's modules.
5. **`tenant_id` mismatch between engines.** Postgres columns are `UUID NOT NULL REFERENCES tenants(id)`; the SQLite convention is a nullable `tenant_id TEXT` and fixtures load NULL. Writers fall back to `BOOTSTRAP_TENANT_ID` on `None` and call `resolve_or_create_tenant` otherwise.
6. **`resolve_or_create_tenant` requires `name` and `slug`** — both are `NOT NULL`, and `slug` is UNIQUE. No optional defaults; collisions surface loudly and are not resolved here.
7. **`actor_id` is UUID; the in-tree actor is a plain string. Write NULL.** Do not coerce, do not hash into a UUID, do not widen the column.
8. **Write Postgres before SQLite.** A timeout on the Postgres write must fail before the SQLite mutation, not skip past it. **Reconciliation is coverage, never count equality**, and must normalize timestamps across engines before comparing.
9. **Integration tests roll back at teardown.** The target is never assumed disposable; a committing test pollutes whatever database the runner was pointed at, and feature 16's live seam runs against the same one.
10. **Migration prefixes are shared with SQLite-only migrations.** Compute the next free prefix over the whole directory. Never write a number or a migration filename into a brief or a comment.
11. **Cite symbols, not coordinates.** No `file.py:NNN`, no counts, no "exactly N" claims about the tree in any code, comment, test, or log. Every quantitative check is an invariant computed at build or test time.

### V1 Hard Constraints (per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — remains the only store for canonical/alias/edge/system-reference data and 10b's pending rows. This feature moves none of it; it adds a second store for approval + audit records only.
- `[PLANNED → this feature]` Postgres at runtime: migration execution, the bootstrap tenant, `approval_decisions`, `audit_log`.
- `[PARTIAL] → code-enforced here` Audit log append-only. `audit.py` exposes INSERT only. The "append-only" wording in `db/schema.sql` is a **SQL comment** — grep-assert that file contains no `CREATE TRIGGER`, no `CREATE RULE`, and no `REVOKE` touching `audit_log`.
- `[PLANNED]` RLS and auth. No `CREATE POLICY` anywhere and no query filters on `tenant_id` — new writes carry it as a value. `supabase==2.9.0` stays unimported; `api/` remains stubs, consistent with `actor_id` staying NULL.

### Relevant Spec Sections

Section 8 (System Architecture — audit trail non-negotiable, idempotency everywhere); Section 9 (Stage 6 — Resolution + Graph Update); Section 10 / rules §10 (data security — append-only audit, tenant scoping, credential handling).
