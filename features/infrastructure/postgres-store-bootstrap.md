# Feature Brief: Postgres Store Bootstrap — Driver, Migration Runner, Test Tier, Approval + Audit Writes

**Author:** Neal Iyer
**Date:** 2026-08-23 (revised after the 2026-08-22 reality check; database target restated as environment-supplied)
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10a (split out of feature 10; lands between 10 and 11)

---

## Problem Statement

`db/schema.sql` and `db/migrations/001_canonical_schema.sql` describe a Postgres store that **has never been executed anywhere**. Per `.claude/rules/01-nexus-finance-v1.md` §0, both are `[PLANNED]` and must be treated as not existing: no code imports a Postgres driver, `requirements.txt` pins none (`supabase==2.9.0` is pinned but never imported and is not a driver path), nothing in the tree reads a `DATABASE_URL`, and the only consumer of `db/schema.sql` is `tests/test_schema_parity.py`, which parses it **as text** (alongside `db/schema_sqlite.sql`). The two tables this feature cares about live in the Postgres schema and only there: `approval_decisions` and `audit_log`. Assert by symbol, not by line: `grep -c 'CREATE TABLE.*\bapproval_decisions\b'` and the same for `audit_log` are non-zero in `db/schema.sql` and `db/migrations/001_canonical_schema.sql`, and zero across `db/schema_sqlite.sql` and `db/migrations/*_sqlite.sql`.

The consequence is that the two records the product treats as non-negotiable — a durable record of every approval decision, and an append-only audit trail of every resolution — have nowhere to go. Feature 10 delivers the compounding graph on SQLite without them; features 11, 14, and 16 need them. Feature 16 (`connectors-audit-infra`) already declares a hard dependency on "the feature that stands up the Postgres path" — **that feature is this one, not 10**.

This feature stands up that path once, correctly, so every later feature inherits it: a pinned driver, a connection helper, a migration runner that is safe to re-run, a **registered** pytest marker for the database-requiring tier, and the first two writers (`approval_decisions`, `audit_log`).

---

## The database target — decided, not to be re-litigated

**Postgres for this repo is whatever `DATABASE_URL` points at, and nothing in this feature is allowed to care which server that is.** The developer environment and the eventual deployment target may be different servers; both are reached identically, through that one variable. Everything below follows from that:

- **Postgres major version is 17.** Any version-conditional reasoning uses 17. **Do not pin a patch version anywhere** — not in the brief, not in a comment, not in a test. `gen_random_uuid()` is used by shipped migrations with no `CREATE EXTENSION pgcrypto`; that is a built-in on the major version in use, so no extension step is required. State the requirement as a **minimum major version**, never as an exact release.
- **The target is supplied entirely by the environment, and nothing may be hardcoded.** No module, test, comment, fixture, docstring, printed line, or sentence of this brief may contain a host, a port, a database name, or a username; may build a DSN out of parts; may assume the DSN carries a password component; or may assume the server does or does not live on the same machine as the build. Anything that would have to change when the DSN changes is a defect.
- **Provisioning is not this feature's business.** Do not write, run, or document any command that installs, initialises, starts, containerises, or tears down a database server, and do not add a CI service container. Whether a server already exists, and where, is the developer's environment, not this feature's contract. If the build wants to execute SQL, it does so through the pinned driver against `DATABASE_URL` and through nothing else.
- **The connection contract is `DATABASE_URL` from the environment, and its absence means "Postgres not available" — never an error.** The developer supplies the real value in a gitignored `.env` at the repo root. `.env` is already ignored; only `.env.example` is committed. Never invent a default DSN, never fall back to any built-in target, never construct a DSN from parts.
- **`.env` loading reuses whatever loader the repo already has.** Derive it at build time: grep the tree for an existing `load_dotenv` call and copy that module's convention — in this repo it is a **lazy, guarded import inside the function that needs it**, so the loader is never a hard import-time dependency and the module stays importable with the package absent. Do not add a new loader, do not add a `requirements.txt` line for one, and do not import `dotenv` at module top level.
- **Every connection this feature opens must carry an explicit connect timeout.** The rationale does not depend on where the server is: a connect with no timeout can hang the caller — the migration runner, a test, or Stage 6 — indefinitely, and an unbounded wait is a defect on any target. Correspondingly, no code, comment, or test may bake in an assumption about how fast a connect or a query returns (no "instant", no retry-free tight loop, no sub-second assumption baked into a test), because the same code runs against targets of very different latencies. **This brief pins no timeout value.** At build time, grep the tree for an existing outbound-client timeout convention and adopt it if one exists; if the grep finds none, the builder chooses the value and defines it as a single UPPER_CASE module-level constant in `core/graph/pg.py`, overridable by an environment variable. The value is never an inline literal at a call site.

### SECRET HANDLING — hard requirement

`DATABASE_URL` is treated as a live credential at all times, **whether or not the current value happens to carry a password** — a deployment DSN will, the value in a developer's `.env` may not, and no code path is permitted to branch on which. Therefore, without exception:

- It is **never logged** — not at any level, not in debug output, not in a `print`.
- It is **never echoed in an error message**. `connect()` may name the *variable* `DATABASE_URL`; it may never include its *value*, and no exception this feature raises or re-raises may carry the DSN in its message. Driver exceptions that embed connection parameters must be caught and re-raised with a scrubbed message.
- It is **never written to any file under `features/` or `.rocket/`**, and never into a fixture, a snapshot, a log, a migration file, or a committed config. Only `.env` (gitignored) holds it; only `.env.example` (committed) holds a placeholder.
- The DSN and **every component it may carry** appear **nowhere in this brief** by design — the brief names the variable, never the value, and never states which components a given value has.

---

## Scope

### Cross-feature ownership note (read first)

This feature **edits `core/graph/resolution.py`, which is feature 10's shipped file**, and **retires a named set of shipped guard tests**. Both are deliberate, both are in scope, and both are described here so a reviewer reads them as ownership edits rather than scope creep.

**1. The added call site must be token-clean.** Feature 10's shipped suite contains a grep-guard over its own modules that fails on any Postgres-related **token**. It matches tokens, not intent — so an edit that reaches Postgres **only through the pg module's availability helper** passes it cleanly, while a perfectly correct edit that merely *mentions* the database in a comment fails it. Therefore:

- The Stage 6 call site reaches the database **exclusively via the pg module's availability helper and the writer functions**. No driver import, no environment read, and no DSN handling appears in feature 10's shipped module.
- **No identifier, string literal, or comment inside any file that guard covers may contain any token the guard forbids.** The forbidden token set and the covered path set are **derived at build time by reading the shipped guard's own regex and path tuple** — they are deliberately **not copied into this brief**, because a copy goes stale the moment the guard is widened.
- This overrides the general instruction elsewhere in this brief to "keep that framing in code comments": the write-ordering rationale is documented in **this feature's new modules**, never in feature 10's shipped ones.
- **Decision: that guard is NOT retired. It genuinely survives**, because indirection through the availability helper introduces none of the tokens it matches. A criterion below asserts it still passes.

**2. The requirements-diff guards ARE retired, in the same commit that adds the driver pin.** Feature 10's shipped suite — and, since 10b landed, feature 10b's suite as well — contains tests asserting the working-tree diff of `requirements.txt` is empty. This feature must add a driver pin to that exact file. **The two cannot hold in one working tree**, and the gate runs pre-commit against the working tree, so the build fails outright. Committing the pin separately would only launder the diff to empty and defeat the guard's purpose; that is explicitly forbidden.

- **Retire every shipped test that asserts the `requirements.txt` diff is empty**, in the **same commit** that adds the pin. Derive the set at build time by grepping `tests/` for tests that shell out to `git diff` against `requirements.txt` and assert emptiness — do not rely on this brief for the list; at minimum it spans feature 10's suite and feature 10b's suite, and a later feature may have added another.
- The retirement is visible in the diff: each retired test is **deleted or explicitly skipped with a reason naming this feature**, so a reviewer can enumerate the retired set from the diff alone.
- Consequently the "feature 10's suite passes untouched" criterion is **restated** (see Success Criteria): *every test in feature 10's suite that this feature does not explicitly retire still passes, and the set of retired tests is enumerable from the diff.* No count of retired tests is written anywhere.

### In Scope

- **Driver dependency.** Add a single pinned Postgres driver to `requirements.txt`: `psycopg[binary]==<pin>`. Confirm at build time by grep that no other Postgres driver is already pinned. The binary wheel is chosen so no local `libpq` or C toolchain is required on Apple-Silicon dev machines or CI.

- **`.env.example` gains `DATABASE_URL`.** A committed line with a **clearly fake placeholder** value, in the same commented-section style the file already uses. The placeholder must be unmistakably a placeholder: every component it carries is obviously synthetic, it may not resolve to any real server, and it may not read as a syntactically-plausible live credential. It is an illustration of the variable's shape, not of any environment's actual target. `.env` itself is already gitignored and is never created, read into, or referenced by a committed file other than as this placeholder.

- **Connection configuration.** `core/graph/pg.py`, a single small module:
  - `get_dsn() -> str | None` — reads `DATABASE_URL` from the environment, loading `.env` first via the repo's existing lazy-guarded loader convention. Returns `None` when unset. **Never raises, never invents a default, never falls back to any built-in target.**
  - `connect() -> psycopg.Connection` — opens a connection carrying the explicit connect timeout described above. Raises a clear `RuntimeError` **naming the variable `DATABASE_URL`** when the DSN is absent; the message never contains the DSN value.
  - `is_available() -> bool` — the single availability helper. Used by test skip guards **and by feature 10's Stage 6 call site**, which reaches Postgres through nothing else.
  - `BOOTSTRAP_TENANT_ID` — see Tenant provisioning.
  - No pooling, no ORM, no engine abstraction.

- **Migration runner — `scripts/migrate_pg.py`.** Applies Postgres migrations against the configured database, recording each applied filename.

  **SELECTION RULE (the one mechanism; no other rule applies anywhere in this brief):** *The runner applies exactly the filenames listed in `db/migrations/postgres.manifest`, in the order they appear in that file, and never reads or applies any other file in `db/migrations/`.* The manifest (NEW, created by this feature) is a newline-delimited list of bare filenames; blank lines and `#` comment lines are ignored.

  **AUTHORING RULE for the manifest (resolves at build time; do not hardcode the list into this brief).** At build time, enumerate `db/migrations/*.sql` and author the manifest as: every file whose name does **not** end in `_sqlite.sql`, in ascending filename order, followed by this feature's own tenant-bootstrap migration last. Earlier queued features keep adding migrations here, so the list is derived once at build time and then frozen as a reviewed static file — the fail-closed property is preserved because nothing is ever applied that a human did not write into the manifest. Two invariants the manifest must satisfy, both mechanically checkable:
  - It contains **no entry matching `_sqlite`**. Every `*_sqlite.sql` file in `db/migrations/` is a SQLite-dialect file shipped by another feature and is loaded only by SQLite tests, never applied to Postgres. Derive that set by globbing `db/migrations/*_sqlite.sql` at build time; do not name the files here.
  - It contains every non-`_sqlite` `.sql` file present in `db/migrations/` at build time — assert as a **set comparison**, never as a count.

  If the manifest names a file that does not exist on disk, the runner exits non-zero before applying anything.

  **MIGRATION NUMBERING — the prefix space is shared with SQLite-only migrations (updated for feature 10b).** Feature 10b shipped a **SQLite-only** pending-decisions migration. It consumes a numeric prefix while having **no Postgres counterpart**, so computing "next free prefix" over the Postgres subset alone would collide with a shipped file. Therefore:
  - The next free prefix is computed over **every file in `db/migrations/`, `_sqlite` siblings included** — one above the highest numeric prefix present across the whole directory. If a feature landing first has claimed it, take the next free one.
  - **This brief writes no number.** The tenant-bootstrap migration is referred to as `<NNN>_bootstrap_tenant.sql` throughout; its number is not load-bearing, only its position **last in the manifest** is.
  - Criterion, stated as an invariant rather than a list: after this feature lands, **no two files in `db/migrations/` share a numeric prefix unless they are a dialect pair of the same migration** — i.e. one filename is the other's `_sqlite` sibling. A SQLite-only migration with no Postgres counterpart therefore owns its prefix outright, and nothing this feature authors may reuse it.

  **10b's table does not enter the Postgres path.** 10b's pending-decisions table is SQLite-only and is deliberately absent from `tests/test_schema_parity.py`'s shared-table literal. This feature: adds **no** Postgres mirror of it; adds **no** manifest entry for it (its filename matches `_sqlite` and is excluded by the invariant above); makes **no** edit to the parity test or its shared-table literal. Assert at build time by importing the parity module and confirming 10b's table name is absent from that literal — and leave it absent. Schema parity is therefore unaffected in both directions: the parity test compares only the tables its own literal names, and this feature adds no table to that literal.

  - Creates `schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())` **before** applying anything.
  - **Skips any file already recorded.** A file is applied at most once, ever.
  - **DESTRUCTIVE-FILE GUARD (defect fix — see below).** Before applying `001_canonical_schema.sql`, the runner checks whether `canonical_entities` already exists while `schema_migrations` has no row for `001`. If so it **refuses to run**, exits non-zero, and prints the reason. This is the case where a database was created some other way and re-running 001 would silently destroy it. **This guard is the primary safety mechanism** — the runner must assume the database it is pointed at holds data someone cares about, and must never rely on the target being disposable.
  - Prints one line per file (`applied` / `skipped`) and exits 0 on success. **No printed line may contain the DSN**, in any code path including the failure paths.
  - `--dry-run` prints the plan and applies nothing.

  **DEFECT FIX — "idempotent on second run" was false and is not repeated here.** `db/migrations/001_canonical_schema.sql` opens, inside a single `BEGIN`, with a block of **unconditional** `DROP TABLE IF EXISTS ... CASCADE` statements. `IF EXISTS` suppresses the error when the table is absent; it does **not** make the statement conditional on anything else, so re-executing 001 drops every table that block names and every row in them. Do not restate that this is limited to one half of the file: **the `DROP` block is unconditional, and the `CREATE TABLE` statements are only partly `IF NOT EXISTS`-guarded.** Derive which is which at build time rather than trusting any list in this brief:
  - `grep -nE '^DROP TABLE' db/migrations/001_canonical_schema.sql` → the destructive set. It is non-empty.
  - `grep -cE '^CREATE TABLE' db/migrations/001_canonical_schema.sql` versus `grep -cE '^CREATE TABLE IF NOT EXISTS' db/migrations/001_canonical_schema.sql` → the guarded/unguarded split. The first is strictly greater than the second; the guarded set is a proper subset of all `CREATE TABLE` statements. Assert that **relationship**, never the membership and never either number.

  Do not enumerate table names or totals anywhere in code comments, docstrings, or tests derived from this brief — enumerate by grep at the moment of use.

  **001 is not the only destructive migration.** More than one shipped Postgres migration in `db/migrations/` carries an unconditional `DROP TABLE ... CASCADE`. Derive the full set at build time with `grep -lE '^DROP TABLE' db/migrations/*.sql`, excluding `*_sqlite.sql`. Do not write the set down.

  **Resolution chosen: the runner never re-runs an applied file, and the shipped migrations are left byte-unchanged.** Idempotency is redefined at the **runner** level and stated that way in every criterion: *a second `migrate_pg.py` run applies zero files and exits 0.* It is explicitly **not** claimed that re-executing any shipped migration is safe. The shipped migrations are not rewritten because reworking a `DROP` block is a schema-authoring change that belongs to the owning schema feature, not to a bootstrap feature.

  **DATA-LOSS HAZARD — flag prominently in `scripts/migrate_pg.py`'s module docstring:** running any destructive migration by hand against the configured database, or deleting its `schema_migrations` row and re-running the runner, **destroys all data in the tables that migration drops**. Assume no escape hatch and no automatic snapshot on whatever target `DATABASE_URL` names. The docstring must **name every migration file containing an unconditional `DROP TABLE`**, and that list is produced at build time by `grep -lE '^DROP TABLE' db/migrations/*.sql` (excluding `*_sqlite.sql`) — **no hardcoded list may be copied out of this brief**.

- **Marker registration — `pytest.ini` at repo root (NEW).** Derive at build time that the repo has no pytest configuration of any kind, then create one. The consequence a dry run measured: an unregistered `@pytest.mark.integration` makes pytest emit a warning rather than an error, `-m integration` **deselects everything**, and the run **exits 0 with a zero-test selection**. That is the dangerous outcome — not a loud failure but a silent pass. Any criterion phrased as "the integration suite passes" or "the integration run exits 0" is therefore false signal. Every criterion in this brief that touches the integration tier asserts a **collected count greater than zero**; a bare exit code is never sufficient on its own. This feature creates:

  ```ini
  [pytest]
  testpaths = tests
  addopts = --strict-markers
  markers =
      integration: requires a reachable Postgres via DATABASE_URL
  ```

  `--strict-markers` turns any future unregistered marker into a collection **error** rather than a warning. Adding a `pytest.ini` fixes pytest's rootdir at the repo root and sets `testpaths`/`addopts` for every future run. Note that rootdir is *not* what puts the repo root on `sys.path` — that comes from invoking `.venv/bin/python -m pytest`, which prepends the current working directory, so `from core...` imports keep working for the same reason they do today. The config still changes collection, so the full existing suite must be re-verified under it (criterion below).

- **Tenant provisioning — owned here, relied on downstream.** `tenants` is declared in both `db/schema.sql` and `db/migrations/001_canonical_schema.sql`; read its DDL by grep at build time rather than trusting a transcription. **Do not assert a count of referencing columns** — earlier queued features keep adding them. Derive the set instead: every column matching `tenant_id UUID NOT NULL REFERENCES tenants(id)` in `db/schema.sql`. Nothing anywhere creates a `tenants` row today, so the first real insert on any of those tables FK-fails. This feature closes that:
  - `core/graph/pg.py` exposes `BOOTSTRAP_TENANT_ID` — a **fixed UUID literal at module level**, not generated, importable with **no database present**, so it is stable across databases and quotable by other features.
  - `db/migrations/<NNN>_bootstrap_tenant.sql` (NEW, next free prefix per MIGRATION NUMBERING above, listed **last** in the manifest) inserts that row: `INSERT INTO tenants (id, name, slug) VALUES ('<BOOTSTRAP_TENANT_ID>', 'Bootstrap Tenant', 'bootstrap') ON CONFLICT (id) DO NOTHING;` — re-running applies nothing and destroys nothing. Grep-assert at build time that `tenants` is **not** named in 001's `DROP TABLE ... CASCADE` block, so this seed survives the destructive block.
  - `core/graph/tenants.py` — `resolve_or_create_tenant(conn, tenant_id, name=None, slug=None) -> UUID`, an `INSERT ... ON CONFLICT (id) DO NOTHING` + `SELECT`.
  - **NULL-tenant fallback, stated explicitly rather than implied.** Feature 10's `resolve_match` carries `tenant_id: Optional[str] = None` and SQLite fixtures load NULL, while the Postgres columns are `NOT NULL REFERENCES tenants(id)`. Every writer in this feature therefore **falls back to `BOOTSTRAP_TENANT_ID` when the caller's `tenant_id` is `None`**, and calls `resolve_or_create_tenant` before inserting when it is not. Without this fallback the default path FK-fails on every write.
  - **Downstream contract:** feature 16's `DEFAULT_TENANT_ID` is bound to `BOOTSTRAP_TENANT_ID` by import (`DEFAULT_TENANT_ID = BOOTSTRAP_TENANT_ID`), asserted by **identity** on 16's side, and feature 16 may assume the row exists after `migrate_pg.py` has run. Nothing in this feature may make `BOOTSTRAP_TENANT_ID` require a database to import, rename it, or turn it into a computed value — all three would break that seam. Feature 16 does not create tenants.

- **`tests/conftest.py` (NEW):** a `pg_conn` fixture that `pytest.skip`s with an explicit reason when `core.graph.pg.is_available()` is `False`, yields a connection wrapped in a transaction, and rolls back at teardown so integration tests leave no residue. **Rollback-at-teardown is mandatory, not hygienic** — the target is never assumed to be disposable, and a test that commits pollutes whatever database the developer or CI pointed at. The skip reason names the variable `DATABASE_URL` and never its value.

- **Writers.**
  - `core/graph/audit.py` — `log_resolution(conn, canonical_id, incoming_entity_raw, match_type, confidence, signals, category_pair, user_id, tenant_id)`. **INSERT-only** into `audit_log`, one row per resolution decision. The module contains no UPDATE and no DELETE path. V1 append-only is enforced **in code**: `db/schema.sql`'s "append-only" line is a SQL comment, not a trigger, `REVOKE`, or `RULE`.

    **`actor_id` is left NULL — mandatory, not optional.** `audit_log.actor_id` is typed `UUID`, while the in-tree actor value that reaches Stage 6 is a plain string carried by feature 10's resolution module. Passing that string through would raise on insert. Therefore: `log_resolution` **inserts NULL into `actor_id`**, and the human-readable actor, if retained at all, goes into a TEXT-typed column or is dropped. **Widening `actor_id` is a future feature-2 schema change and is explicitly out of scope for 10a.**
  - `core/graph/approvals.py` — `record_approval_decision(conn, ...)` writing `approval_decisions`, whose `disposition` column carries a `CHECK` constraint. **Key the mapping on the terminal disposition values feature 10's resolution module actually produces today** — do not key it on `core.matching.types.Action`. `Action` is a `typing.Literal` of Stage 4 **routing** bands, at least two of which explicitly denote *not yet decided*; it is the wrong vocabulary, it is not an `Enum` so it has no member iteration and no `terminal` attribute. At build time, derive the producer set by grepping feature 10's resolution module for the terminal disposition string literals it writes — that grep, not this brief, is the authority on the exact strings, and it must be run in a way that introduces no forbidden token into that shipped file.

    The mapping is an explicit, tested dict from each producer value to a member of the DDL's `CHECK` set, and may need to normalize case. Read the allowed set out of `approval_decisions.disposition`'s `CHECK` constraint in `db/schema.sql` **by grep at test time**, never by line number and never as a literal list written into a test. The invariant is a **subset assertion**: `set(MAPPING.values()) <= CHECK_SET_PARSED_FROM_DDL`, plus `set(MAPPING.keys()) == PRODUCER_SET_PARSED_FROM_RESOLUTION`. Both sides are derived at run time.

    **One CHECK value has no in-tree producer.** The CHECK set includes a value that nothing in `core/`, `api/`, or `dashboard/` emits — verify by grep at build time rather than naming it here. Because the DDL's CHECK is not being changed, that value stays in the CHECK set and is **unmapped by design**. Do not invent a producer for it, and do not assert that the mapping covers the whole CHECK set — the subset direction is the only claim that holds.

    Note that feature 10b's SQLite pending-decisions table deliberately reuses this same terminal vocabulary for its `status` column, so no translation layer is needed if a later feature back-fills pending rows into `approval_decisions`. **This feature builds no such back-fill** and makes no edit to 10b's module, migration, or tests.
  - Both are called from feature 10's shipped resolution module (see the cross-feature ownership note) **behind the availability helper**: when `DATABASE_URL` is unset the calls are no-ops and Stage 6 continues on SQLite exactly as it does today.

- **Split-store risk, owned here.** With this feature live, Stage 6 reads and mutates the graph in **SQLite** and writes approval/audit records to **Postgres** — two engines, no shared transaction, and a separate connection that can fail or time out independently. A crash or a timeout between them can leave an approved alias with no audit row.
  - **Ordering:** write the Postgres audit row **first**, then the SQLite graph mutation. The failure mode becomes an audit row for a resolution that did not land (detectable and re-drivable) rather than a graph mutation with no record (undetectable). Because the Postgres write can time out, a connect timeout on that write must fail **before** the SQLite mutation, preserving the ordering guarantee rather than silently skipping it.
  - **Re-drive safety:** feature 10 already makes `resolve_match` idempotent on `(canonical_id, value, source)` and on `(source_node, target_node, relationship)`, so re-driving is safe.
  - **Reconciliation — `scripts/reconcile_stores.py`. DEFECT FIX: the invariant is coverage, not count equality.** An earlier draft compared SQLite alias/edge row counts against Postgres `audit_log` row counts for equality. That is **false by construction and must not be built**: one resolution decision writes one audit row but multiple graph rows (and may write none on a repeat, since both writes are idempotent upserts). Counts diverge on a perfectly healthy system.

    The invariant that is actually true under the write-Postgres-first ordering is **coverage in one direction only**: *every canonical entity touched by a Stage 6 graph mutation for a tenant has at least one `audit_log` row for that tenant.* For a given tenant and a watermark timestamp (default: the earliest `audit_log.created_at` for that tenant, so rows written before this feature landed are excluded):
    - Collect the distinct canonical identifiers appearing in SQLite `entity_aliases.canonical_id` and in `entity_edges.source_node` / `entity_edges.target_node`, restricted to rows at or after the watermark, and scoped to the tenant by joining to `canonical_entities.tenant_id` (neither alias nor edge rows carry a `tenant_id` column of their own — derive the join at build time from `db/schema_sqlite.sql`).
    - **Normalize timestamp formats across engines.** SQLite's `created_at` is TEXT in a `CURRENT_TIMESTAMP` format; the Postgres watermark is `TIMESTAMPTZ`. Derive both formats at build time and convert to a common UTC representation before comparing — a naive string comparison across the two is wrong.
    - Assert each identifier appears in at least one `audit_log` row for that tenant, against the TEXT column the writer populates with the canonical identifier; confirm which column that is against the writer at build time.
    - The **reverse direction is expected and is not an error**: audit rows with no corresponding graph mutation are exactly the benign crash outcome the write ordering was chosen to produce. Feature 16 also writes audit rows that are provider-scoped and never canonical-scoped, which land in this same benign bucket. Report them as an informational line; do not fail on them.
    - Exit non-zero **only** on the uncovered-mutation direction, printing each uncovered identifier. Print no DSN on any path.
  - This is **not** atomicity — nothing short of a single store gives that.

- **Tests:** `tests/test_pg_bootstrap.py` (migration runner + connection helper), `tests/test_audit_pg.py` (audit + approval writers), both `@pytest.mark.integration`. Plus non-integration tests for the marker registration, the disposition mapping, the secret-hygiene greps, and the guard-retirement invariant — none of which need a database.

### Out of Scope

- **Migrating the graph store to Postgres.** Every table in `db/schema_sqlite.sql` keeps its reads and writes on SQLite. Do not assert a table count for it; derive the list by grepping its `CREATE TABLE` statements. This feature adds a *second* store for approval + audit records only.
- **Any database-provisioning story.** No Docker, no `docker compose`, no `testcontainers`, no `initdb`, no server-install or server-start instructions, no CI service container. `DATABASE_URL` names the only target this feature knows about; standing that target up is out of scope in both directions.
- **Mirroring feature 10b's pending-decisions table into Postgres**, and any edit to 10b's module, migration, tests, or to `tests/test_schema_parity.py`'s shared-table literal.
- **Rewriting any shipped migration** to remove its `DROP TABLE ... CASCADE` block.
- **Widening `audit_log.actor_id`** or any other `db/schema.sql` type change.
- **Down-migrations / rollback.** Forward-only.
- **Database-enforced append-only** on `audit_log` (trigger / `REVOKE` / `RULE`). V1 enforcement is code-level.
- **Row-level security.** No `CREATE POLICY` exists anywhere. Writes carry `tenant_id` as a value.
- **Connection pooling, ORM, async.** One driver, one `connect()`.
- **Any vendor SDK or auth layer in front of the database.** This feature speaks to a plain Postgres endpoint over `DATABASE_URL` through the pinned driver and nothing else; `supabase==2.9.0` stays unimported whatever the target turns out to be.
- **A producer for the unmapped `disposition` CHECK value.**
- The audit *middleware* and audit *dashboard page* — feature 16.

---

## Success Criteria

**Marker registration (no database required):**

- [ ] `pytest.ini` exists at repo root and registers `integration`; `.venv/bin/python -m pytest --markers` output contains a line starting `@pytest.mark.integration`.
- [ ] `.venv/bin/python -m pytest tests/test_pg_bootstrap.py tests/test_audit_pg.py -m integration --collect-only -q` reports a **collected count greater than zero**, parsed from the collection output. The exit code alone is **not** the assertion: a non-matching marker deselects everything and still exits 0.
- [ ] `.venv/bin/python -m pytest tests/ -m "not integration" -x --tb=short` exits 0 with `DATABASE_URL` unset **and reports a collected count greater than zero**, parsed from the same run's output — the default contributor path stays green with no database, and a zero-selection run does not count as green.
- [ ] With `--strict-markers` active, a test decorated with a deliberately bogus marker causes a **collection error**, asserted in a subprocess test expecting a non-zero exit.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` exits 0 under the new `pytest.ini`, and its collected count equals the baseline captured on the same checkout immediately before this feature's changes **minus the size of the retired-guard set**, both computed at build time from the runs themselves. No literal is written into the test.

**Guard retirement and cross-feature ownership (no database required):**

- [ ] **Restated "feature 10's suite still passes":** every test in feature 10's shipped suite that this feature does not explicitly retire passes under `.venv/bin/python -m pytest tests/test_resolution.py -x --tb=short`, with `DATABASE_URL` unset, and its collected count is greater than zero. The retired set is enumerable from the diff — each retired test is deleted or skipped with a reason naming this feature. No count of retired tests appears in any test or comment.
- [ ] The same holds for feature 10b's suite: `.venv/bin/python -m pytest tests/test_pending_decisions.py -x --tb=short` passes for every test not explicitly retired, with a collected count greater than zero.
- [ ] **The Postgres-token guard survives and is not retired:** feature 10's shipped grep-guard over its own modules **still passes unmodified**. A test derives the guard's forbidden-token regex and covered-path set from the shipped guard itself and asserts no covered file matches. Neither the regex nor the path set is transcribed into this feature's code.
- [ ] **No requirements-diff guard remains that contradicts the pin:** a test greps `tests/` for any test asserting the `requirements.txt` diff is empty and asserts the result set is empty (all such guards retired). Derived by grep, never against an expected number.
- [ ] The driver pin and the guard retirements appear in the **same commit** — asserted by inspecting that commit's changed-path set, which contains both `requirements.txt` and every file holding a retired guard.

**Driver + connection (no database required):**

- [ ] The `requirements.txt` diff adds a Postgres driver pin matching `^psycopg\[binary\]==` and **removes no line** — asserted as: the added-line set is non-empty and every added line matches that pattern, and the removed-line set is empty. `.venv/bin/python -c "import psycopg"` succeeds after `pip install -r requirements.txt`. A grep for any second Postgres driver pin in `requirements.txt` matches nothing.
- [ ] `.env.example` contains a `DATABASE_URL=` line whose value is a placeholder: a test asserts it does not parse as a usable credential and that every component it carries is obviously synthetic. The test asserts nothing about which components are present. `.env` remains gitignored and untracked (`git check-ignore .env` succeeds; `git ls-files .env` returns nothing).
- [ ] With `DATABASE_URL` unset: `core.graph.pg.get_dsn()` returns `None`, `is_available()` returns `False`, and `connect()` raises `RuntimeError` whose message contains the literal string `DATABASE_URL` and **does not** contain any DSN value.
- [ ] **Connect timeout is present and named:** a grep asserts `connect()` passes an explicit connect-timeout argument, that its value is a module-level UPPER_CASE constant in `core/graph/pg.py`, and that no inline numeric literal appears at the call site. No timeout value is asserted — only that one is set from a named constant.

**Secret hygiene (no database required):**

- [ ] **The DSN never reaches logs or errors:** a grep over `core/graph/pg.py`, `core/graph/audit.py`, `core/graph/approvals.py`, `core/graph/tenants.py`, `scripts/migrate_pg.py`, and `scripts/reconcile_stores.py` finds no path that logs, prints, or formats `get_dsn()`'s return value or the raw environment value into a message. A unit test monkeypatches `DATABASE_URL` to a sentinel string, exercises every failure path reachable without a database (missing DSN, malformed DSN, connect failure), captures stdout/stderr and the exception messages, and asserts the sentinel appears in none of them.
- [ ] **The secret is not in the diff or in test output:** a check reads `DATABASE_URL` from the environment at run time and asserts that the whole value, and each of the non-empty components it happens to carry, appears nowhere in this feature's full commit diff, in any file under `features/` or `.rocket/`, or in the captured output of this feature's test runs. Components the value does not carry are simply skipped — the check never requires a particular component to be present. The check holds the secret only in memory and writes it nowhere. When `DATABASE_URL` is unset the check runs against the sentinel and still asserts absence.
- [ ] `grep -rn 'DATABASE_URL=' -- features/ .rocket/` finds no line carrying a value; the only committed assignment with a value is the placeholder in `.env.example`.

**Migration runner (requires a reachable Postgres via `DATABASE_URL`):**

- [ ] Against the configured database, `.venv/bin/python scripts/migrate_pg.py` exits 0 with `DATABASE_URL` set, and `SELECT to_regclass('public.approval_decisions')` and `SELECT to_regclass('public.audit_log')` both return non-NULL.
- [ ] **`schema_migrations` equals the manifest, derived at runtime — no hardcoded total.** The test parses `db/migrations/postgres.manifest` (stripping blanks and `#` comments) into a list and asserts `SELECT filename FROM schema_migrations ORDER BY applied_at` returns that same list in that same order and nothing else: `assert rows == manifest_entries`.
- [ ] **SQLite-dialect migrations are provably not applied:** after a full run, `SELECT COUNT(*) FROM schema_migrations WHERE filename LIKE '%\_sqlite%'` returns zero. The runner's stdout mentions no file matching `*_sqlite.sql` (neither `applied` nor `skipped`); the test derives that filename set by globbing rather than naming files. Grep-assert the manifest contains no `_sqlite` entry. This explicitly covers feature 10b's migration.
- [ ] **Manifest covers every Postgres migration on disk:** the set of non-`_sqlite` `.sql` files under `db/migrations/` equals the set of manifest entries — a set comparison computed at test time.
- [ ] **Prefix invariant:** no two files in `db/migrations/` share a numeric prefix unless one is the other's `_sqlite` dialect sibling — computed by listing the directory at test time.
- [ ] **Fail-closed on an unlisted file:** a test drops a syntactically-invalid `.sql` file into `db/migrations/` without listing it; the runner still exits 0 and `SELECT filename FROM schema_migrations` still equals the parsed manifest list.
- [ ] **Fail-closed on a missing file:** with a manifest entry naming a nonexistent file, the runner exits **non-zero**, names the missing filename, and applies nothing.
- [ ] **Runner-level idempotency (the only idempotency claimed):** a second immediate run exits 0, prints `skipped` for every manifest entry, prints `applied` for none, and leaves both `SELECT COUNT(*) FROM schema_migrations` and a pre-seeded `SELECT COUNT(*) FROM canonical_entities` unchanged from the values captured after the first run (compare before/after; never against a literal). Seed a `canonical_entities` row before the second run and assert it survives.
- [ ] **Destructive-file guard:** on a database where `canonical_entities` exists but `schema_migrations` has no row for `001_canonical_schema.sql`, the runner exits **non-zero**, prints a message containing `001_canonical_schema.sql`, and applies nothing (row count unchanged from the value captured before the attempt).
- [ ] `scripts/migrate_pg.py --dry-run` exits 0 and creates no table.
- [ ] `scripts/migrate_pg.py`'s module docstring contains the literal words `DROP TABLE` and `data loss` — grep-asserted.
- [ ] **The docstring names every destructive migration, derived not hardcoded:** a no-database test computes the set of files under `db/migrations/` (excluding `*_sqlite.sql`) matching `^DROP TABLE`, and asserts every filename in that set appears verbatim in the module docstring.

**Tenant provisioning (requires a reachable Postgres via `DATABASE_URL`):**

- [ ] After `migrate_pg.py` completes, `SELECT COUNT(*) FROM tenants WHERE id = <BOOTSTRAP_TENANT_ID>` returns a row, and the seeded value is byte-identical to `core.graph.pg.BOOTSTRAP_TENANT_ID` (asserted in the test, not eyeballed).
- [ ] **A dependent insert referencing it succeeds:** an `audit_log` insert using `core.graph.pg.BOOTSTRAP_TENANT_ID` commits without an FK error, and the same row shape with a random unseeded UUID raises a `ForeignKeyViolation`.
- [ ] `resolve_or_create_tenant(conn, <fresh UUID>)` returns that UUID, creates a `tenants` row where none existed, and a second call with the same UUID creates none and returns the same value (compare row counts before/after, not against a literal).
- [ ] **NULL-tenant fallback:** a writer called with `tenant_id=None` inserts a row whose `tenant_id` equals `BOOTSTRAP_TENANT_ID` and does not raise. (integration)
- [ ] A no-database test asserts `core.graph.pg.BOOTSTRAP_TENANT_ID` imports **without a database present**, parses as a valid UUID, and is a module-level literal (no `uuid4()` call in `core/graph/pg.py` — grep-asserted), so feature 16's `DEFAULT_TENANT_ID` can bind to it by identity.

**Writers:**

- [ ] `core/graph/audit.py` exists; `log_resolution` inserts one `audit_log` row per call — asserted as a before/after row-count delta equal to the number of calls the test makes, with `category` and `tenant_id` populated. (integration)
- [ ] `grep -nE "\b(UPDATE|DELETE)\b" core/graph/audit.py` returns no match — append-only enforced in code.
- [ ] **No code path passes a non-UUID string into `actor_id`.** (a) a no-database test greps `core/graph/audit.py` and `core/graph/approvals.py` and confirms `actor_id` is only ever bound to `None`/SQL `NULL`; (b) an integration test calls `log_resolution` with a deliberately non-UUID actor string and asserts the insert succeeds with `actor_id` NULL.
- [ ] `record_approval_decision` inserts an `approval_decisions` row whose `disposition` satisfies the table's CHECK, for every key in the mapping. A no-database unit test parses the CHECK set out of `db/schema.sql` by grep and the producer set out of feature 10's resolution module by grep, then asserts `set(MAPPING.values()) <= CHECK_SET` and `set(MAPPING.keys()) == PRODUCER_SET`. Neither set may appear as a literal in the test. (mapping: no database; insert: integration)
- [ ] With `DATABASE_URL` unset, the added Stage 6 calls are no-ops and Stage 6 behaves as feature 10 ships it — covered by the restated feature-10-suite criterion above.
- [ ] `scripts/reconcile_stores.py` exits 0 on a healthy tenant **whose SQLite alias and edge row counts deliberately exceed its `audit_log` row count** (drive at least one resolution end to end) — proving coverage, not count equality. It exits non-zero with a per-identifier diff line when a mutated canonical identifier loses its last audit row, and exits 0 with an informational line only when an audit row exists for a resolution whose graph mutation did not land. Timestamps are compared after cross-engine normalization. (integration)

**The integration tier must actually have run (F3 gate):**

- [ ] `DATABASE_URL=<from .env> .venv/bin/python -m pytest tests/test_pg_bootstrap.py tests/test_audit_pg.py -m integration -x --tb=short` exits 0 **and** its collected count is greater than zero, asserted from the same run's output.
- [ ] **This feature may NOT be marked SHIPPED while the number of integration-marked tests that actually EXECUTED is zero.** The number executed is read from the run's own collection-and-outcome output — the count of integration-marked tests that reported a passed or failed outcome, i.e. collected-and-selected minus skipped and deselected. It is **never** compared against an expected total, a baseline, or any number written in this brief; the only assertion is *strictly greater than zero*. An all-skip run, a fully-deselected run, and a zero-collection run each fail this criterion regardless of exit code. A skip is not a pass.

---

## Dependencies

- [ ] **Feature 2 (canonical-schema) — SHIPPED**, in the narrow sense that it authored `db/schema.sql` and `db/migrations/001_canonical_schema.sql`. **Those files have never been executed** — per rules §0 they are `[PLANNED]`. This feature is the first to run them. Feature 2 remains the owner of any type change to `db/schema.sql`.
- [ ] **Feature 10 (resolution-graph-update) — SHIPPED.** It ships the Stage 6 call site, the terminal disposition vocabulary, and the decision payloads. This feature edits that module and retires a named subset of that suite's guards — see the cross-feature ownership note.
- [ ] **Feature 10b (pending-decision-persistence) — SHIPPED.** It added a SQLite-only migration (consuming a numeric prefix with no Postgres counterpart) and a pending-decisions store whose terminal vocabulary matches this feature's `approval_decisions` CHECK. Consequences, all handled above: prefix computation spans the whole migrations directory; no Postgres mirror and no manifest entry for its migration; no edit to the parity test's shared-table literal; and its own requirements-diff guard is in the retired set.
- [ ] **A reachable Postgres — PROVISIONED, by the developer, outside this feature.** Major version 17 (no patch pin anywhere). Reached only via `DATABASE_URL` from a gitignored `.env`; this feature installs, starts, and containerises nothing, and adds no CI service container. The DSN is supplied by the developer, is never committed, and the code never learns anything about the target beyond what the driver needs. Absent the variable, the integration criteria skip — and per the F3 gate above, a skip-only run cannot ship this feature.
- [ ] **`python-dotenv`** — already pinned; the repo's existing lazy-guarded `load_dotenv` convention is reused, not replaced. No new pin.
- **Downstream:** feature 16 (`connectors-audit-infra`) depends on this feature for the Postgres path; its queue row already points at 10a — edit nothing there. Feature 16 binds `DEFAULT_TENANT_ID` to `BOOTSTRAP_TENANT_ID` by import and asserts identity, appends its own migration filename to `db/migrations/postgres.manifest`, uses the `pg_conn` fixture, and relies on `pytest.ini`'s `integration` marker. Nothing here may rename, relocate, or database-couple `BOOTSTRAP_TENANT_ID`, `is_available`, `connect`, `pg_conn`, or the manifest's append-at-end semantics.

---

## Estimated Complexity

**Rating:** L

**Rationale:** Every item is a *first* for this repo: the first Postgres driver, the first `DATABASE_URL`, the first migration ever executed, the first pytest configuration file, the first test tier requiring external infrastructure, and the first application code writing to a second engine — one whose location is a deployment detail the code is forbidden to know. Risks priced in: more than one shipped migration is destructive on re-execution and all are contained by the runner rather than rewritten, with the destructive-file guard the only safety net since the target is never assumed disposable; the split-store write path has no shared transaction and is mitigated by write ordering plus a *coverage* reconciliation; the audit/actor type mismatch is sidestepped by writing NULL; and a live credential is in play, so secret hygiene is a hard criterion rather than a convention. The load-bearing risks are that adding `pytest.ini` changes rootdir and collection for a suite that has never had a config file, and that this feature must retire shipped guard tests in the same commit as the change that contradicts them.

---

## PROJECT CONTEXT

### Pipeline position

```
Stage 6 Resolution (feature 10, SQLite)
    └─ if the availability helper reports available (THIS FEATURE):
         1. Postgres: audit row                 ← log_resolution()          (INSERT-only, written FIRST, actor_id NULL)
         2. Postgres: approval decision         ← record_approval_decision()
         3. SQLite:   graph mutation            ← feature 10's transaction
       else: steps 1-2 are no-ops; Stage 6 behaves exactly as feature 10 ships it.
```

### Implementation Notes (constraints for the build)

1. **The database is whatever `DATABASE_URL` names, and the code must work unchanged against any of them.** Hardcode no host, no port, no database name, no username; build no DSN from parts; assume no password component; assume nothing about where the server runs. Provision nothing — no `docker`, no `initdb`, no server-start instructions, no `testcontainers`, no CI service container. Absence of `DATABASE_URL` means *not available*, never an error. Postgres major version 17; never pin a patch version.
2. **Treat `DATABASE_URL` as a live secret regardless of what the current value contains.** Never log it, never put it in an exception message, never write it under `features/` or `.rocket/`, never bake it into a fixture or snapshot. `.env` is gitignored; `.env.example` carries only an obviously fake placeholder. See the secret-hygiene criteria.
3. **Set an explicit connect timeout on every connection, from a named module-level constant.** This brief pins no value: adopt an existing repo timeout convention if a build-time grep finds one, otherwise choose one and name it. An untimed connect can hang forever on any target, which is why this is unconditional; and no code or test may assume any particular connect or query latency.
4. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` import fails to collect.
5. **An unregistered or non-matching marker fails silently, not loudly.** Any criterion that "passes" by selecting zero tests is false signal — hence the collected-count-greater-than-zero assertions, and the separate F3 gate on tests that actually *executed*.
6. **Do not put forbidden tokens into feature 10's shipped modules.** The Stage 6 call site reaches the database only through the availability helper and the writer functions. The forbidden-token set and the covered-path set are read from the shipped guard's own regex at build time and never transcribed. The write-ordering rationale is documented in this feature's new modules, not in feature 10's.
7. **Retire the requirements-diff guards in the same commit as the pin.** Do not commit the pin separately to launder the diff — that defeats the guard rather than resolving it. Enumerate the retired set in the diff; write no count of it anywhere.
8. **Never claim a shipped migration is idempotent.** Derive the destructive set with `grep -lE '^DROP TABLE' db/migrations/*.sql` (excluding `*_sqlite.sql`) whenever you need it. Idempotency here means *the runner does not re-run an applied file*.
9. **Migration prefixes are shared with SQLite-only migrations.** Compute the next free prefix over the whole directory, `_sqlite` files included — feature 10b's SQLite-only migration owns a prefix with no Postgres counterpart. Never write a number into a brief or a comment.
10. **`tenant_id` mismatch between engines.** The Postgres columns are `UUID NOT NULL REFERENCES tenants(id)`; the SQLite convention is a nullable `tenant_id TEXT` and fixtures load NULL. Writers fall back to `BOOTSTRAP_TENANT_ID` when the caller passes `None`, and call `resolve_or_create_tenant` otherwise. This is the most likely first-run failure, and feature 16 depends on the seeded row.
11. **`actor_id` is UUID; the in-tree actor is a plain string. Write NULL.** Do not coerce, do not hash into a UUID, do not widen the column.
12. **Write Postgres before SQLite.** The ordering is the mitigation. A timeout on the Postgres write must fail before the SQLite mutation, not skip past it.
13. **Reconciliation is coverage, never count equality**, and must normalize timestamps across the two engines before comparing.
14. **Integration tests roll back at teardown.** The target is never assumed disposable; a committing test pollutes whatever database the runner was pointed at, and feature 16's live seam assertion runs against the same one.
15. **`--strict-markers` is deliberate.** It converts the class of bug this feature exists to fix into a hard error for everyone after.
16. **Do not key approval dispositions on `core.matching.types.Action`.** Key on the terminal disposition strings feature 10's resolution module writes, derived by grep at build time; normalize case into the CHECK vocabulary.
17. **Cite symbols, not coordinates.** No `file.py:NNN`, no counts, no "exactly N" claims about the tree anywhere in this feature's code, comments, tests, or logs. Every quantitative check is an invariant computed at build or test time.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — remains the only store for canonical/alias/edge/system-reference data, and for feature 10b's pending-decision rows. This feature does not move any of it.
- `[PLANNED → this feature]` Postgres at runtime: driver, `DATABASE_URL`, migration execution, `approval_decisions`, `audit_log` — against whatever Postgres `DATABASE_URL` names.
- `[PARTIAL] → code-enforced here` Audit log append-only. `audit.py` exposes INSERT only. The "append-only" wording in `db/schema.sql` is a **SQL comment** — grep-assert that file contains no `CREATE TRIGGER`, no `CREATE RULE`, and no `REVOKE` touching `audit_log`. Do not cite the comment as an enforced constraint.
- `[PLANNED]` RLS. No `CREATE POLICY` anywhere; no query filters on `tenant_id`. New writes carry `tenant_id` as a value; they are not RLS-scoped. No target confers RLS for free — none is configured and none is added here.
- `[PLANNED]` Auth. `supabase==2.9.0` stays unimported and no vendor SDK is used; the target is consumed purely as a Postgres endpoint. `api/` remains stubs. `actor_id` staying NULL is consistent with there being no authenticated principal in V1.

### Relevant Spec Sections

- Section 8: System Architecture (audit trail non-negotiable, idempotency everywhere)
- Section 9: Stage 6 — Resolution + Graph Update (the decision records persisted here)
- Section 10 / rules §10: Data security posture — audit log append-only, tenant scoping, credential handling
