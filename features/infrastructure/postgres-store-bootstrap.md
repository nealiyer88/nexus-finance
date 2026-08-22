# Feature Brief: Postgres Store Bootstrap — Driver, Migration Runner, Test Tier, Approval + Audit Writes

**Author:** Neal Iyer
**Date:** 2026-08-22
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10a (split out of feature 10; lands between 10 and 11)

---

## Problem Statement

`db/schema.sql` and `db/migrations/001_canonical_schema.sql` describe a Postgres store that **has never been executed anywhere**. Per `.claude/rules/01-nexus-finance-v1.md` §0, both are `[PLANNED]` and must be treated as not existing: no code imports a Postgres driver, `requirements.txt` pins none (`supabase==2.9.0` is pinned but never imported and is not a driver path), nothing in the tree reads a `DATABASE_URL`, and the only consumer of `schema.sql` is `tests/test_schema_parity.py`, which parses it **as text**. Two tables live there and only there: `approval_decisions` and `audit_log` — both are declared in `db/schema.sql` and in `db/migrations/001_canonical_schema.sql`, and in neither `db/schema_sqlite.sql` nor any `*_sqlite.sql` migration. Assert by symbol, not by line: `grep -c 'CREATE TABLE.*\bapproval_decisions\b'` and the same for `audit_log` are non-zero in `db/schema.sql` and `db/migrations/001_canonical_schema.sql`, and zero across `db/schema_sqlite.sql` and `db/migrations/*_sqlite.sql`.

The consequence is that the two records the product treats as non-negotiable — a durable record of every approval decision, and an append-only audit trail of every resolution — have nowhere to go. Feature 10 delivers the compounding graph on SQLite without them; features 11, 14, and 16 need them. Feature 16 (`connectors-audit-infra`) already declares a hard dependency on "the feature that stands up the Postgres path" — **that feature is this one, not 10**.

This feature stands up that path once, correctly, so every later feature inherits it: a pinned driver, a connection helper, a migration runner that is safe to re-run, a **registered** pytest marker for the database-requiring tier, and the first two writers (`approval_decisions`, `audit_log`).

Two defects found in the 2026-08-22 reality-check dry run of the combined brief are fixed here rather than carried forward — the unregistered `integration` marker (§Scope, Marker registration) and the false idempotency claim about migration 001 (§Scope, Migration runner). Both are called out explicitly below.

---

## Scope

### In Scope

- **Driver dependency.** Add exactly one pinned Postgres driver to `requirements.txt`: `psycopg[binary]==<pin>`. Verified 2026-08-22: `requirements.txt` contains no `psycopg`, no `asyncpg`, no `SQLAlchemy`. The binary wheel is chosen so no local `libpq` or C toolchain is required on Apple-Silicon dev machines or CI.

- **Connection configuration.** `core/graph/pg.py`, a single small module:
  - `get_dsn() -> str | None` — reads `DATABASE_URL` via `python-dotenv` (already pinned). Returns `None` when unset; never raises, never invents a default DSN.
  - `connect() -> psycopg.Connection` — raises a clear `RuntimeError` naming `DATABASE_URL` when the DSN is absent.
  - `is_available() -> bool` — used by test skip guards.
  - No pooling, no ORM, no engine abstraction.

- **Migration runner — `scripts/migrate_pg.py`.** Applies Postgres migrations against the configured database, recording each applied filename.

  **SELECTION RULE (the one mechanism; no other rule applies anywhere in this brief):** *The runner applies exactly the filenames listed in `db/migrations/postgres.manifest`, in the order they appear in that file, and never reads or applies any other file in `db/migrations/`.* The manifest (NEW, created by this feature) is a newline-delimited list of bare filenames; blank lines and `#` comment lines are ignored.

  **AUTHORING RULE for the manifest (resolves at build time; do not hardcode the list into this brief).** At build time, enumerate `db/migrations/*.sql` and author the manifest as: every file whose name does **not** end in `_sqlite.sql`, in ascending filename order, followed by this feature's own tenant-bootstrap migration last. Earlier queued features keep adding migrations here, so the list is derived once at build time and then frozen as a reviewed static file — the fail-closed property is preserved because nothing is ever applied that a human did not write into the manifest. Two invariants the manifest must satisfy, both mechanically checkable:
  - It contains **no entry matching `_sqlite`**. Every `*_sqlite.sql` file in `db/migrations/` is a SQLite mirror (feature 9 shipped `002_llm_training_data_sqlite.sql`; feature 8b shipped `003_transactions_sqlite.sql`) and is loaded only by SQLite tests, never applied to Postgres.
  - It contains, at minimum, every non-`_sqlite` `.sql` file present in `db/migrations/` at build time — assert as a set comparison at build time, not as a count.

  If the manifest names a file that does not exist on disk, the runner exits non-zero before applying anything.

  **MIGRATION NUMBERING — avoid prefix collisions with upstream features.** Feature 8b has already taken the `003_` prefix (`003_transactions.sql`). This feature's tenant-bootstrap migration therefore takes the **next unused numeric prefix at build time**, computed as one above the highest numeric prefix present in `db/migrations/` — as of 2026-08-22 that is `004_bootstrap_tenant.sql`, and this brief refers to it as `<NNN>_bootstrap_tenant.sql` everywhere below. If feature 10 (or any feature landing first) has already claimed `004_`, take the next free number instead; the file's number is not load-bearing, only its position **last in the manifest** is. Criterion: after this feature lands, no two non-`_sqlite` files in `db/migrations/` share a numeric prefix.

  **Why a manifest rather than a naming convention or a directory split:** it is fail-closed and opt-in — a migration file added later is invisible to Postgres until someone lists it, so a future SQLite-only or dialect-ambiguous file can never be applied by accident no matter how it is named, and the worst failure mode is a migration that visibly did not run rather than a wrong-dialect one that did. (A directory split was rejected because `tests/test_schema_parity.py` parses `db/migrations/001_canonical_schema.sql` at its current path.)

  - Creates `schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())` **before** applying anything.
  - **Skips any file already recorded.** A file is applied at most once, ever.
  - **DESTRUCTIVE-FILE GUARD (defect fix — see below).** Before applying `001_canonical_schema.sql`, the runner checks whether `canonical_entities` already exists while `schema_migrations` has no row for `001`. If so it **refuses to run**, exits non-zero, and prints the reason. This is the case where a database was created some other way and re-running 001 would silently destroy it.
  - Prints one line per file (`applied` / `skipped`) and exits 0 on success.
  - `--dry-run` prints the plan and applies nothing.
  - Each Postgres-side migration shipped by an earlier feature (e.g. `002_llm_training_data.sql`, `003_transactions.sql`) is applied by the same runner because the manifest lists it; each `_sqlite` sibling is not listed and is not applied.

  **DEFECT FIX — "idempotent on second run" was false and is not repeated here.** `db/migrations/001_canonical_schema.sql` opens, inside a single `BEGIN`, with an unconditional `DROP TABLE IF EXISTS ... CASCADE` block naming `approvals`, `entities`, `approval_decisions`, `system_references`, `entity_edges`, `entity_aliases`, `canonical_entities`, and `audit_log`. Its own comment calls this "idempotent migration"; it is not — re-executing 001 **drops every table that block names, and every row in them**. Only the `CREATE TABLE` half is `IF NOT EXISTS`-guarded; the `DROP` half is unconditional. Assert by symbol: `grep -E '^DROP TABLE IF EXISTS' db/migrations/001_canonical_schema.sql` returns a non-empty set, and that set is exactly the tables named above.

  **Resolution chosen: the runner never re-runs an applied file, and 001 is left byte-unchanged.** Idempotency is redefined at the **runner** level and stated that way in every criterion: *a second `migrate_pg.py` run applies zero files and exits 0.* It is explicitly **not** claimed that re-executing 001 is safe. 001 is not rewritten because `tests/test_schema_parity.py` parses it and `db/schema.sql` as text and parity-checks table shapes across the SQLite mirror; reworking the DROP block is a schema-authoring change that belongs to feature 2's owner, not to a bootstrap feature.

  **DATA-LOSS HAZARD — flag prominently in `scripts/migrate_pg.py`'s module docstring and in `README`-adjacent notes:** running `psql -f db/migrations/001_canonical_schema.sql` by hand, or deleting the `schema_migrations` row for 001 and re-running the runner, **destroys all canonical, alias, edge, system-reference, approval, and audit data**. The runner's guard defends the first-run case; it cannot defend a manual `psql` invocation.

- **Marker registration — `pytest.ini` at repo root (NEW).** Verified 2026-08-22: this repo has **no pytest configuration of any kind** — no `pytest.ini`, no `pyproject.toml`, no `setup.cfg`, no `tox.ini`, no `conftest.py` anywhere outside `.venv`. Consequences the dry run identified: `@pytest.mark.integration` is unregistered, so `-m integration` matches nothing, pytest exits **code 5 (no tests collected)**, and any criterion phrased as "the integration suite passes" either hard-fails on the exit code or silently passes as a no-op. Both outcomes are false signal. This feature creates:

  ```ini
  [pytest]
  testpaths = tests
  addopts = --strict-markers
  markers =
      integration: requires a live Postgres reachable via DATABASE_URL
  ```

  `--strict-markers` turns any future unregistered marker into a collection **error** rather than a warning. Adding a `pytest.ini` fixes pytest's rootdir at the repo root and sets `testpaths`/`addopts` for every future run. Note that rootdir is *not* what puts the repo root on `sys.path` — that comes from invoking `.venv/bin/python -m pytest`, which prepends the current working directory, so `from core...` imports keep working for the same reason they do today. The config still changes collection (rootdir, `testpaths`, `addopts`), so the full existing suite must be re-verified under it (criterion below).

- **Tenant provisioning — owned here, relied on downstream.** Verified 2026-08-22: `tenants` is declared in both `db/schema.sql` and `db/migrations/001_canonical_schema.sql` as `(id UUID PRIMARY KEY DEFAULT gen_random_uuid(), name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now())`. **Do not assert a count of referencing columns** — earlier queued features keep adding them (8b's `transactions.tenant_id` is the most recent). Derive the set instead: every column matching `tenant_id UUID NOT NULL REFERENCES tenants(id)` in `db/schema.sql` — at minimum `connectors`, `canonical_entities`, `system_references`, `transactions`, `approval_decisions`, and `audit_log`, plus any table added by an earlier queued feature. Nothing anywhere creates a `tenants` row today, so the first real insert on any of those tables FK-fails. This feature closes that:
  - `core/graph/pg.py` exposes `BOOTSTRAP_TENANT_ID` — a **fixed UUID literal**, not generated, so it is stable across databases and quotable by other features.
  - `db/migrations/<NNN>_bootstrap_tenant.sql` (NEW, next free numeric prefix — see MIGRATION NUMBERING above; `004_bootstrap_tenant.sql` as of 2026-08-22 — listed **last** in the manifest) inserts that row: `INSERT INTO tenants (id, name, slug) VALUES ('<BOOTSTRAP_TENANT_ID>', 'Bootstrap Tenant', 'bootstrap') ON CONFLICT (id) DO NOTHING;` — re-running applies nothing and destroys nothing. `tenants` is **not** named in 001's `DROP TABLE ... CASCADE` block (grep-assert: `grep -E '^DROP TABLE IF EXISTS +tenants\b' db/migrations/001_canonical_schema.sql` returns nothing), so this seed survives the destructive block.
  - `core/graph/tenants.py` — `resolve_or_create_tenant(conn, tenant_id, name=None, slug=None) -> UUID`, an `INSERT ... ON CONFLICT (id) DO NOTHING` + `SELECT`. Every writer in this feature calls it before inserting, so a tenant id that is not the bootstrap one still works.
  - **Downstream contract:** feature 16's `DEFAULT_TENANT_ID` constant **must be the same literal as `BOOTSTRAP_TENANT_ID`**, and feature 16 may assume the row exists after `migrate_pg.py` has run. Feature 16 does not create tenants.

- **`tests/conftest.py` (NEW):** a `pg_conn` fixture that `pytest.skip`s with an explicit reason when `core.graph.pg.is_available()` is `False`, yields a connection wrapped in a transaction, and rolls back at teardown so integration tests leave no residue.

- **Writers.**
  - `core/graph/audit.py` — `log_resolution(conn, canonical_id, incoming_entity_raw, match_type, confidence, signals, category_pair, user_id, tenant_id)`. **INSERT-only** into `audit_log`, one row per resolution decision. The module contains no UPDATE and no DELETE path. V1 append-only is enforced **in code**: `db/schema.sql`'s "append-only" line is a SQL comment, not a trigger, `REVOKE`, or `RULE`.
  - `core/graph/approvals.py` — `record_approval_decision(conn, ...)` writing `approval_decisions`. Maps the in-tree `Action` / terminal-outcome vocabulary onto that table's `CHECK (disposition IN ('approved','rejected','corrected'))` — read the allowed set out of `approval_decisions.disposition`'s CHECK constraint in `db/schema.sql` by grep rather than by line number — via an explicit, tested dict — the two vocabularies differ and the mapping must not be implicit.
  - Both are called from `core/graph/resolution.py` (shipped by feature 10) **behind an availability check**: when `DATABASE_URL` is unset the calls are no-ops and Stage 6 continues on SQLite exactly as it does today. Feature 10's own test suite must still pass untouched.

- **Split-store risk, owned here.** With this feature live, Stage 6 reads and mutates the graph in **SQLite** and writes approval/audit records to **Postgres** — two engines, no shared transaction. A crash between them can leave an approved alias with no audit row.
  - **Ordering:** write the Postgres audit row **first**, then the SQLite graph mutation. The failure mode becomes an audit row for a resolution that did not land (detectable and re-drivable) rather than a graph mutation with no record (undetectable).
  - **Re-drive safety:** feature 10 already makes `resolve_match` idempotent on `(canonical_id, value, source)` and on `(source_node, target_node, relationship)`, so re-driving is safe.
  - **Reconciliation:** `scripts/reconcile_stores.py` counts SQLite alias/edge rows against Postgres `audit_log` rows for a tenant and exits non-zero on divergence.
  - This is **not** atomicity — nothing short of a single store gives that. The split collapses when the graph store itself moves to Postgres, which is out of scope here.

- **Tests:** `tests/test_pg_bootstrap.py` (migration runner + connection helper), `tests/test_audit_pg.py` (audit + approval writers), both `@pytest.mark.integration`. Plus non-integration tests for the marker registration itself and for the `Action` → `disposition` mapping, which needs no database.

- **Local/CI Postgres story** documented in Implementation Notes: a `docker run postgres:16` one-liner, and the same image as a CI service container. Chosen over `testcontainers` because it adds no new test dependency and keeps `.venv/bin/python -m pytest tests/` green on a machine with no database — which every current contributor path assumes.

### Out of Scope

- **Migrating the graph store to Postgres.** Every table in `db/schema_sqlite.sql` keeps its reads and writes on SQLite — the four stable graph tables `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references`, plus `transactions` (added by feature 8b) **and any table added there by an earlier queued feature**. Do not assert a table count for `db/schema_sqlite.sql`; derive the list by grepping its `CREATE TABLE` statements. This feature adds a *second* store for approval + audit records only.
- **Rewriting `db/migrations/001_canonical_schema.sql`** to remove its `DROP TABLE ... CASCADE` block. Deliberate — see the resolution above. If the owner later wants true file-level idempotency, that is a separate schema feature that must also re-verify `tests/test_schema_parity.py`.
- **Down-migrations / rollback.** Forward-only.
- **Database-enforced append-only** on `audit_log` (trigger / `REVOKE` / `RULE`). V1 enforcement is code-level.
- **Row-level security.** No `CREATE POLICY` exists anywhere; zero queries filter on `tenant_id`. Writes carry `tenant_id` as a value.
- **Connection pooling, ORM, async.** One driver, one `connect()`.
- **Supabase hosting/auth.** `supabase==2.9.0` stays unimported.
- The audit *middleware* and audit *dashboard page* — feature 16, which consumes this feature's `audit_log` and `core/graph/pg.py`.

---

## Success Criteria

**Marker registration (no database required):**

- [ ] `pytest.ini` exists at repo root and registers `integration`; `.venv/bin/python -m pytest --markers` output contains a line starting `@pytest.mark.integration` — assert exit 0 and non-empty grep.
- [ ] `.venv/bin/python -m pytest tests/test_pg_bootstrap.py tests/test_audit_pg.py -m integration --collect-only -q` **exits 0 and reports ≥ 4 collected tests** — proving the marker actually selects tests rather than matching nothing (which would exit 5).
- [ ] `.venv/bin/python -m pytest tests/ -m "not integration" -x --tb=short` exits 0 with `DATABASE_URL` unset — the default contributor path stays green with no database.
- [ ] With `--strict-markers` active, a test decorated with a deliberately bogus marker causes a **collection error**, asserted in a subprocess test that expects a non-zero exit.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` exits 0 under the new `pytest.ini` — no shipped test regressed from the rootdir/`testpaths`/`addopts` change.

**Driver + connection (no database required):**

- [ ] `requirements.txt` gains exactly one new pinned line and deletes none: `git diff -U0 requirements.txt` shows exactly one added line, that line matches `^psycopg\[binary\]==`, and no removed lines. `.venv/bin/python -c "import psycopg"` succeeds after `pip install -r requirements.txt`. `grep -cE '^(asyncpg|SQLAlchemy|pg8000)' requirements.txt` returns 0 — one driver, no second path.
- [ ] With `DATABASE_URL` unset: `core.graph.pg.get_dsn()` returns `None`, `is_available()` returns `False`, and `connect()` raises `RuntimeError` whose message contains the literal string `DATABASE_URL`.

**Migration runner (requires a database):**

- [ ] Against an empty database, `DATABASE_URL=... .venv/bin/python scripts/migrate_pg.py` exits 0, and `SELECT to_regclass('public.approval_decisions')` and `SELECT to_regclass('public.audit_log')` both return non-NULL.
- [ ] **`schema_migrations` equals the manifest, derived at runtime — no hardcoded total.** The test parses `db/migrations/postgres.manifest` (stripping blanks and `#` comments) into a list, and asserts `SELECT filename FROM schema_migrations ORDER BY applied_at` returns **that exact list, in that exact order, and nothing else**. This stays PASS/FAIL as the manifest grows: `assert rows == manifest_entries`.
- [ ] **SQLite migrations are provably not applied:** after a full run, `SELECT COUNT(*) FROM schema_migrations WHERE filename LIKE '%\_sqlite%'` returns **0**. The runner's stdout contains **no** line mentioning any file matching `*_sqlite.sql` (neither `applied` nor `skipped` — those files are never read at all); the test derives that filename set by globbing `db/migrations/*_sqlite.sql` rather than naming files. Grep-assert `db/migrations/postgres.manifest` contains no `_sqlite` entry.
- [ ] **Manifest covers every Postgres migration on disk:** the set of non-`_sqlite` `.sql` files under `db/migrations/` equals the set of manifest entries — asserted as a set comparison computed at test time, so a migration an earlier feature added cannot be silently unapplied.
- [ ] **Fail-closed on an unlisted file:** a test drops a syntactically-invalid `.sql` file into `db/migrations/` without listing it in the manifest; the runner still exits 0 and `SELECT filename FROM schema_migrations` still equals the manifest list (the invalid file is absent) — proving selection is by manifest, not by directory scan. Assert against the parsed manifest, not a literal row count.
- [ ] **Fail-closed on a missing file:** with a manifest entry naming a nonexistent file, the runner exits **non-zero**, names the missing filename, and applies nothing.
- [ ] **Runner-level idempotency (the only idempotency claimed):** a second immediate run exits 0, prints `skipped` for every manifest entry, prints `applied` for none, and leaves both `SELECT COUNT(*) FROM schema_migrations` and a pre-seeded `SELECT COUNT(*) FROM canonical_entities` **unchanged from the value captured after the first run** (compare before/after, do not compare against a literal). Explicitly asserted: seed one `canonical_entities` row before the second run and assert it still exists afterward.
- [ ] **Destructive-file guard:** on a database where `canonical_entities` exists but `schema_migrations` has no row for `001_canonical_schema.sql`, the runner exits **non-zero**, prints a message containing `001_canonical_schema.sql`, and applies nothing (`canonical_entities` row count unchanged).
- [ ] `scripts/migrate_pg.py --dry-run` exits 0 and creates no table (`SELECT to_regclass('public.audit_log')` still NULL on an empty database).
- [ ] `scripts/migrate_pg.py`'s module docstring contains the literal words `DROP TABLE` and `data loss` — grep-asserted, so the hazard cannot be silently deleted.

**Tenant provisioning (requires a database):**

- [ ] After `migrate_pg.py` completes on an empty database, `SELECT COUNT(*) FROM tenants WHERE id = '<BOOTSTRAP_TENANT_ID>'` returns **1**, and that literal is byte-identical to `core.graph.pg.BOOTSTRAP_TENANT_ID` (asserted in the test, not eyeballed).
- [ ] **A dependent insert referencing it succeeds:** `INSERT INTO audit_log (tenant_id, action, resource) VALUES (core.graph.pg.BOOTSTRAP_TENANT_ID, 'test', 'test')` commits without an FK error, and `INSERT` of the same row shape with a random unseeded UUID raises a `ForeignKeyViolation` — proving the FK is live and the seed is what satisfies it.
- [ ] `resolve_or_create_tenant(conn, <fresh UUID>)` returns that UUID, creates exactly one `tenants` row, and a second call with the same UUID creates none and returns the same value.
- [ ] A no-database test asserts `core.graph.pg.BOOTSTRAP_TENANT_ID` parses as a valid UUID and is a module-level literal (no `uuid4()` call in `core/graph/pg.py` — grep-asserted), so feature 16's `DEFAULT_TENANT_ID` can quote it.

**Writers:**

- [ ] `core/graph/audit.py` exists; `log_resolution` inserts exactly one `audit_log` row per call, with `category` and `tenant_id` populated. (integration)
- [ ] `grep -nE "\b(UPDATE|DELETE)\b" core/graph/audit.py` returns no match — append-only enforced in code, not by a trigger.
- [ ] `core/graph/approvals.py` `record_approval_decision` inserts one `approval_decisions` row and its `disposition` value satisfies the table's CHECK for every input in the mapping table; a unit test (no database) asserts the mapping dict's values are a subset of `{'approved','rejected','corrected'}` and that every terminal `Action` has an entry. (mapping test: no database; insert: integration)
- [ ] With `DATABASE_URL` unset, `core/graph/resolution.py`'s calls into these writers are no-ops: `.venv/bin/python -m pytest tests/test_resolution.py -x` still exits 0 unchanged from feature 10.
- [ ] `scripts/reconcile_stores.py` exits 0 when SQLite alias/edge counts and Postgres `audit_log` counts agree for a tenant, and non-zero with a diff line when a row is deleted from `audit_log`. (integration)
- [ ] `DATABASE_URL=... .venv/bin/python -m pytest tests/test_pg_bootstrap.py tests/test_audit_pg.py -m integration -x --tb=short` exits 0 against a freshly migrated database.

---

## Dependencies

- [ ] **Feature 2 (canonical-schema) — SHIPPED**, in the narrow sense that it authored `db/schema.sql` and `db/migrations/001_canonical_schema.sql`. **Those files have never been executed** — per rules §0 they are `[PLANNED]` and must be treated as not existing. This feature is the first to run them.
- [ ] **Feature 10 (resolution-graph-update) — must land first.** It ships `core/graph/resolution.py`, the call site for `log_resolution` and `record_approval_decision`, and the decision payloads they persist. Feature 10 has no reverse dependency on this feature and is fully buildable and testable with no Postgres present.
- [ ] **A reachable Postgres 16 instance** for the integration tier — local Docker or a CI service container. Absent one, the integration criteria are **not verified**; they skip. A skip is not a pass, and this feature cannot be marked SHIPPED on skips alone.
- [ ] **`python-dotenv`** — already pinned in `requirements.txt`; no new pin needed for config loading.
- **Downstream:** feature 16 (`connectors-audit-infra`) currently names feature 10 as the owner of the Postgres path. That is now this feature — feature 16's dependency and queue row must point at **10a**. Feature 16 also inherits tenant provisioning from here: its `DEFAULT_TENANT_ID` must be set to this feature's `BOOTSTRAP_TENANT_ID` literal, and it must not create `tenants` rows of its own.

---

## Estimated Complexity

**Rating:** L

**Rationale:** Every item here is a *first* for this repo: the first Postgres driver in `requirements.txt`, the first `DATABASE_URL`, the first migration ever executed, the first pytest configuration file of any kind, the first test tier that requires external infrastructure, the first CI service container, and the first application code to write to a second engine. None of it extends existing code, so none of it has a pattern to copy. Two of the risks are already identified and priced in: migration 001 is destructive on re-execution and is being contained by the runner rather than rewritten, and the split-store write path between SQLite and Postgres has no shared transaction and is being mitigated by write ordering plus reconciliation rather than solved. The load-bearing risk is that adding `pytest.ini` changes rootdir and collection behavior for a suite that has never had a config file — hence the explicit full-suite re-verification criterion.

---

## PROJECT CONTEXT

### Pipeline position

```
Stage 6 Resolution (feature 10, SQLite)
    └─ if DATABASE_URL set (THIS FEATURE):
         1. Postgres  audit_log            ← log_resolution()          (INSERT-only, written FIRST)
         2. Postgres  approval_decisions   ← record_approval_decision()
         3. SQLite    graph mutation       ← feature 10's transaction
       else: steps 1-2 are no-ops; Stage 6 behaves exactly as feature 10 ships it.
```

### Implementation Notes (constraints for the build)

1. **Local/CI Postgres:**
   `docker run --rm -d -e POSTGRES_PASSWORD=nexus -p 5432:5432 postgres:16`
   `DATABASE_URL=postgresql://postgres:nexus@localhost:5432/postgres .venv/bin/python scripts/migrate_pg.py`
   CI uses the same image as a service container.
2. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` import fails to collect.
3. **`@pytest.mark.integration` is worthless without registration.** Before this feature there is no pytest config at all, so `-m integration` selects nothing and pytest exits 5. Any criterion that "passes" by selecting zero tests is false signal — that is why the criteria above assert a **collected count** and an exit code, not just "the suite passes".
4. **Never claim migration 001 is idempotent.** It is not: its leading `DROP TABLE IF EXISTS ... CASCADE` block runs unconditionally inside a `BEGIN`. Idempotency in this feature means *the runner does not re-run an applied file*. Keep that wording in code comments, help text, and any future brief that inherits this.
5. **`tenant_id` mismatch between engines.** In `db/schema.sql`, `approval_decisions.tenant_id` and `audit_log.tenant_id` are `UUID NOT NULL REFERENCES tenants(id)`, while the SQLite convention is a nullable `tenant_id TEXT` and fixtures load NULL — see `db/schema_sqlite.sql`'s `canonical_entities` DDL and the "Tenant scoping" paragraph of `core/graph/entity_store.py`'s module docstring, which states that reads default to no tenant filter. The writers must call `resolve_or_create_tenant()` before insert, or the NOT NULL FK rejects every write. `<NNN>_bootstrap_tenant.sql` seeds `BOOTSTRAP_TENANT_ID` so the default path already has a valid row. Test this explicitly — it is the most likely first-run failure, and feature 16 depends on the seeded row existing.
6. **Write Postgres before SQLite.** The ordering is the mitigation, not an implementation detail; do not reorder for convenience.
7. **`--strict-markers` is deliberate.** It converts the class of bug this feature exists to fix into a hard error for everyone after.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — remains the only store for canonical/alias/edge/system-reference data. This feature does not move it.
- `[PLANNED → this feature]` Postgres at runtime: driver, `DATABASE_URL`, migration execution, `approval_decisions`, `audit_log`. Nothing exists today; this feature builds it.
- `[PARTIAL] → code-enforced here` Audit log append-only. `audit.py` exposes INSERT only. The "append-only" wording above `audit_log` in `db/schema.sql` is a **SQL comment** — not a trigger, `REVOKE`, or `RULE`; grep-assert `db/schema.sql` contains no `CREATE TRIGGER`, no `CREATE RULE`, and no `REVOKE` touching `audit_log`. Do not cite the comment as an enforced constraint.
- `[PLANNED]` RLS. No `CREATE POLICY` anywhere; zero queries filter on `tenant_id` despite the column existing. New writes carry `tenant_id` as a value; they are not RLS-scoped.
- `[PLANNED]` Auth. `supabase==2.9.0` stays unimported; `api/` remains stubs. This feature adds no auth surface.

### Relevant Spec Sections

- Section 8: System Architecture (audit trail non-negotiable, idempotency everywhere)
- Section 9: Stage 6 — Resolution + Graph Update (the decision records persisted here)
- Section 10 / rules §10: Data security posture — audit log append-only, tenant scoping
