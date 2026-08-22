# Feature Brief: Resolution + Graph Update (Pipeline Stage 6)

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved (revised 2026-08-22 after reality-check block)
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10

---

## Problem Statement

After disposition (auto-approve or human approval), the graph must be updated: new aliases added, edges created with category metadata, inverted indices updated, and structured training pairs stored. Without this stage, approvals don't compound — the graph never learns, and every sync cycle starts from scratch.

---

## Scope

### In Scope

- Create `core/graph/resolution.py` implementing Stage 6:
  - **Match confirmed:** Add alias to canonical node, create/update graph edge with category metadata (source_category, target_category, weight, approval_count, approved_by, timestamp), update all inverted indices, store structured training pair
  - **New entity confirmed:** Create canonical node, generate canonical_id, add system references, initialize edges to related entities, update indices, store negative training pairs for rejected candidates
  - **Match rejected:** Log as hard negative training pair with full signal breakdown

- Extend `core/graph/entity_store.py` (SQLite) with write methods:
  - `create_canonical_entity(name, entity_type, entity_category, confidence)` → canonical_id
  - `add_alias(canonical_id, value, source, category, confidence)`
  - `create_edge(source_node, target_node, relationship, source_category, target_category, weight, approved_by)`
  - `increment_approval_count(edge_id)`
  - `update_confidence(canonical_id, new_confidence)`

  **Constraint:** `entity_store.py` today is a 557-line read-only module — 13 module-level query helpers, no class wrapper, no shared state, each taking `tenant_id: Optional[str] = None` as the last parameter, and a module docstring that states "no write path". The new writes are module-level functions in the same file following the same `conn: sqlite3.Connection` first-arg / `tenant_id` last-arg shape. **No existing read signature may change**, and the docstring's "no write path" line is updated rather than the read contract.

- **Stand up the Postgres path (NEW — this feature owns it; none of it exists today).** `approval_decisions` (`db/schema.sql:86`, `db/migrations/001_canonical_schema.sql:95`) and `audit_log` (`db/schema.sql:106`, `001_canonical_schema.sql:114`) exist in the **Postgres schema only** and are never created or queried at runtime. Per owner decision (2026-08-22), feature 10 builds against real Postgres rather than mirroring those two tables into SQLite. In scope:
  - **Driver dependency:** add exactly one pinned Postgres driver to `requirements.txt` (`psycopg[binary]`). Nothing Postgres-capable is there today — no psycopg, no SQLAlchemy, no engine. (`supabase==2.9.0` is pinned but never imported and is not a driver path.)
  - **Connection configuration:** a `DATABASE_URL` env var read through `python-dotenv` (already pinned), surfaced by a single connection helper (`core/graph/pg.py`). No code reads a `DATABASE_URL` today.
  - **Migration execution:** a minimal runner (`scripts/migrate_pg.py`) that applies `db/migrations/001_canonical_schema.sql` against the configured database. Idempotent; records applied filenames in a `schema_migrations` table. No migration runner exists today — `001` has never been run anywhere.
  - **Test strategy:** a **marked integration suite** (`@pytest.mark.integration`) that **skips when `DATABASE_URL` is unset**, plus a documented `docker run postgres:16` one-liner in the brief's Implementation Notes for local and CI use. Chosen over testcontainers because it adds no new test dependency and keeps the default `.venv/bin/python -m pytest tests/` green on a machine with no database, which every current contributor path assumes.

- Create `core/matching/training_data.py`:
  - `TrainingPair` dataclass: entity_pair, signal_breakdown, graph_evidence, category_pair, disposition, reasoning_trace
  - `store_training_pair(pair: TrainingPair)` → writes to the **Postgres** `approval_decisions` table (created by the migration runner above; it does not exist in SQLite)
  - Captures both positive (match confirmed) and negative (match rejected) pairs

- Create `core/graph/audit.py`:
  - `log_resolution(canonical_id, incoming_entity_raw, match_type, confidence, signals, category_pair, user_id)`
  - INSERT-only audit entry, written to the **Postgres** `audit_log` table, for every resolution decision

- **Test suite:** `tests/test_resolution.py` (SQLite graph assertions, no database required) + `tests/test_resolution_pg.py` (integration-marked, skipped without `DATABASE_URL`)
  - Seed empty graph, resolve 10 entities from fixtures, verify graph state
  - Assert: aliases added after match confirmation
  - Assert: edge created with correct category metadata
  - Assert: inverted indices updated (new alias findable in subsequent lookups)
  - Assert: existing `entity_store.py` read helpers still pass `tests/test_deterministic.py` and `tests/test_blocking.py` unchanged
  - Assert (integration): training pair stored in `approval_decisions` with full signal breakdown
  - Assert (integration): rejected match produces negative training pair
  - Assert (integration): audit log rows created for every resolution

### Out of Scope

- Approval queue UI — separate feature
- Confidence decay — separate feature (feature 13)
- Write-back to source systems — Shadow Ledger only
- Batch resolution (process 1000 entities at once) — V1 processes sequentially
- Row-level security / tenant policies. No `CREATE POLICY` exists anywhere in the tree and zero queries filter on `tenant_id` today; this feature threads `tenant_id` through the new writes as a column value but does **not** introduce RLS.
- Migrating the SQLite graph store to Postgres. Reads stay on SQLite (see Split-Store Risk).
- Database-enforced append-only on `audit_log` (trigger / `REVOKE` / `RULE`). V1 enforcement is code-level: `audit.py` exposes INSERT only.

---

## Success Criteria

- [ ] `core/graph/resolution.py` exists with `resolve_match()`, `create_new_entity()`, `reject_match()` functions
- [ ] `core/graph/entity_store.py` extended with write methods; **all 13 existing read helpers keep their exact signatures** and the prior suite passes unchanged
- [ ] `requirements.txt` adds exactly one pinned Postgres driver; `core/graph/pg.py` reads `DATABASE_URL` and returns a connection
- [ ] `scripts/migrate_pg.py` applies `db/migrations/001_canonical_schema.sql` against a real database and is idempotent on second run
- [ ] `core/matching/training_data.py` exists with `TrainingPair` dataclass and `store_training_pair()` writing to Postgres `approval_decisions`
- [ ] `core/graph/audit.py` exists with `log_resolution()` function writing to Postgres `audit_log`
- [ ] After match confirmation: alias exists in alias table, edge exists in edge table, inverted index updated (SQLite)
- [ ] After new entity creation: canonical node exists, system_references populated, canonical_id generated (SQLite)
- [ ] After match rejection: negative training pair stored with signal breakdown (Postgres)
- [ ] `audit.py` exposes no UPDATE or DELETE path — append-only is enforced in code, not by a database trigger (none exists; the `db/schema.sql` "append-only" line is a comment)
- [ ] Training data captures all fields: entity_pair, signal_breakdown, graph_evidence, category_pair, disposition, reasoning_trace
- [ ] `.venv/bin/python -m pytest tests/test_resolution.py` passes with no `DATABASE_URL` set (Postgres tests skip, not fail)
- [ ] `.venv/bin/python -m pytest tests/test_resolution_pg.py -m integration` passes with `DATABASE_URL` pointed at a migrated database
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` passes with no regression to the shipped suite

---

## Dependencies

- [ ] Threshold + LLM fallback (feature 9) — input is `Disposition` from `core/matching/disposition.py` (thresholds live at `disposition.py:54-56`; there is no `core/matching/confidence.py`)
- [ ] Entity store read methods (feature 7) — SHIPPED, `core/graph/entity_store.py` (557 lines, read-only)
- [ ] SQLite canonical schema — SHIPPED, `db/schema_sqlite.sql`: exactly 4 tables (`canonical_entities`, `entity_aliases`, `entity_edges`, `system_references`); the only other real SQLite table is `llm_training_data`. These are the write targets for the graph half of Stage 6.
- [ ] **Postgres schema — NOT a satisfied dependency.** `db/schema.sql` and `db/migrations/001_canonical_schema.sql` are marked `[PLANNED]` in `.claude/rules/01-nexus-finance-v1.md` §0 and have never been executed. Per §0, anything not `[BUILT]` is treated as not existing — so **this feature must build it, not assume it.** That is the driver + `DATABASE_URL` + migration-runner work scoped above, and it is the reason the complexity rating rose.
- [ ] A reachable Postgres instance for integration tests (local Docker or CI service). Absent one, the Postgres criteria skip and the feature cannot be verified end to end.

---

## Estimated Complexity

**Rating:** L (raised from M, 2026-08-22)

**Rationale:** The original M assumed all writes landed in SQLite against tables that already existed. They do not: `approval_decisions` and `audit_log` are Postgres-only DDL that has never been run. The owner's decision to build against real Postgres rather than mirror the tables into SQLite means this feature now owns standing up an entire second persistence path from zero — first Postgres driver in `requirements.txt`, first `DATABASE_URL` handling, first migration runner, first integration-test tier with its own CI story. That is additive to the original four files, plus the split-store correctness burden below. The load-bearing risks are the split-store transaction boundary and the fact that no contributor or CI job currently has a database at all.

### Split-Store Risk (named)

Stage 6 will read from **SQLite** (`entity_store.py` — aliases, edges, canonicals) and write approval/audit records to **Postgres** (`approval_decisions`, `audit_log`). Two engines, no shared transaction. A crash between the SQLite graph write and the Postgres audit write leaves an approved alias with no audit trail — precisely the gap the audit requirement exists to close.

Mitigation (honest, not complete): order writes **Postgres audit first, then SQLite graph**, so the failure mode is an audit row for a resolution that did not land (detectable, re-drivable) rather than a graph mutation with no record (undetectable). Make `resolve_match()` idempotent on `(canonical_id, incoming_entity_raw)` so a re-drive is safe. Add a reconciliation check that counts SQLite alias/edge rows against Postgres audit rows. This does not give atomicity — nothing short of a single store does — and the split is expected to be temporary: it collapses when the graph store itself moves to Postgres, which is out of scope here.

---

## PROJECT CONTEXT

### Resolution Outputs (from spec Section 9, Stage 6)

| Decision | Graph Action | Training Data |
|----------|-------------|---------------|
| Match confirmed | Add alias, create/update edge, update indices | Positive pair: entity_pair + signals + disposition |
| New entity | Create canonical node, initialize edges, update indices | Negative pairs: all rejected candidates |
| Match rejected | No graph change | Hard negative: rejected pair + full signal breakdown |

### Idempotency Requirements

- Same entity resolved twice → same canonical_id, no duplicate aliases
- Edge approval_count increments on re-confirmation, not duplicate edge creation
- Training pairs are append-only — duplicate resolution produces new training pair (captures temporal signal)

### Implementation Notes

1. **Local/CI Postgres:** `docker run --rm -e POSTGRES_PASSWORD=nexus -p 5432:5432 postgres:16`, then `DATABASE_URL=postgresql://postgres:nexus@localhost:5432/postgres .venv/bin/python scripts/migrate_pg.py`. CI uses the same image as a service container.
2. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and the console script does not put the repo root on `sys.path`.
3. Postgres tests are `@pytest.mark.integration` and skip on missing `DATABASE_URL` — the default suite must stay green with no database.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — the only engine any code uses today. Stage 6 graph writes go here.
- `[PLANNED → this feature]` Postgres for `approval_decisions` + `audit_log`. Nothing exists at runtime; feature 10 builds the driver, config, and migration path.
- `[PARTIAL]` Audit log append-only. Enforced in V1 by code (`audit.py` exposes INSERT only). The "append-only" line in `db/schema.sql` is a **SQL comment**, not a trigger, `REVOKE`, or `RULE` — do not cite it as an enforced constraint.
- `[PLANNED]` RLS. There is **no row-level security** — no `CREATE POLICY` anywhere, and zero queries filter on `tenant_id` despite the column existing. New writes carry `tenant_id` as a value; they are not RLS-scoped.
- `[BUILT]` Training data capture from Day 1 — load-bearing for V2+ fine-tuning.

### Relevant Spec Sections

- Section 9: Stage 6 — Resolution + Graph Update
- Section 8: System Architecture (idempotency everywhere, audit trail non-negotiable)
