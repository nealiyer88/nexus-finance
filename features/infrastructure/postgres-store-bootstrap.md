# Feature Brief: Postgres Store Bootstrap — Driver, Migration Runner, Test Tier, Approval + Audit Writes

**Author:** Neal Iyer
**Date:** 2026-08-22
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10a (split out of feature 10; lands between 10 and 11)

---

## Problem Statement

`db/schema.sql` and `db/migrations/001_canonical_schema.sql` describe a Postgres store that **has never been executed anywhere**. Per `.claude/rules/01-nexus-finance-v1.md` §0, both are `[PLANNED]` and must be treated as not existing: no code imports a Postgres driver, `requirements.txt` pins none (`supabase==2.9.0` is pinned but never imported and is not a driver path), nothing in the tree reads a `DATABASE_URL`, and the only consumer of `schema.sql` is `tests/test_schema_parity.py`, which parses it **as text**. Two tables live there and only there: `approval_decisions` (`db/schema.sql:86`, `001:95`) and `audit_log` (`db/schema.sql:106`, `001:114`).

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

- **Migration runner — `scripts/migrate_pg.py`.** Applies SQL files from `db/migrations/` in filename order against the configured database, recording each applied filename.
  - Creates `schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())` **before** applying anything.
  - **Skips any file already recorded.** A file is applied at most once, ever.
  - **DESTRUCTIVE-FILE GUARD (defect fix — see below).** Before applying `001_canonical_schema.sql`, the runner checks whether `canonical_entities` already exists while `schema_migrations` has no row for `001`. If so it **refuses to run**, exits non-zero, and prints the reason. This is the case where a database was created some other way and re-running 001 would silently destroy it.
  - Prints one line per file (`applied` / `skipped`) and exits 0 on success.
  - `--dry-run` prints the plan and applies nothing.
  - `db/migrations/002_llm_training_data.sql` (the Postgres sibling of the SQLite mirror feature 9 shipped) is applied by the same runner in filename order.

  **DEFECT FIX — "idempotent on second run" was false and is not repeated here.** `db/migrations/001_canonical_schema.sql:9-18` opens, inside a single `BEGIN`, with eight statements: `DROP TABLE IF EXISTS approvals / entities / approval_decisions / system_references / entity_edges / entity_aliases / canonical_entities / audit_log CASCADE`. Its own comment calls this "idempotent migration"; it is not — re-executing 001 **drops eight tables and every row in them**. Only the `CREATE TABLE` half is `IF NOT EXISTS`-guarded; the `DROP` half is unconditional.

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

  `--strict-markers` turns any future unregistered marker into a collection **error** rather than a warning. Adding a `pytest.ini` fixes pytest's rootdir at the repo root, which is also where `sys.path` insertion for `from core...` imports comes from — the full existing suite must be re-verified under the new config (criterion below).

- **`tests/conftest.py` (NEW):** a `pg_conn` fixture that `pytest.skip`s with an explicit reason when `core.graph.pg.is_available()` is `False`, yields a connection wrapped in a transaction, and rolls back at teardown so integration tests leave no residue.

- **Writers.**
  - `core/graph/audit.py` — `log_resolution(conn, canonical_id, incoming_entity_raw, match_type, confidence, signals, category_pair, user_id, tenant_id)`. **INSERT-only** into `audit_log`, one row per resolution decision. The module contains no UPDATE and no DELETE path. V1 append-only is enforced **in code**: `db/schema.sql`'s "append-only" line is a SQL comment, not a trigger, `REVOKE`, or `RULE`.
  - `core/graph/approvals.py` — `record_approval_decision(conn, ...)` writing `approval_decisions`. Maps the in-tree `Action` / terminal-outcome vocabulary onto that table's `CHECK (disposition IN ('approved','rejected','corrected'))` (`db/schema.sql:94`) via an explicit, tested dict — the two vocabularies differ and the mapping must not be implicit.
  - Both are called from `core/graph/resolution.py` (shipped by feature 10) **behind an availability check**: when `DATABASE_URL` is unset the calls are no-ops and Stage 6 continues on SQLite exactly as it does today. Feature 10's own test suite must still pass untouched.

- **Split-store risk, owned here.** With this feature live, Stage 6 reads and mutates the graph in **SQLite** and writes approval/audit records to **Postgres** — two engines, no shared transaction. A crash between them can leave an approved alias with no audit row.
  - **Ordering:** write the Postgres audit row **first**, then the SQLite graph mutation. The failure mode becomes an audit row for a resolution that did not land (detectable and re-drivable) rather than a graph mutation with no record (undetectable).
  - **Re-drive safety:** feature 10 already makes `resolve_match` idempotent on `(canonical_id, value, source)` and on `(source_node, target_node, relationship)`, so re-driving is safe.
  - **Reconciliation:** `scripts/reconcile_stores.py` counts SQLite alias/edge rows against Postgres `audit_log` rows for a tenant and exits non-zero on divergence.
  - This is **not** atomicity — nothing short of a single store gives that. The split collapses when the graph store itself moves to Postgres, which is out of scope here.

- **Tests:** `tests/test_pg_bootstrap.py` (migration runner + connection helper), `tests/test_audit_pg.py` (audit + approval writers), both `@pytest.mark.integration`. Plus non-integration tests for the marker registration itself and for the `Action` → `disposition` mapping, which needs no database.

- **Local/CI Postgres story** documented in Implementation Notes: a `docker run postgres:16` one-liner, and the same image as a CI service container. Chosen over `testcontainers` because it adds no new test dependency and keeps `.venv/bin/python -m pytest tests/` green on a machine with no database — which every current contributor path assumes.

### Out of Scope

- **Migrating the graph store to Postgres.** `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references` reads and writes stay on SQLite. This feature adds a *second* store for approval + audit records only.
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

- [ ] `requirements.txt` gains exactly one new pinned line (`git diff --stat requirements.txt` shows 1 insertion, 0 deletions) and `.venv/bin/python -c "import psycopg"` succeeds after `pip install -r requirements.txt`.
- [ ] With `DATABASE_URL` unset: `core.graph.pg.get_dsn()` returns `None`, `is_available()` returns `False`, and `connect()` raises `RuntimeError` whose message contains the literal string `DATABASE_URL`.

**Migration runner (requires a database):**

- [ ] Against an empty database, `DATABASE_URL=... .venv/bin/python scripts/migrate_pg.py` exits 0, and `SELECT to_regclass('public.approval_decisions')` and `SELECT to_regclass('public.audit_log')` both return non-NULL.
- [ ] `SELECT filename FROM schema_migrations ORDER BY filename` returns exactly the files under `db/migrations/` that target Postgres, in filename order.
- [ ] **Runner-level idempotency (the only idempotency claimed):** a second immediate run exits 0, prints `skipped` for every file, prints `applied` for none, and leaves both `SELECT COUNT(*) FROM schema_migrations` and a pre-seeded `SELECT COUNT(*) FROM canonical_entities` **unchanged**. Explicitly asserted: seed one `canonical_entities` row before the second run and assert it still exists afterward.
- [ ] **Destructive-file guard:** on a database where `canonical_entities` exists but `schema_migrations` has no row for `001_canonical_schema.sql`, the runner exits **non-zero**, prints a message containing `001_canonical_schema.sql`, and applies nothing (`canonical_entities` row count unchanged).
- [ ] `scripts/migrate_pg.py --dry-run` exits 0 and creates no table (`SELECT to_regclass('public.audit_log')` still NULL on an empty database).
- [ ] `scripts/migrate_pg.py`'s module docstring contains the literal words `DROP TABLE` and `data loss` — grep-asserted, so the hazard cannot be silently deleted.

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
- **Downstream:** feature 16 (`connectors-audit-infra`) currently names feature 10 as the owner of the Postgres path. That is now this feature — feature 16's dependency and queue row must point at **10a**.

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
4. **Never claim migration 001 is idempotent.** It is not: `001:9-18` drops eight tables `CASCADE` inside a `BEGIN`. Idempotency in this feature means *the runner does not re-run an applied file*. Keep that wording in code comments, help text, and any future brief that inherits this.
5. **`tenant_id` mismatch between engines.** `approval_decisions.tenant_id` and `audit_log.tenant_id` are `NOT NULL REFERENCES tenants(id)` (`db/schema.sql:88`, `:108`), while the SQLite convention is nullable and fixtures load NULL (`schema_sqlite.sql:8`, `entity_store.py:12-16`). The writers must resolve or create a `tenants` row for the active tenant before insert, or the NOT NULL FK rejects every write. Test this explicitly — it is the most likely first-run failure.
6. **Write Postgres before SQLite.** The ordering is the mitigation, not an implementation detail; do not reorder for convenience.
7. **`--strict-markers` is deliberate.** It converts the class of bug this feature exists to fix into a hard error for everyone after.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — remains the only store for canonical/alias/edge/system-reference data. This feature does not move it.
- `[PLANNED → this feature]` Postgres at runtime: driver, `DATABASE_URL`, migration execution, `approval_decisions`, `audit_log`. Nothing exists today; this feature builds it.
- `[PARTIAL] → code-enforced here` Audit log append-only. `audit.py` exposes INSERT only. The "append-only" line in `db/schema.sql` is a **SQL comment** — not a trigger, `REVOKE`, or `RULE`. Do not cite it as an enforced constraint.
- `[PLANNED]` RLS. No `CREATE POLICY` anywhere; zero queries filter on `tenant_id` despite the column existing. New writes carry `tenant_id` as a value; they are not RLS-scoped.
- `[PLANNED]` Auth. `supabase==2.9.0` stays unimported; `api/` remains stubs. This feature adds no auth surface.

### Relevant Spec Sections

- Section 8: System Architecture (audit trail non-negotiable, idempotency everywhere)
- Section 9: Stage 6 — Resolution + Graph Update (the decision records persisted here)
- Section 10 / rules §10: Data security posture — audit log append-only, tenant scoping
