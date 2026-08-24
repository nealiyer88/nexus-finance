# Feature Brief: Postgres Bootstrap — Driver, Connection Module, Test Config, Migration Runner

**Author:** Neal Iyer
**Date:** 2026-08-23 (split: the live-database half moved to feature 10c)
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10a

---

## Problem Statement

`db/schema.sql` describes a Postgres store that **has never been executed anywhere**. Per `.claude/rules/01-nexus-finance-v1.md` §0 it is `[PLANNED]` and must be treated as not existing: no code imports a Postgres driver, `requirements.txt` pins none (`supabase==2.9.0` is pinned but never imported and is not a driver path), nothing reads a `DATABASE_URL`, and the only consumer of `db/schema.sql` is `tests/test_schema_parity.py`, which parses it **as text**. This feature builds the half of that path that **needs no database to build, run, or verify**: a pinned driver, a connection module, a registered pytest marker, a migration runner and its manifest, and the disposition mapping. Feature 10c executes migrations, ships the writers, and wires Stage 6. **Everything here must be fully verifiable with no server reachable and `DATABASE_URL` unset**; nothing here opens a connection during its own tests.

---

## The database target — decided, not to be re-litigated

**Postgres for this repo is whatever `DATABASE_URL` points at, and nothing in this feature is allowed to care which server that is.**

- **Minimum Postgres major version 17** — a minimum major version, **never a pinned patch version** anywhere. `gen_random_uuid()` is a built-in there, so no `CREATE EXTENSION` step is required.
- **The target is supplied entirely by the environment.** No module, test, comment, fixture, docstring, printed line, or sentence of this brief may contain a host, a port, a database name, or a username; may build a DSN out of parts; or may assume the DSN carries a password component. Anything that would have to change when the DSN changes is a defect. Provisioning is not this feature's business: no Docker, `initdb`, `testcontainers`, CI service container, or server-start instructions.
- **Absence of `DATABASE_URL` means "Postgres not available" — never an error.** The developer supplies the real value in a gitignored `.env`; only `.env.example` is committed. Never invent a default DSN and never fall back to a built-in target.
- **`.env` loading reuses the repo's existing loader convention.** Derive it at build time by grepping the tree for an existing `load_dotenv` call: in this repo it is a **lazy, guarded import inside the function that needs it**, so the module stays importable with the package absent. Add no new loader and no new `requirements.txt` line for one.
- **Every connection carries an explicit connect timeout.** An untimed connect can hang a caller indefinitely on any target. **This brief pins no value:** grep at build time for an existing outbound-client timeout convention and adopt it; failing that, choose one and define it as a single UPPER_CASE module-level constant in `core/graph/pg.py`, overridable by an environment variable. Never an inline literal at a call site. No code or test may assume any particular connect or query latency.

### SECRET HANDLING — hard requirement

`DATABASE_URL` is a live credential at all times, **whether or not the current value happens to carry a password**, and no code path may branch on which. Without exception it is **never logged** at any level or in any `print`; **never echoed in an error message** (`connect()` may name the *variable*, never its *value* — driver exceptions embedding connection parameters are caught and re-raised scrubbed); and **never written to any file under `features/` or `.rocket/`**, nor into a fixture, snapshot, log, migration, or committed config. The value and every component it may carry appear **nowhere in this brief** by design.

---

## Scope

### Cross-feature ownership note (read first)

This feature **retires a named set of shipped guard tests** and edits no other feature's module.

**The requirements-diff guards ARE retired, in the same commit that adds the driver pin.** Feature 10's shipped suite — and, since 10b landed, feature 10b's suite as well — contains tests asserting the working-tree diff of `requirements.txt` is empty. This feature must add a driver pin to that exact file. **The two cannot hold in one working tree**, and the gate runs pre-commit against the working tree, so the build fails outright. Committing the pin separately would only launder the diff to empty and defeat the guard's purpose; that is explicitly forbidden.

- **Retire every shipped test that asserts the `requirements.txt` diff is empty**, in the **same commit** that adds the pin. Derive the set at build time by grepping `tests/` for tests that shell out to `git diff` against `requirements.txt` and assert emptiness — do not rely on this brief for the list; at minimum it spans feature 10's suite and feature 10b's suite, and a later feature may have added another.
- Each retired test is **deleted outright — never skipped**. A skipped guard still matches the grep in the success criteria below, so skipping would contradict the criterion; deletion is the only permitted form of retirement. The retired set is enumerable from the diff alone. No count of retired tests is written anywhere.
- **Feature 10's Postgres-token grep-guard is NOT touched here.** This feature adds no call site into feature 10's shipped modules; 10c owns that edit and that guard's survival assertion.

### In Scope

- **Driver dependency.** Add a single pinned Postgres driver to `requirements.txt`: `psycopg[binary]==<pin>`. Confirm at build time by grep that no other Postgres driver is already pinned. The binary wheel avoids any local `libpq` or C toolchain requirement. **`.env.example` gains a `DATABASE_URL` line** with a **clearly fake placeholder**, in the file's existing commented-section style: every component it carries must be obviously synthetic, must not resolve to any real server, and must not read as a syntactically-plausible live credential.

- **`core/graph/pg.py`** — one small module, importable with no database present:
  - `get_dsn() -> str | None` — reads `DATABASE_URL`, loading `.env` first via the repo's lazy-guarded loader convention. Returns `None` when unset. **Never raises, never invents a default.**
  - `connect() -> psycopg.Connection` — opens a connection carrying the explicit connect timeout. Raises `RuntimeError` **naming the variable `DATABASE_URL`** when the DSN is absent; the message never contains the value.
  - `is_available() -> bool` — the single availability helper. Used by test skip guards and, in 10c, by the Stage 6 call site, which reaches Postgres through nothing else.
  - `BOOTSTRAP_TENANT_ID` — a **fixed UUID literal at module level**, not generated, importable with **no database present**, so it is stable across databases and quotable by other features.
  - No pooling, no ORM, no engine abstraction.

- **`pytest.ini` at repo root (NEW).** Derive at build time that the repo has no pytest configuration of any kind, then create one. An unregistered `@pytest.mark.integration` makes pytest emit a warning rather than an error, `-m integration` **deselects everything**, and the run **exits 0 with a zero-test selection** — a silent pass, not a loud failure. Every criterion touching the integration tier therefore asserts a **collected count greater than zero**; a bare exit code is never sufficient.

  ```ini
  [pytest]
  testpaths = tests
  addopts = --strict-markers
  markers =
      integration: requires a reachable Postgres via DATABASE_URL
  ```

  `--strict-markers` turns any future unregistered marker into a collection **error**. Adding a `pytest.ini` fixes rootdir at the repo root and sets `testpaths`/`addopts` for every future run; rootdir is *not* what puts the repo root on `sys.path` — that comes from invoking `.venv/bin/python -m pytest`. The config still changes collection, so the full existing suite must be re-verified under it.

- **Migration runner — `scripts/migrate_pg.py` — and `db/migrations/postgres.manifest` (NEW).** The runner is authored and unit-verified here; **executing it against a live server is 10c's**.

  **SELECTION RULE (the one mechanism; no other rule applies):** *the runner applies exactly the filenames listed in the manifest, in the order they appear there, and never reads or applies any other file in `db/migrations/`.* The manifest is a newline-delimited list of bare filenames; blank lines and `#` comment lines are ignored. If it names a file absent from disk, the runner exits non-zero before applying anything.

  **AUTHORING RULE (resolves at build time; no list is hardcoded here).** Enumerate `db/migrations/*.sql` and author the manifest as: every file whose name does **not** end in `_sqlite.sql`, in ascending filename order. 10c appends its tenant-bootstrap migration last. Earlier queued features keep adding migrations, so the list is derived once and then frozen as a reviewed static file — nothing is ever applied that a human did not write into it. Two mechanically checkable invariants:
  - It contains **no entry matching `_sqlite`**. Every `*_sqlite.sql` file is a SQLite-dialect file loaded only by SQLite tests, never applied to Postgres. Derive that set by globbing; name no file here.
  - It contains every non-`_sqlite` `.sql` file present at build time — assert as a **set comparison**, never a count.

  **MIGRATION PREFIXES are shared with SQLite-only migrations.** Feature 10b shipped a **SQLite-only** migration that consumes a numeric prefix with **no Postgres counterpart**, so computing "next free prefix" over the Postgres subset alone collides with a shipped file. Therefore the next free prefix is computed over **every file in `db/migrations/`, `_sqlite` siblings included** — one above the highest prefix present across the whole directory. **This brief writes no number and no migration filename.** Invariant: after this feature lands, **no two files in `db/migrations/` share a numeric prefix unless they are a dialect pair of the same migration** — one filename is the other's `_sqlite` sibling. A SQLite-only migration with no counterpart owns its prefix outright.

  **10b's table does not enter the Postgres path.** This feature adds **no** Postgres mirror of it, **no** manifest entry for it (its filename matches `_sqlite` and is excluded above), and makes **no** edit to `tests/test_schema_parity.py` or its shared-table literal. Assert at build time by importing the parity module and confirming 10b's table name is absent from that literal — and leave it absent.

  Runner behaviour: creates `schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())` **before** applying anything; **skips any file already recorded**, so a file is applied at most once ever; prints one line per file (`applied` / `skipped`) and exits 0 on success, with **no printed line containing the DSN on any path including failures**; and supports `--dry-run`, which prints the plan and applies nothing.

  **DESTRUCTIVE-FILE GUARD.** Before applying the canonical-schema migration — identified at build time as the manifest entry whose file creates `canonical_entities` — the runner checks whether `canonical_entities` already exists while `schema_migrations` holds no row for that filename. If so it **refuses to run**, exits non-zero, and prints the reason. This is the case where a database was created some other way and re-running would silently destroy it. **This guard is the primary safety mechanism**: the runner must assume the target holds data someone cares about and must never treat it as disposable.

  **"Idempotent on second run" was false and is not repeated.** The canonical-schema migration opens, inside a single `BEGIN`, with a block of **unconditional** `DROP TABLE IF EXISTS ... CASCADE` statements. `IF EXISTS` suppresses the error when the table is absent; it does **not** make the statement conditional on anything else. The `DROP` block is unconditional and the `CREATE TABLE` statements are only partly `IF NOT EXISTS`-guarded. Derive which is which at build time — `grep -nE '^DROP TABLE'` gives the destructive set (non-empty); comparing `grep -cE '^CREATE TABLE'` against `grep -cE '^CREATE TABLE IF NOT EXISTS'` gives the split, where the first is strictly greater and the guarded set is a proper subset. Assert that **relationship**, never the membership and never either number. Enumerate table names by grep at the moment of use. **More than one shipped Postgres migration is destructive** — derive the full set with `grep -lE '^DROP TABLE' db/migrations/*.sql`, excluding `*_sqlite.sql`, and do not write the set down.

  **Resolution: the runner never re-runs an applied file, and the shipped migrations are left byte-unchanged.** Idempotency is redefined at the **runner** level in every criterion: *a second run applies zero files and exits 0.* It is explicitly **not** claimed that re-executing any shipped migration is safe. Rewriting a `DROP` block belongs to the owning schema feature.

  **DATA-LOSS HAZARD — flag prominently in the runner's module docstring:** running a destructive migration by hand, or deleting its `schema_migrations` row and re-running, **destroys all data in the tables that migration drops**. Assume no escape hatch and no automatic snapshot. The docstring must **name every migration file containing an unconditional `DROP TABLE`**, produced at build time by the grep above — no list is copied out of this brief.

- **Disposition mapping — `core/graph/dispositions.py` (NEW), no database required.** A `MAPPING` dict from a terminal decision value to a member of the `approval_decisions.disposition` `CHECK` set. 10c's approvals writer imports it.

  **THE TERMINAL VOCABULARY — DECIDED, NOT TO BE RE-LITIGATED.** The terminal set is **the value list of the `status` `CHECK` constraint feature 10b ships in its pending-decisions SQLite migration, minus that constraint's single non-terminal "still pending" value**. 10b already reuses this vocabulary for `approval_decisions`, so keying on it needs no translation layer. Two earlier revisions tried to source it elsewhere; do not reopen the question.
  - **Locate the migration by listing `db/migrations/` at build time** and selecting the SQLite-dialect pending-decisions migration. **This brief writes neither its filename nor its prefix.** **Parse the `CHECK`'s value list out of that file at build time** and drop the non-terminal value; the remainder is `TERMINAL_SET`. That parse, not this brief, is the authority — **no value is transcribed into this brief, into code, or into a test as a literal.** Nothing here edits 10b's migration, module, or tests.
  - **Do not key on `core.matching.types.Action`.** It is a `typing.Literal` of Stage 4 **routing** bands, at least two of which denote *not yet decided*; it is not an `Enum`, so it has no member iteration and no `terminal` attribute.
  - Read the allowed set out of `approval_decisions.disposition`'s `CHECK` in `db/schema.sql` **by grep at test time**, never as a literal list. Keys and values are **different obligations**: `set(MAPPING.keys()) == TERMINAL_SET` is a **set EQUALITY**, so an empty or partial mapping fails loudly; `set(MAPPING.values()) <= CHECK_SET` stays a **subset** assertion. Both right-hand sides are derived at run time. **No producer-set assertion of any kind exists anywhere in this feature** — that construct made this criterion vacuous twice.
  - **The mapping must COVER the full terminal set.** Every terminal value is a key. There is no exclusion rule of any kind: nothing is left out on the grounds of having no in-tree producer, and no producer may be invented either. Producer presence is simply irrelevant to this mapping.
  - **This is an identity mapping — there is no translation work to do.** The terminal set and the `disposition` `CHECK` set **coincide** in this repo, so each terminal value maps to the identically-spelled CHECK value. Do not go hunting for a transformation that is not there, and do not add a case-normalization or aliasing layer. Confirm the coincidence at build time by comparing the two derived sets rather than by transcription; if they ever diverge, the two invariants above still govern — keys equal the terminal set, values stay inside the CHECK set.
  - **NON-VACUITY — asserted directly.** `MAPPING` must be **non-empty**, and that is asserted on its own line, independently of the set assertions. A subset assertion is satisfied vacuously by the empty set; the key equality plus this explicit non-emptiness check make an empty or quietly hollowed-out mapping fail.

- **Feature 16 seam.** Feature 16 binds `DEFAULT_TENANT_ID = BOOTSTRAP_TENANT_ID` and asserts **identity**. Nothing in this feature may make `BOOTSTRAP_TENANT_ID` require a database to import, rename it, or turn it into a computed value.

- **Tests:** non-integration tests only — marker registration and strict-marker behaviour, the connection module's no-DSN paths, the manifest invariants, the runner's docstring and dry-run paths, the disposition mapping, secret hygiene, and the guard-retirement invariant. **This feature adds no `@pytest.mark.integration` test and opens no connection.**

### Out of Scope

- **Executing migrations against a live server, tenant provisioning, the writers, the Stage 6 call site, the `pg_conn` fixture, `scripts/reconcile_stores.py`, and the integration tier** — all feature 10c.
- **Any database-provisioning story.** No Docker, `docker compose`, `testcontainers`, `initdb`, server-install or server-start instructions, no CI service container.
- **Mirroring feature 10b's pending-decisions table into Postgres**, and any edit to 10b's module, migration, tests, or to the parity test's shared-table literal.
- **Rewriting any shipped migration** to remove its `DROP TABLE ... CASCADE` block; **any `db/schema.sql` type change** (feature 2 owns those); **down-migrations / rollback** (forward-only).
- **Connection pooling, ORM, async, or any vendor SDK.** One driver, one `connect()`; `supabase==2.9.0` stays unimported.
- **Inventing an in-tree producer for any `disposition` CHECK value.** The mapping covers the terminal set regardless of what emits those values today; adding emitters is not this feature's work.

---

## Success Criteria

**Marker registration (no database required):**

- [ ] `pytest.ini` exists at repo root and registers `integration`; `.venv/bin/python -m pytest --markers` output contains a line starting `@pytest.mark.integration`.
- [ ] With `--strict-markers` active, a test decorated with a deliberately bogus marker causes a **collection error**, asserted in a subprocess test expecting a non-zero exit.
- [ ] `.venv/bin/python -m pytest tests/ -m "not integration" -x --tb=short` exits 0 with `DATABASE_URL` unset **and reports a collected count greater than zero**, parsed from the same run's output.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` exits 0 under the new `pytest.ini`, and its collected count is **greater than or equal to** the count from the same command on the prior commit — both captured at build time from the runs themselves. No literal count is written into any test, and no count is expected to *decrease*: this feature retires guards but also adds test files.

**Guard retirement (no database required):**

- [ ] **Restated "feature 10's suite still passes":** every test in feature 10's shipped suite that this feature does not explicitly retire passes under `.venv/bin/python -m pytest tests/test_resolution.py -x --tb=short`, with `DATABASE_URL` unset, and its collected count is greater than zero. The retired set is enumerable from the diff — each retired test is **deleted**, never skipped. No count of retired tests appears in any test or comment.
- [ ] The same holds for feature 10b's suite: `.venv/bin/python -m pytest tests/test_pending_decisions.py -x --tb=short` passes for every test not explicitly retired, with a collected count greater than zero.
- [ ] **No requirements-diff guard remains that contradicts the pin:** a test greps `tests/` for any test asserting the `requirements.txt` diff is empty and asserts the result set is empty. Derived by grep, never against an expected number.
- [ ] **REVIEWER / CHECKLIST OBLIGATION — not a pytest assertion.** The driver pin and the guard retirements must land in the **same commit**. The reviewer confirms, at review time, that the commit's changed-path set contains both `requirements.txt` and every file that held a retired guard. This is deliberately a human step: the gate runs **pre-commit against the working tree**, where that commit does not yet exist, so no test in this suite can inspect it — any attempt to write one would be asserting against something unavailable. The substance is unchanged and unconditional: pin and retirements land together, and committing the pin separately to launder the diff is forbidden.

**Driver + connection (no database required):**

- [ ] The `requirements.txt` diff adds a Postgres driver pin matching `^psycopg\[binary\]==` and **removes no line** — the added-line set is non-empty, every added line matches that pattern, and the removed-line set is empty. `.venv/bin/python -c "import psycopg"` succeeds after `pip install -r requirements.txt`. A grep for any second Postgres driver pin matches nothing.
- [ ] `.env.example` contains a `DATABASE_URL=` line whose value is a placeholder: a test asserts it does not parse as a usable credential and that every component it carries is obviously synthetic. The test asserts nothing about which components are present. `.env` remains gitignored and untracked (`git check-ignore .env` succeeds; `git ls-files .env` returns nothing).
- [ ] With `DATABASE_URL` unset: `core.graph.pg.get_dsn()` returns `None`, `is_available()` returns `False`, and `connect()` raises `RuntimeError` whose message contains the literal string `DATABASE_URL` and **does not** contain any DSN value.
- [ ] **Connect timeout is present and named:** a grep asserts `connect()` passes an explicit connect-timeout argument, that its value is a module-level UPPER_CASE constant in `core/graph/pg.py`, and that no inline numeric literal appears at the call site. No timeout value is asserted — only that one is set from a named constant.
- [ ] `core.graph.pg.BOOTSTRAP_TENANT_ID` imports **with no database present**, parses as a valid UUID, and is a module-level literal (no `uuid4()` call in `core/graph/pg.py` — grep-asserted), so feature 16's `DEFAULT_TENANT_ID` can bind to it by identity.

**Migration runner and manifest (no database required):**

- [ ] **Manifest covers every Postgres migration on disk:** the set of non-`_sqlite` `.sql` files under `db/migrations/` equals the set of manifest entries — a set comparison computed at test time. Grep-assert the manifest contains no `_sqlite` entry.
- [ ] **Prefix invariant:** no two files in `db/migrations/` share a numeric prefix unless one is the other's `_sqlite` dialect sibling — computed by listing the directory at test time.
- [ ] `scripts/migrate_pg.py`'s module docstring contains the literal words `DROP TABLE` and `data loss` — grep-asserted.
- [ ] **The docstring names every destructive migration, derived not hardcoded:** a test computes the set of files under `db/migrations/` (excluding `*_sqlite.sql`) matching `^DROP TABLE`, and asserts every filename in that set appears verbatim in the module docstring.
- [ ] The manifest parser is unit-tested directly with `DATABASE_URL` unset: blank lines and `#` comments are ignored, order is preserved, and a manifest entry naming a nonexistent file makes the runner exit **non-zero** naming the missing filename before opening any connection.
- [ ] The parity test's shared-table literal is unchanged and still omits 10b's table — asserted by importing the parity module.

**Disposition mapping (no database required):**

- [ ] A test parses the `CHECK` set out of `db/schema.sql` by grep, parses `TERMINAL_SET` out of the `status` CHECK in 10b's pending-decisions SQLite migration (located by listing `db/migrations/`, never by filename) minus the non-terminal value, then asserts `set(MAPPING.keys()) == TERMINAL_SET` (**equality** — an empty or partial mapping fails here) **and** `set(MAPPING.values()) <= CHECK_SET` (subset). No set appears as a literal in the test.
- [ ] **Non-vacuity:** a separate assertion requires `MAPPING` to be non-empty, standing on its own so no future edit can hollow the mapping out behind a vacuously-true subset check.
- [ ] **Identity, derived not transcribed:** the same test asserts the two parsed sets coincide and that every key maps to a value equal to itself — confirming the mapping is the identity and that no translation layer was introduced.

**Secret hygiene (no database required):**

- [ ] **The DSN never reaches logs or errors:** a grep over `core/graph/pg.py` and `scripts/migrate_pg.py` finds no path that logs, prints, or formats `get_dsn()`'s return value or the raw environment value into a message. A unit test monkeypatches `DATABASE_URL` to a sentinel, exercises every failure path reachable without a database (missing DSN, malformed DSN, connect failure), captures stdout/stderr and the exception messages, and asserts the sentinel appears in none of them.
- [ ] **Scoped to this feature's own diff.** Across the lines **this feature adds** under `features/` and `.rocket/` — derived from this feature's commit diff, added lines only — no added line assigns a value to `DATABASE_URL`. The criterion deliberately does not grep the repo: other briefs already contain illustrative `DATABASE_URL=` text on a clean checkout. The substance is unconditional: the connection string is **never logged, never placed in an error message, and never written to any file under `features/` or `.rocket/`**; the only committed assignment carrying a value anywhere is the obviously-fake placeholder in `.env.example`. When `DATABASE_URL` is unset the check runs against the sentinel and still asserts absence.

---

## Dependencies

- [ ] **Feature 2 (canonical-schema) — SHIPPED**, in the narrow sense that it authored `db/schema.sql` and the canonical-schema migration. **Those files have never been executed** — per rules §0 they are `[PLANNED]`. Feature 2 remains the owner of any type change to `db/schema.sql`.
- [ ] **Feature 10 (resolution-graph-update) — SHIPPED.** This feature retires a named subset of that suite's requirements-diff guards; it makes no other edit to feature 10's files.
- [ ] **Feature 10b (pending-decision-persistence) — SHIPPED.** It added a SQLite-only migration consuming a numeric prefix with no Postgres counterpart, and a pending-decisions store whose terminal vocabulary this feature's mapping keys on. Its requirements-diff guard is in the retired set.
- [ ] **`python-dotenv`** — already pinned; the existing lazy-guarded `load_dotenv` convention is reused. No new pin.
- **No database is required to build, run, or verify this feature.** A reachable Postgres is feature 10c's dependency, not this one's.
- **Downstream:** feature 10c consumes `core/graph/pg.py`, the manifest, the runner, `pytest.ini`'s `integration` marker, and the disposition mapping. Feature 16 binds `DEFAULT_TENANT_ID` to `BOOTSTRAP_TENANT_ID`, appends its migration filename to the manifest, and relies on the `integration` marker. Nothing here may rename, relocate, or database-couple `BOOTSTRAP_TENANT_ID`, `is_available`, `connect`, or the manifest's append-at-end semantics.

---

## Estimated Complexity

**Rating:** M

**Rationale:** Several firsts, but all of them offline: the first Postgres driver pin, the first `DATABASE_URL`, the first pytest configuration file, and a migration runner that is authored and unit-tested without ever connecting. Risks priced in: adding `pytest.ini` changes rootdir and collection for a suite that has never had a config file; the feature must retire shipped guard tests in the same commit as the change that contradicts them; more than one shipped migration is destructive on re-execution and is contained by the runner rather than rewritten; and a live credential is in play, so secret hygiene is a hard criterion rather than a convention.

---

## PROJECT CONTEXT

### Implementation Notes (constraints for the build)

1. **The database is whatever `DATABASE_URL` names.** Hardcode no host, port, database name, or username; build no DSN from parts; assume no password component. Provision nothing. Absence means *not available*, never an error. Minimum major version 17; never pin a patch.
2. **Treat `DATABASE_URL` as a live secret regardless of what the value contains**, and set an explicit connect timeout from a named module-level constant. This brief pins no timeout value; no code or test may assume any particular latency.
3. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`.
4. **An unregistered or non-matching marker fails silently, not loudly.** Any criterion that "passes" by selecting zero tests is false signal — hence the collected-count-greater-than-zero assertions.
5. **Retire the requirements-diff guards in the same commit as the pin — by deleting them, never by skipping them.** Do not commit the pin separately to launder the diff. Same-commit landing is a reviewer obligation, not a pytest assertion, because the gate runs pre-commit. Enumerate the retired set in the diff; write no count of it anywhere.
6. **Never claim a shipped migration is idempotent.** Idempotency here means *the runner does not re-run an applied file*. Derive the destructive set by grep whenever you need it.
7. **Migration prefixes are shared with SQLite-only migrations.** Compute the next free prefix over the whole directory, `_sqlite` files included. Never write a number or a migration filename into a brief or a comment.
8. **Do not key approval dispositions on `core.matching.types.Action`.** Key on the terminal vocabulary decided above, parsed at build time. The mapping **covers the whole terminal set** (keys equal it, asserted as an equality), its values are a subset of the CHECK set, and it is asserted non-empty. The two sets coincide, so it is an identity mapping — add no translation or case-normalization layer.
9. **Do not open a connection anywhere in this feature's tests.** If a criterion needs a live server, it belongs to 10c.
10. **Cite symbols, not coordinates.** No `file.py:NNN`, no counts, no "exactly N" claims about the tree in any code, comment, test, or log. Every quantitative check is an invariant computed at build or test time.

### V1 Hard Constraints (per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — still the only store for canonical/alias/edge/system-reference data and 10b's pending rows. This feature moves none of it and writes to no store at all.
- `[PLANNED → 10a/10c]` Postgres at runtime. This feature ships the driver, `DATABASE_URL`, the runner, and the manifest; 10c executes and writes.
- `[PLANNED]` RLS and auth. No `CREATE POLICY` anywhere; `supabase==2.9.0` stays unimported and `api/` remains stubs.

### Relevant Spec Sections

Section 8 (System Architecture — idempotency everywhere); Section 10 / rules §10 (data security posture — credential handling).
