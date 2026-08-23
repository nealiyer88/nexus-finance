# Feature Brief: Postgres Store Bootstrap — Driver, Migration Runner, Test Tier, Approval + Audit Writes

**Author:** Neal Iyer
**Date:** 2026-08-22
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10a (split out of feature 10; lands between 10 and 11)

---

## Problem Statement

`db/schema.sql` and `db/migrations/001_canonical_schema.sql` describe a Postgres store that **has never been executed anywhere**. Per `.claude/rules/01-nexus-finance-v1.md` §0, both are `[PLANNED]` and must be treated as not existing: no code imports a Postgres driver, `requirements.txt` pins none (`supabase==2.9.0` is pinned but never imported and is not a driver path), nothing in the tree reads a `DATABASE_URL`, and the only consumer of `db/schema.sql` is `tests/test_schema_parity.py`, which parses it **as text** (alongside `db/schema_sqlite.sql`, and only those two files). The two tables this feature cares about live in the Postgres schema and only there: `approval_decisions` and `audit_log` — both are declared in `db/schema.sql` and in `db/migrations/001_canonical_schema.sql`, and in neither `db/schema_sqlite.sql` nor any `*_sqlite.sql` migration. Assert by symbol, not by line: `grep -c 'CREATE TABLE.*\bapproval_decisions\b'` and the same for `audit_log` are non-zero in `db/schema.sql` and `db/migrations/001_canonical_schema.sql`, and zero across `db/schema_sqlite.sql` and `db/migrations/*_sqlite.sql`.

The consequence is that the two records the product treats as non-negotiable — a durable record of every approval decision, and an append-only audit trail of every resolution — have nowhere to go. Feature 10 delivers the compounding graph on SQLite without them; features 11, 14, and 16 need them. Feature 16 (`connectors-audit-infra`) already declares a hard dependency on "the feature that stands up the Postgres path" — **that feature is this one, not 10**.

This feature stands up that path once, correctly, so every later feature inherits it: a pinned driver, a connection helper, a migration runner that is safe to re-run, a **registered** pytest marker for the database-requiring tier, and the first two writers (`approval_decisions`, `audit_log`).

Three defects found in the 2026-08-22 reality-check dry runs are fixed here rather than carried forward — the unregistered `integration` marker (§Scope, Marker registration), the false idempotency claim about the shipped migrations (§Scope, Migration runner), and the false reconciliation invariant (§Scope, Split-store risk). All three are called out explicitly below.

---

## Scope

### Cross-feature ownership note (read first)

This feature **edits `core/graph/resolution.py`, which is feature 10's shipped file**. Feature 10 is the prior owner of that module; this feature adds guarded calls into the new writers at its Stage 6 call sites and changes nothing else there. Because feature 10's tests must also pass **untouched** (criterion below), the edit is deliberately additive and no-op by default: with `DATABASE_URL` unset the added calls do nothing and Stage 6 behaves exactly as feature 10 ships it. Any reviewer seeing `resolution.py` in this feature's diff should read it as an intentional cross-feature ownership edit, not scope creep.

### In Scope

- **Driver dependency.** Add a single pinned Postgres driver to `requirements.txt`: `psycopg[binary]==<pin>`. Verified 2026-08-22: `requirements.txt` contains no `psycopg`, no `asyncpg`, no `SQLAlchemy`. The binary wheel is chosen so no local `libpq` or C toolchain is required on Apple-Silicon dev machines or CI.

- **Connection configuration.** `core/graph/pg.py`, a single small module:
  - `get_dsn() -> str | None` — reads `DATABASE_URL` via `python-dotenv` (already pinned). Returns `None` when unset; never raises, never invents a default DSN.
  - `connect() -> psycopg.Connection` — raises a clear `RuntimeError` naming `DATABASE_URL` when the DSN is absent.
  - `is_available() -> bool` — used by test skip guards.
  - No pooling, no ORM, no engine abstraction.

- **Migration runner — `scripts/migrate_pg.py`.** Applies Postgres migrations against the configured database, recording each applied filename.

  **SELECTION RULE (the one mechanism; no other rule applies anywhere in this brief):** *The runner applies exactly the filenames listed in `db/migrations/postgres.manifest`, in the order they appear in that file, and never reads or applies any other file in `db/migrations/`.* The manifest (NEW, created by this feature) is a newline-delimited list of bare filenames; blank lines and `#` comment lines are ignored.

  **AUTHORING RULE for the manifest (resolves at build time; do not hardcode the list into this brief).** At build time, enumerate `db/migrations/*.sql` and author the manifest as: every file whose name does **not** end in `_sqlite.sql`, in ascending filename order, followed by this feature's own tenant-bootstrap migration last. Earlier queued features keep adding migrations here, so the list is derived once at build time and then frozen as a reviewed static file — the fail-closed property is preserved because nothing is ever applied that a human did not write into the manifest. Two invariants the manifest must satisfy, both mechanically checkable:
  - It contains **no entry matching `_sqlite`**. Every `*_sqlite.sql` file in `db/migrations/` is a SQLite mirror shipped by an earlier feature and is loaded only by SQLite tests, never applied to Postgres. Derive that set by globbing `db/migrations/*_sqlite.sql` at build time; do not name the files here.
  - It contains, at minimum, every non-`_sqlite` `.sql` file present in `db/migrations/` at build time — assert as a set comparison at build time, not as a count.

  If the manifest names a file that does not exist on disk, the runner exits non-zero before applying anything.

  **MIGRATION NUMBERING — avoid prefix collisions with upstream features.** Earlier queued features have already claimed numeric prefixes in `db/migrations/`. This feature's tenant-bootstrap migration therefore takes the **next unused numeric prefix at build time**, computed as one above the highest numeric prefix present in `db/migrations/`; this brief refers to it as `<NNN>_bootstrap_tenant.sql` everywhere below. If a feature landing first has already claimed that number, take the next free one instead; the file's number is not load-bearing, only its position **last in the manifest** is. Criterion: after this feature lands, no two non-`_sqlite` files in `db/migrations/` share a numeric prefix.

  **Why a manifest rather than a naming convention or a directory split:** it is fail-closed and opt-in — a migration file added later is invisible to Postgres until someone lists it, so a future SQLite-only or dialect-ambiguous file can never be applied by accident no matter how it is named, and the worst failure mode is a migration that visibly did not run rather than a wrong-dialect one that did. A **directory split was rejected** for a different reason: `db/migrations/001_canonical_schema.sql` is referenced by path — by this feature's own manifest, and by shipped feature briefs under `features/` that quote that path — so moving it silently invalidates references outside this feature's ownership. Verification is by search, not by asserting anything about a specific test: at build time, `grep -rn 'migrations/' tests/` and confirm whether any test resolves a path under `db/migrations/`; whatever that grep returns, the manifest keeps 001 at its current path, so no consumer breaks either way.

  - Creates `schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())` **before** applying anything.
  - **Skips any file already recorded.** A file is applied at most once, ever.
  - **DESTRUCTIVE-FILE GUARD (defect fix — see below).** Before applying `001_canonical_schema.sql`, the runner checks whether `canonical_entities` already exists while `schema_migrations` has no row for `001`. If so it **refuses to run**, exits non-zero, and prints the reason. This is the case where a database was created some other way and re-running 001 would silently destroy it.
  - Prints one line per file (`applied` / `skipped`) and exits 0 on success.
  - `--dry-run` prints the plan and applies nothing.
  - Each Postgres-side migration shipped by an earlier feature is applied by the same runner because the manifest lists it; each `_sqlite` sibling is not listed and is not applied.

  **DEFECT FIX — "idempotent on second run" was false and is not repeated here.** `db/migrations/001_canonical_schema.sql` opens, inside a single `BEGIN`, with a block of **unconditional** `DROP TABLE IF EXISTS ... CASCADE` statements. `IF EXISTS` suppresses the error when the table is absent; it does **not** make the statement conditional on anything else, so re-executing 001 drops every table that block names and every row in them. Do not restate that this is limited to one half of the file: **the `DROP` block is unconditional, and the `CREATE TABLE` statements are only partly `IF NOT EXISTS`-guarded.** Derive which is which at build time rather than trusting any list in this brief:
  - `grep -nE '^DROP TABLE' db/migrations/001_canonical_schema.sql` → the destructive set. It is non-empty.
  - `grep -cE '^CREATE TABLE' db/migrations/001_canonical_schema.sql` versus `grep -cE '^CREATE TABLE IF NOT EXISTS' db/migrations/001_canonical_schema.sql` → the guarded/unguarded split. The first is strictly greater than the second; the guarded set is a proper subset of all `CREATE TABLE` statements. Assert that relationship, never the membership.

  Do not enumerate table names or totals anywhere in code comments, docstrings, or tests derived from this brief — enumerate by grep at the moment of use.

  **001 is not the only destructive migration.** At least one other shipped Postgres migration in `db/migrations/` also carries an unconditional `DROP TABLE ... CASCADE`. Derive the full set at build time with `grep -lE '^DROP TABLE' db/migrations/*.sql`, excluding `*_sqlite.sql`.

  **Resolution chosen: the runner never re-runs an applied file, and the shipped migrations are left byte-unchanged.** Idempotency is redefined at the **runner** level and stated that way in every criterion: *a second `migrate_pg.py` run applies zero files and exits 0.* It is explicitly **not** claimed that re-executing any shipped migration is safe. The shipped migrations are not rewritten because reworking a `DROP` block is a schema-authoring change that belongs to the owning schema feature, not to a bootstrap feature — and because leaving them byte-identical keeps this feature's diff free of any file another queued feature may also be editing.

  **DATA-LOSS HAZARD — flag prominently in `scripts/migrate_pg.py`'s module docstring and in `README`-adjacent notes:** running any destructive migration by hand via `psql -f`, or deleting its `schema_migrations` row and re-running the runner, **destroys all data in the tables that migration drops**. The docstring must **name every migration file containing an unconditional `DROP TABLE`**, and that list is produced at build time by `grep -lE '^DROP TABLE' db/migrations/*.sql` (excluding `*_sqlite.sql`) — **no hardcoded list may be copied out of this brief**. The runner's guard defends the first-run case for `001`; it cannot defend a manual `psql` invocation of anything.

- **Marker registration — `pytest.ini` at repo root (NEW).** Verified 2026-08-22: this repo has **no pytest configuration of any kind** — no `pytest.ini`, no `pyproject.toml`, no `setup.cfg`, no `tox.ini`, no `conftest.py` anywhere outside `.venv`. The consequence the dry run actually measured: `@pytest.mark.integration` is unregistered, so pytest emits a warning rather than an error, `-m integration` **deselects everything**, and the run **exits 0 with a zero-test selection**. That is the dangerous outcome — not a loud failure but a silent pass. Any criterion phrased as "the integration suite passes" or "the integration run exits 0" is therefore false signal: it passes just as readily when nothing at all ran. Every criterion in this brief that touches the integration tier asserts a **collected count greater than zero** on the two new test files; a bare exit code is never sufficient on its own. This feature creates:

  ```ini
  [pytest]
  testpaths = tests
  addopts = --strict-markers
  markers =
      integration: requires a live Postgres reachable via DATABASE_URL
  ```

  `--strict-markers` turns any future unregistered marker into a collection **error** rather than a warning — converting the silent-deselect failure mode into a loud one for everyone after. Adding a `pytest.ini` fixes pytest's rootdir at the repo root and sets `testpaths`/`addopts` for every future run. Note that rootdir is *not* what puts the repo root on `sys.path` — that comes from invoking `.venv/bin/python -m pytest`, which prepends the current working directory, so `from core...` imports keep working for the same reason they do today. The config still changes collection (rootdir, `testpaths`, `addopts`), so the full existing suite must be re-verified under it (criterion below).

- **Tenant provisioning — owned here, relied on downstream.** Verified 2026-08-22: `tenants` is declared in both `db/schema.sql` and `db/migrations/001_canonical_schema.sql` as `(id UUID PRIMARY KEY DEFAULT gen_random_uuid(), name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now())`. **Do not assert a count of referencing columns** — earlier queued features keep adding them. Derive the set instead: every column matching `tenant_id UUID NOT NULL REFERENCES tenants(id)` in `db/schema.sql`, which includes `approval_decisions` and `audit_log` plus every other table an earlier queued feature added there. Nothing anywhere creates a `tenants` row today, so the first real insert on any of those tables FK-fails. This feature closes that:
  - `core/graph/pg.py` exposes `BOOTSTRAP_TENANT_ID` — a **fixed UUID literal**, not generated, so it is stable across databases and quotable by other features.
  - `db/migrations/<NNN>_bootstrap_tenant.sql` (NEW, next free numeric prefix — see MIGRATION NUMBERING above — listed **last** in the manifest) inserts that row: `INSERT INTO tenants (id, name, slug) VALUES ('<BOOTSTRAP_TENANT_ID>', 'Bootstrap Tenant', 'bootstrap') ON CONFLICT (id) DO NOTHING;` — re-running applies nothing and destroys nothing. `tenants` is **not** named in 001's `DROP TABLE ... CASCADE` block (grep-assert at build time: `grep -E '^DROP TABLE IF EXISTS +tenants\b' db/migrations/001_canonical_schema.sql` returns nothing), so this seed survives the destructive block.
  - `core/graph/tenants.py` — `resolve_or_create_tenant(conn, tenant_id, name=None, slug=None) -> UUID`, an `INSERT ... ON CONFLICT (id) DO NOTHING` + `SELECT`. Every writer in this feature calls it before inserting, so a tenant id that is not the bootstrap one still works.
  - **Downstream contract:** feature 16's `DEFAULT_TENANT_ID` constant **must be the same literal as `BOOTSTRAP_TENANT_ID`**, and feature 16 may assume the row exists after `migrate_pg.py` has run. Feature 16 does not create tenants.

- **`tests/conftest.py` (NEW):** a `pg_conn` fixture that `pytest.skip`s with an explicit reason when `core.graph.pg.is_available()` is `False`, yields a connection wrapped in a transaction, and rolls back at teardown so integration tests leave no residue.

- **Writers.**
  - `core/graph/audit.py` — `log_resolution(conn, canonical_id, incoming_entity_raw, match_type, confidence, signals, category_pair, user_id, tenant_id)`. **INSERT-only** into `audit_log`, one row per resolution decision. The module contains no UPDATE and no DELETE path. V1 append-only is enforced **in code**: `db/schema.sql`'s "append-only" line is a SQL comment, not a trigger, `REVOKE`, or `RULE`.

    **`actor_id` is left NULL — mandatory, not optional.** `audit_log.actor_id` is typed `UUID` in `db/schema.sql`, while the in-tree actor value that reaches Stage 6 is the plain-string `approved_by` parameter carried by `core/graph/resolution.py` (and stored into `entity_edges.approved_by`, which is TEXT on SQLite). Passing that string through would raise on insert. Therefore: `log_resolution` **inserts NULL into `actor_id`**, and the human-readable actor, if it is to be retained at all, goes into a TEXT-typed column or is dropped. **Widening `actor_id` to accept a non-UUID actor identifier is a future feature-2 schema change and is explicitly out of scope for 10a** — do not edit `db/schema.sql` or any shipped migration to accommodate it here.
  - `core/graph/approvals.py` — `record_approval_decision(conn, ...)` writing `approval_decisions`, whose `disposition` column carries a `CHECK` constraint. **Key the mapping on the terminal disposition values `core/graph/resolution.py` actually produces today** — do not key it on `core.matching.types.Action`. `Action` is a `typing.Literal` of Stage 4 **routing** bands, at least two of which explicitly denote *not yet decided*; it is the wrong vocabulary, it is not an `Enum` so it has no member iteration and no `terminal` attribute, and there is no in-tree definition of which of its members are "terminal". At build time, derive the producer set by grepping `core/graph/resolution.py` for the terminal disposition string literals it writes (search its `TrainingPair(...)` constructions and the `disposition=` assignments in its write-path functions) — that grep, not this brief, is the authority on the exact strings.

    The mapping is an explicit, tested dict from each of those producer values to a member of the DDL's `CHECK` set. Read the allowed set out of `approval_decisions.disposition`'s `CHECK` constraint in `db/schema.sql` **by grep at test time**, never by line number and never as a literal list written into a test. The invariant is a **subset assertion**: `set(MAPPING.values()) <= CHECK_SET_PARSED_FROM_DDL`, plus `set(MAPPING.keys()) == PRODUCER_SET_PARSED_FROM_RESOLUTION_PY`. Both sides are derived at run time, so a later feature adding a producer or widening the CHECK cannot silently invalidate the test.

    **`'corrected'` has no in-tree producer.** The CHECK set includes a value that nothing in `core/`, `api/`, or `dashboard/` ever emits (verify with `grep -rn "corrected" core/ api/ dashboard/`). Because the DDL's CHECK is not being changed here, that value stays in the CHECK set and is **unmapped by design**: it is a legal column value with no producer, reachable only by a future feature that adds a correction flow. Do not invent a producer for it, and do not assert that the mapping covers the whole CHECK set — the subset direction above is the only claim that holds.
  - Both are called from `core/graph/resolution.py` (feature 10's shipped file — see the cross-feature ownership note above) **behind an availability check**: when `DATABASE_URL` is unset the calls are no-ops and Stage 6 continues on SQLite exactly as it does today. Feature 10's own test suite must still pass untouched.

- **Split-store risk, owned here.** With this feature live, Stage 6 reads and mutates the graph in **SQLite** and writes approval/audit records to **Postgres** — two engines, no shared transaction. A crash between them can leave an approved alias with no audit row.
  - **Ordering:** write the Postgres audit row **first**, then the SQLite graph mutation. The failure mode becomes an audit row for a resolution that did not land (detectable and re-drivable) rather than a graph mutation with no record (undetectable).
  - **Re-drive safety:** feature 10 already makes `resolve_match` idempotent on `(canonical_id, value, source)` and on `(source_node, target_node, relationship)`, so re-driving is safe.
  - **Reconciliation — `scripts/reconcile_stores.py`. DEFECT FIX: the invariant is coverage, not count equality.** An earlier draft compared SQLite alias/edge row counts against Postgres `audit_log` row counts for equality. That is **false by construction and must not be built**: `log_resolution` writes one row per *resolution decision*, while a single resolution writes an alias row **and** an edge row (and may write neither on a repeat, since both writes are idempotent upserts). Counts diverge on a perfectly healthy system, so the tool would exit non-zero in the normal case.

    The invariant that is actually true under the write-Postgres-first ordering is **coverage in one direction only**: *every canonical entity touched by a Stage 6 graph mutation for a tenant has at least one `audit_log` row for that tenant.* Concretely, for a given tenant and a watermark timestamp (default: the earliest `audit_log.created_at` for that tenant, so rows written before this feature landed are excluded):
    - Collect the distinct canonical identifiers appearing in SQLite `entity_aliases.canonical_id` and in `entity_edges.source_node` / `entity_edges.target_node`, restricted to rows whose `created_at` is at or after the watermark, and scoped to the tenant by joining to `canonical_entities.tenant_id` (neither alias nor edge rows carry a `tenant_id` column of their own — derive the join at build time from `db/schema_sqlite.sql` rather than assuming a column).
    - Assert each such identifier appears in at least one `audit_log` row for that tenant. `audit_log.resource_id` is TEXT and is the column the writer populates with the canonical identifier; confirm that against the writer at build time.
    - The **reverse direction is expected and is not an error**: audit rows with no corresponding graph mutation are exactly the benign crash outcome the write ordering was chosen to produce. Report them as an informational line; do not fail on them.
    - Exit non-zero **only** on the uncovered-mutation direction, printing each uncovered identifier.
  - This is **not** atomicity — nothing short of a single store gives that. The split collapses when the graph store itself moves to Postgres, which is out of scope here.

- **Tests:** `tests/test_pg_bootstrap.py` (migration runner + connection helper), `tests/test_audit_pg.py` (audit + approval writers), both `@pytest.mark.integration`. Plus non-integration tests for the marker registration itself and for the disposition mapping, which needs no database.

- **Local/CI Postgres story** documented in Implementation Notes: a `docker run postgres:16` one-liner, and the same image as a CI service container. Chosen over `testcontainers` because it adds no new test dependency and keeps `.venv/bin/python -m pytest tests/` green on a machine with no database — which every current contributor path assumes.

### Out of Scope

- **Migrating the graph store to Postgres.** Every table in `db/schema_sqlite.sql` keeps its reads and writes on SQLite — the stable graph tables plus any table added there by an earlier queued feature. Do not assert a table count for `db/schema_sqlite.sql`; derive the list by grepping its `CREATE TABLE` statements. This feature adds a *second* store for approval + audit records only.
- **Rewriting any shipped migration** to remove its `DROP TABLE ... CASCADE` block. Deliberate — see the resolution above. If the owner later wants true file-level idempotency, that is a separate schema feature.
- **Widening `audit_log.actor_id`** (or any other `db/schema.sql` type change) to carry the free-form actor string. That is a feature-2 schema change; here `actor_id` stays NULL.
- **Down-migrations / rollback.** Forward-only.
- **Database-enforced append-only** on `audit_log` (trigger / `REVOKE` / `RULE`). V1 enforcement is code-level.
- **Row-level security.** No `CREATE POLICY` exists anywhere; zero queries filter on `tenant_id`. Writes carry `tenant_id` as a value.
- **Connection pooling, ORM, async.** One driver, one `connect()`.
- **Supabase hosting/auth.** `supabase==2.9.0` stays unimported.
- **A producer for the unmapped `disposition` CHECK value.** No correction flow is built here.
- The audit *middleware* and audit *dashboard page* — feature 16, which consumes this feature's `audit_log` and `core/graph/pg.py`.

---

## Success Criteria

**Marker registration (no database required):**

- [ ] `pytest.ini` exists at repo root and registers `integration`; `.venv/bin/python -m pytest --markers` output contains a line starting `@pytest.mark.integration` — assert exit 0 and non-empty grep.
- [ ] `.venv/bin/python -m pytest tests/test_pg_bootstrap.py tests/test_audit_pg.py -m integration --collect-only -q` reports a **collected count greater than zero**, parsed from the collection output. The exit code alone is **not** the assertion: an unregistered or non-matching marker deselects everything and still exits 0, which is precisely the silent-pass failure this criterion exists to catch.
- [ ] `.venv/bin/python -m pytest tests/ -m "not integration" -x --tb=short` exits 0 with `DATABASE_URL` unset — the default contributor path stays green with no database.
- [ ] With `--strict-markers` active, a test decorated with a deliberately bogus marker causes a **collection error**, asserted in a subprocess test that expects a non-zero exit.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` exits 0 under the new `pytest.ini`, and its collected count is greater than or equal to the count captured on the same checkout immediately before `pytest.ini` was added — no shipped test dropped out of collection from the rootdir/`testpaths`/`addopts` change. Capture the baseline at build time; do not write a literal into the test.

**Driver + connection (no database required):**

- [ ] `requirements.txt` gains one new pinned line and deletes none: `git diff -U0 requirements.txt` shows a single added line matching `^psycopg\[binary\]==` and no removed lines. `.venv/bin/python -c "import psycopg"` succeeds after `pip install -r requirements.txt`. `grep -cE '^(asyncpg|SQLAlchemy|pg8000)' requirements.txt` returns 0 — one driver, no second path.
- [ ] With `DATABASE_URL` unset: `core.graph.pg.get_dsn()` returns `None`, `is_available()` returns `False`, and `connect()` raises `RuntimeError` whose message contains the literal string `DATABASE_URL`.

**Migration runner (requires a database):**

- [ ] Against an empty database, `DATABASE_URL=... .venv/bin/python scripts/migrate_pg.py` exits 0, and `SELECT to_regclass('public.approval_decisions')` and `SELECT to_regclass('public.audit_log')` both return non-NULL.
- [ ] **`schema_migrations` equals the manifest, derived at runtime — no hardcoded total.** The test parses `db/migrations/postgres.manifest` (stripping blanks and `#` comments) into a list, and asserts `SELECT filename FROM schema_migrations ORDER BY applied_at` returns that same list in that same order and nothing else: `assert rows == manifest_entries`. This stays PASS/FAIL as the manifest grows.
- [ ] **SQLite migrations are provably not applied:** after a full run, `SELECT COUNT(*) FROM schema_migrations WHERE filename LIKE '%\_sqlite%'` returns zero. The runner's stdout contains no line mentioning any file matching `*_sqlite.sql` (neither `applied` nor `skipped` — those files are never read at all); the test derives that filename set by globbing `db/migrations/*_sqlite.sql` rather than naming files. Grep-assert `db/migrations/postgres.manifest` contains no `_sqlite` entry.
- [ ] **Manifest covers every Postgres migration on disk:** the set of non-`_sqlite` `.sql` files under `db/migrations/` equals the set of manifest entries — asserted as a set comparison computed at test time, so a migration an earlier feature added cannot be silently unapplied.
- [ ] **Fail-closed on an unlisted file:** a test drops a syntactically-invalid `.sql` file into `db/migrations/` without listing it in the manifest; the runner still exits 0 and `SELECT filename FROM schema_migrations` still equals the parsed manifest list (the invalid file is absent) — proving selection is by manifest, not by directory scan. Assert against the parsed manifest, not a literal row count.
- [ ] **Fail-closed on a missing file:** with a manifest entry naming a nonexistent file, the runner exits **non-zero**, names the missing filename, and applies nothing.
- [ ] **Runner-level idempotency (the only idempotency claimed):** a second immediate run exits 0, prints `skipped` for every manifest entry, prints `applied` for none, and leaves both `SELECT COUNT(*) FROM schema_migrations` and a pre-seeded `SELECT COUNT(*) FROM canonical_entities` unchanged from the values captured after the first run (compare before/after; never compare against a literal). Explicitly asserted: seed a `canonical_entities` row before the second run and assert it still exists afterward.
- [ ] **Destructive-file guard:** on a database where `canonical_entities` exists but `schema_migrations` has no row for `001_canonical_schema.sql`, the runner exits **non-zero**, prints a message containing `001_canonical_schema.sql`, and applies nothing (`canonical_entities` row count unchanged from the value captured before the attempt).
- [ ] `scripts/migrate_pg.py --dry-run` exits 0 and creates no table (`SELECT to_regclass('public.audit_log')` still NULL on an empty database).
- [ ] `scripts/migrate_pg.py`'s module docstring contains the literal words `DROP TABLE` and `data loss` — grep-asserted, so the hazard cannot be silently deleted.
- [ ] **The docstring names every destructive migration, derived not hardcoded:** a no-database test computes the set of files under `db/migrations/` (excluding `*_sqlite.sql`) matching `^DROP TABLE`, and asserts every filename in that set appears verbatim in `scripts/migrate_pg.py`'s module docstring. A new destructive migration added later therefore fails this test until it is named.

**Tenant provisioning (requires a database):**

- [ ] After `migrate_pg.py` completes on an empty database, `SELECT COUNT(*) FROM tenants WHERE id = '<BOOTSTRAP_TENANT_ID>'` returns a single row, and that literal is byte-identical to `core.graph.pg.BOOTSTRAP_TENANT_ID` (asserted in the test, not eyeballed).
- [ ] **A dependent insert referencing it succeeds:** `INSERT INTO audit_log (tenant_id, action, resource) VALUES (core.graph.pg.BOOTSTRAP_TENANT_ID, 'test', 'test')` commits without an FK error, and the same row shape with a random unseeded UUID raises a `ForeignKeyViolation` — proving the FK is live and the seed is what satisfies it.
- [ ] `resolve_or_create_tenant(conn, <fresh UUID>)` returns that UUID, creates a single `tenants` row where none existed, and a second call with the same UUID creates none and returns the same value (compare row counts before/after, not against a literal).
- [ ] A no-database test asserts `core.graph.pg.BOOTSTRAP_TENANT_ID` parses as a valid UUID and is a module-level literal (no `uuid4()` call in `core/graph/pg.py` — grep-asserted), so feature 16's `DEFAULT_TENANT_ID` can quote it.

**Writers:**

- [ ] `core/graph/audit.py` exists; `log_resolution` inserts one `audit_log` row per call — asserted as a before/after count delta of one, with `category` and `tenant_id` populated. (integration)
- [ ] `grep -nE "\b(UPDATE|DELETE)\b" core/graph/audit.py` returns no match — append-only enforced in code, not by a trigger.
- [ ] **No code path passes a non-UUID string into `actor_id`.** Two assertions: (a) a no-database test greps `core/graph/audit.py` and `core/graph/approvals.py` and confirms `actor_id` is only ever bound to `None`/SQL `NULL` — no parameter, and specifically not `approved_by` or `user_id`, is bound to it; (b) an integration test calls `log_resolution` with a deliberately non-UUID actor string and asserts the insert succeeds and the resulting row's `actor_id` is NULL.
- [ ] `core/graph/approvals.py` `record_approval_decision` inserts one `approval_decisions` row whose `disposition` satisfies the table's CHECK, for every key in the mapping. A no-database unit test parses the CHECK set out of `db/schema.sql` by grep and the producer set out of `core/graph/resolution.py` by grep, then asserts `set(MAPPING.values()) <= CHECK_SET` and `set(MAPPING.keys()) == PRODUCER_SET`. Neither set may appear as a literal in the test. (mapping test: no database; insert: integration)
- [ ] With `DATABASE_URL` unset, `core/graph/resolution.py`'s calls into these writers are no-ops: `.venv/bin/python -m pytest tests/test_resolution.py -x` still exits 0 and its collected count is unchanged from the pre-edit baseline captured at build time — feature 10's suite passes untouched despite this feature editing its file.
- [ ] `scripts/reconcile_stores.py` exits 0 on a healthy tenant **whose SQLite alias and edge row counts deliberately exceed its `audit_log` row count** (drive at least one resolution end to end, which writes multiple graph rows behind a single audit row) — proving the tool asserts coverage, not count equality. It exits non-zero with a per-identifier diff line when an `audit_log` row is deleted such that a mutated canonical identifier loses its last audit row, and it exits 0 (with an informational line only) when an `audit_log` row exists for a resolution whose graph mutation did not land. (integration)
- [ ] `DATABASE_URL=... .venv/bin/python -m pytest tests/test_pg_bootstrap.py tests/test_audit_pg.py -m integration -x --tb=short` exits 0 against a freshly migrated database **and** its collected count is greater than zero, asserted from the same run's output.

---

## Dependencies

- [ ] **Feature 2 (canonical-schema) — SHIPPED**, in the narrow sense that it authored `db/schema.sql` and `db/migrations/001_canonical_schema.sql`. **Those files have never been executed** — per rules §0 they are `[PLANNED]` and must be treated as not existing. This feature is the first to run them. Feature 2 also remains the owner of any type change to `db/schema.sql`, including the `audit_log.actor_id` widening this feature declines to make.
- [ ] **Feature 10 (resolution-graph-update) — must land first.** It ships `core/graph/resolution.py`, the call site for `log_resolution` and `record_approval_decision`, the terminal disposition vocabulary they key on, and the decision payloads they persist. This feature edits that file (see the cross-feature ownership note in Scope). Feature 10 has no reverse dependency on this feature and is fully buildable and testable with no Postgres present.
- [ ] **A reachable Postgres 16 instance** for the integration tier — local Docker or a CI service container. Absent one, the integration criteria are **not verified**; they skip. A skip is not a pass, and this feature cannot be marked SHIPPED on skips alone.
- [ ] **`python-dotenv`** — already pinned in `requirements.txt`; no new pin needed for config loading.
- **Downstream:** feature 16 (`connectors-audit-infra`) depends on this feature for the Postgres path; verify its queue row and dependency list already point at **10a** and edit nothing if they do. Feature 16 also inherits tenant provisioning from here: its `DEFAULT_TENANT_ID` must be set to this feature's `BOOTSTRAP_TENANT_ID` literal, and it must not create `tenants` rows of its own.

---

## Estimated Complexity

**Rating:** L

**Rationale:** Every item here is a *first* for this repo: the first Postgres driver in `requirements.txt`, the first `DATABASE_URL`, the first migration ever executed, the first pytest configuration file of any kind, the first test tier that requires external infrastructure, the first CI service container, and the first application code to write to a second engine. None of it extends existing code, so none of it has a pattern to copy. Three risks are identified and priced in: more than one shipped migration is destructive on re-execution and all are being contained by the runner rather than rewritten; the split-store write path between SQLite and Postgres has no shared transaction and is mitigated by write ordering plus a *coverage* reconciliation rather than solved; and the audit/actor type mismatch is sidestepped by writing NULL rather than by a schema change this feature does not own. The load-bearing risk is that adding `pytest.ini` changes rootdir and collection behavior for a suite that has never had a config file — hence the explicit full-suite re-verification criterion, phrased as a collected-count comparison against a baseline captured at build time.

---

## PROJECT CONTEXT

### Pipeline position

```
Stage 6 Resolution (feature 10, SQLite)
    └─ if DATABASE_URL set (THIS FEATURE):
         1. Postgres  audit_log            ← log_resolution()          (INSERT-only, written FIRST, actor_id NULL)
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
3. **An unregistered marker fails silently, not loudly.** Before this feature there is no pytest config at all, so `-m integration` deselects the entire suite and **exits 0 having run nothing**. Any criterion that "passes" by selecting zero tests is false signal — that is why every integration criterion above asserts a **collected count greater than zero**, never a bare exit code. Keep that framing in code comments and in any future brief that inherits this.
4. **Never claim a shipped migration is idempotent.** More than one carries an unconditional leading `DROP TABLE ... CASCADE` block inside a `BEGIN`; derive the set with `grep -lE '^DROP TABLE' db/migrations/*.sql` (excluding `*_sqlite.sql`) whenever you need it. Note also that the `CREATE TABLE` statements in `001` are only partly `IF NOT EXISTS`-guarded — compare `grep -cE '^CREATE TABLE'` against `grep -cE '^CREATE TABLE IF NOT EXISTS'` rather than assuming either way. Idempotency in this feature means *the runner does not re-run an applied file*. Keep that wording in code comments, help text, and any inheriting brief.
5. **`tenant_id` mismatch between engines.** In `db/schema.sql`, `approval_decisions.tenant_id` and `audit_log.tenant_id` are `UUID NOT NULL REFERENCES tenants(id)`, while the SQLite convention is a nullable `tenant_id TEXT` and fixtures load NULL — see `db/schema_sqlite.sql`'s `canonical_entities` DDL and the "Tenant scoping" paragraph of `core/graph/entity_store.py`'s module docstring, which states that reads default to no tenant filter. Note also that SQLite's alias and edge tables carry **no** `tenant_id` column at all; tenant scoping there is by join to `canonical_entities`. The writers must call `resolve_or_create_tenant()` before insert, or the NOT NULL FK rejects every write. `<NNN>_bootstrap_tenant.sql` seeds `BOOTSTRAP_TENANT_ID` so the default path already has a valid row. Test this explicitly — it is the most likely first-run failure, and feature 16 depends on the seeded row existing.
6. **`actor_id` is UUID; the in-tree actor is a plain string.** Write NULL. Do not coerce, do not hash into a UUID, do not widen the column. See Scope and the dedicated success criterion.
7. **Write Postgres before SQLite.** The ordering is the mitigation, not an implementation detail; do not reorder for convenience. The reconciliation tool's one-directional coverage invariant is only sound *because* of this ordering.
8. **Reconciliation is coverage, never count equality.** One resolution decision produces one audit row and multiple graph rows; equality of counts is false on a healthy system. Assert that every mutated canonical identifier has at least one audit row, and treat the reverse (audit row without a landed mutation) as informational.
9. **`--strict-markers` is deliberate.** It converts the class of bug this feature exists to fix into a hard error for everyone after.
10. **Do not key approval dispositions on `core.matching.types.Action`.** It is a `typing.Literal` of Stage 4 routing bands, some of which explicitly mean *not yet decided*, and it has no iterable members and no notion of terminality. Key on the terminal disposition strings `core/graph/resolution.py` writes, derived by grep at build time.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — remains the only store for canonical/alias/edge/system-reference data. This feature does not move it.
- `[PLANNED → this feature]` Postgres at runtime: driver, `DATABASE_URL`, migration execution, `approval_decisions`, `audit_log`. Nothing exists today; this feature builds it.
- `[PARTIAL] → code-enforced here` Audit log append-only. `audit.py` exposes INSERT only. The "append-only" wording above `audit_log` in `db/schema.sql` is a **SQL comment** — not a trigger, `REVOKE`, or `RULE`; grep-assert `db/schema.sql` contains no `CREATE TRIGGER`, no `CREATE RULE`, and no `REVOKE` touching `audit_log`. Do not cite the comment as an enforced constraint.
- `[PLANNED]` RLS. No `CREATE POLICY` anywhere; zero queries filter on `tenant_id` despite the column existing. New writes carry `tenant_id` as a value; they are not RLS-scoped.
- `[PLANNED]` Auth. `supabase==2.9.0` stays unimported; `api/` remains stubs. This feature adds no auth surface. `actor_id` staying NULL is consistent with there being no authenticated principal in V1.

### Relevant Spec Sections

- Section 8: System Architecture (audit trail non-negotiable, idempotency everywhere)
- Section 9: Stage 6 — Resolution + Graph Update (the decision records persisted here)
- Section 10 / rules §10: Data security posture — audit log append-only, tenant scoping
