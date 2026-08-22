# Feature Brief: Resolution + Graph Update (Pipeline Stage 6) — SQLite

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved (narrowed 2026-08-22; Postgres infrastructure split out to feature 10a)
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10

---

## Problem Statement

After disposition (auto-approve or human approval), the graph must be updated: new aliases added, edges created with category metadata, inverted indices rebuilt so the next lookup sees the new alias, and structured training pairs stored. Without this stage, approvals don't compound — the graph never learns, and every sync cycle starts from scratch. Stage 4 already ships anticipating this write path (`entity_store.py:are_clustered` docstring: "shipped now so Stage 6 can populate `entity_edges` rows"), and `core/matching/indices.py:12-14` states "Stage 6 owns the write path."

This feature is scoped to the **SQLite store that every runtime and test path in the repo already uses**. It must be buildable, runnable, and fully verifiable on a machine with **no Postgres present**. Standing up Postgres — driver pin, `DATABASE_URL`, migration runner, integration test tier, and the Postgres-only `approval_decisions` / `audit_log` tables — is **feature 10a** (`features/infrastructure/postgres-store-bootstrap.md`) and is deliberately **not** a dependency of this feature.

---

## Scope

### In Scope

- **Create `core/graph/resolution.py`** implementing Stage 6 over SQLite. Input is a `Disposition` (`core/matching/types.py:159-181`) plus a resolved human/auto decision:
  - **Match confirmed** (`resolve_match`): add alias to the canonical node, create or update the `entity_edges` row with category metadata, mark indices stale, store a positive training pair.
  - **New entity confirmed** (`create_new_entity`): create the canonical node, generate `canonical_id`, insert `system_references` rows, initialize edges to related canonicals, store negative training pairs for the rejected candidates.
  - **Match rejected** (`reject_match`): no graph mutation; store a hard-negative training pair with the full signal breakdown.

- **Extend `core/graph/entity_store.py` with write functions** (same module, module-level, `conn: sqlite3.Connection` first / `tenant_id: Optional[str] = None` last, mirroring the read convention):
  - `create_canonical_entity(conn, canonical_name, entity_type, entity_category, confidence, tenant_id=None) -> str`
  - `add_alias(conn, canonical_id, value, source, category, confidence, tenant_id=None) -> int`
  - `add_system_reference(conn, canonical_id, source, category, external_id, external_fields, tenant_id=None) -> int`
  - `upsert_edge(conn, source_node, target_node, relationship, source_category, target_category, weight, approved_by, tenant_id=None) -> int`
  - `increment_approval_count(conn, edge_id) -> int`
  - `update_confidence(conn, canonical_id, new_confidence, tenant_id=None) -> None`

  **Constraint:** `entity_store.py` is read-only by convention today — 557 lines, 13 module-level query helpers, no class wrapper, no shared state, and a module docstring stating "no write path. Stage 6 (resolution / graph update) will add write functions to this same module." **No existing function may change name, parameter order, parameter names, or defaults.** Only the docstring's "no write path" sentence is updated.

- **Column names are taken from the shipped DDL, not from the rules file.** `db/schema_sqlite.sql:32-45` `entity_edges` has `created_at` and **no `approved_at`**, despite rules §4 naming `approved_at`. Writers use `created_at`. Adding `approved_at` is out of scope — `tests/test_schema_parity.py` parity-checks `entity_edges` across `schema.sql` and `schema_sqlite.sql` and a one-sided column would break it.

- **Idempotency, enforced by the schema that already exists:**
  - `entity_aliases` has `UNIQUE (canonical_id, value, source)` (`schema_sqlite.sql:28`) — `add_alias` uses `INSERT ... ON CONFLICT DO NOTHING` and returns the existing `alias_id`.
  - `system_references` has `UNIQUE (source, external_id)` (`schema_sqlite.sql:57`) — `add_system_reference` upserts on that key.
  - `entity_edges` has **no** uniqueness constraint. `upsert_edge` therefore does a `SELECT` on `(source_node, target_node, relationship)` and, when a row exists, calls `increment_approval_count` instead of inserting a second edge.
  - The whole of `resolve_match` runs inside **one SQLite transaction** — a single `conn`, one `BEGIN`/`COMMIT`, rollback on exception. One engine, one transaction: there is no split-store problem in this feature.

- **Create `core/matching/training_data.py`:**
  - `TrainingPair` dataclass: `entity_pair`, `signal_breakdown`, `graph_evidence`, `category_pair`, `disposition`, `reasoning_trace`.
  - `store_training_pair(conn, pair, tenant_id=None)` writes **one row to the SQLite `llm_training_data` table**, which already exists (`db/migrations/002_llm_training_data_sqlite.sql`) and is the only training-capture table any code writes to today (`core/matching/llm_fallback.py:400-428`, `_write_training_row`). **No new DDL is introduced by this feature.** Column mapping is fixed and asserted:

    | Column | Value |
    |---|---|
    | `call_id` | `"resolution:" + sha256(f"{canonical_id}|{incoming_entity_raw}|{iso_timestamp}")[:32]` |
    | `tenant_id` | pass-through, nullable (SQLite convention — fixtures load NULL) |
    | `category_pair` | `f"{source_category}:{target_category}"` (same format as `llm_fallback.py`) |
    | `redacted_prompt` | the entity pair rendered through the shipped `core/matching/redaction.py` scrubber |
    | `prompt_sha256` | `sha256(redacted_prompt)` |
    | `llm_response_json` | `json.dumps(asdict(pair))` — the full TrainingPair payload |

  - `disposition` on `TrainingPair` uses the **`Action` vocabulary already in the tree** (`core/matching/types.py:120`: `AUTO_APPROVE` / `QUEUE_FOR_REVIEW` / `LLM_FALLBACK` / `NO_MATCH`) plus the terminal human outcome (`CONFIRMED` / `REJECTED`). It does **not** use the Postgres `approval_decisions.disposition` CHECK vocabulary (`approved`/`rejected`/`corrected`, `db/schema.sql:94`) — that table does not exist at runtime and is feature 10a's.

- **Index staleness:** `core/matching/indices.py` has no incremental update path. `resolution.py` exposes `mark_indices_stale()` / returns a boolean so the caller (feature 12's orchestrator) rebuilds `TokenIndex`/`NgramIndex`/`EmbeddingIndex` from the store after a write. This feature does **not** add incremental index mutation.

- **Test suite `tests/test_resolution.py`** — pure SQLite, no database server, no network:
  - Loads `db/schema_sqlite.sql` **and** `db/migrations/002_llm_training_data_sqlite.sql` into an in-memory connection, following `tests/test_llm_fallback.py:44-45`.
  - Seeds an empty graph, drives 10 entities from the existing fixtures, asserts final graph state.
  - Asserts alias insertion, edge metadata, edge re-confirmation, canonical creation, index-rebuild visibility, training-pair rows, and read-signature preservation (see Success Criteria).

### Out of Scope

- **All Postgres work — moved to feature 10a** (`features/infrastructure/postgres-store-bootstrap.md`): the driver pin, `DATABASE_URL`, `core/graph/pg.py`, `scripts/migrate_pg.py`, the `pytest.ini` that registers `@pytest.mark.integration`, and the `approval_decisions` + `audit_log` tables. This feature adds **zero** new entries to `requirements.txt` and reads **no** new environment variable.
- **Durable append-only audit log.** Provenance for a graph write is carried by columns that already exist (`entity_edges.approved_by`, `entity_edges.approval_count`, `entity_edges.created_at`, `canonical_entities.created_at`/`updated_at`) plus the append-only `llm_training_data` row per decision. The separate `audit_log` table is Postgres-only and belongs to 10a. Feature 10 delivers its value — the graph compounds and training pairs accumulate — without it.
- **Approval-decision recording to `approval_decisions`** — 10a.
- Approval queue UI — feature 11.
- Confidence decay — feature 13.
- Write-back to source systems — Shadow Ledger only.
- Batch resolution (1000 entities at once) — V1 processes sequentially.
- Adding `approved_at` to `entity_edges`, or any change to `db/schema.sql` / `db/schema_sqlite.sql` / migration 001. No new migration file.
- Row-level security. No `CREATE POLICY` exists anywhere and no query filters on `tenant_id` unless the caller passes one. **`tenant_id` is a column on exactly one table.** Per-table reality, taken from the shipped DDL:
  - `canonical_entities` **has** a `tenant_id` column (`schema_sqlite.sql:8`, nullable). `create_canonical_entity` writes it as a column value; `update_confidence` scopes with `WHERE canonical_id = ? AND tenant_id = ?` when a tenant is passed.
  - `entity_aliases`, `entity_edges`, and `system_references` have **no `tenant_id` column** (`schema_sqlite.sql:20-29`, `:32-44`, `:48-57`). Their `tenant_id=` parameter is a **scoping filter, never an inserted value**: it is applied by joining to the parent `canonical_entities` row, exactly as the shipped reads do — `entity_store.py:11-12` ("alias and system_reference tables join through `canonical_id`"), `get_aliases` (`:265-276`) joins `canonical_entities AS c` and filters `c.tenant_id = ?`, and `are_clustered` (`:530-555`) requires **both** edge endpoints to match the tenant.
  - Therefore `add_alias`, `upsert_edge`, and `add_system_reference` must **never** put `tenant_id` in an INSERT column list — `INSERT ... tenant_id` on those three tables raises `no such column: tenant_id`. They either ignore the parameter or use it to verify the parent canonical is in tenant scope before writing.
- Migrating the SQLite graph store to Postgres.

---

## Success Criteria

Every criterion below runs with **no `DATABASE_URL` set and no Postgres installed**.

- [ ] `core/graph/resolution.py` exists and `from core.graph.resolution import resolve_match, create_new_entity, reject_match` succeeds.
- [ ] **Read-signature preservation:** a test snapshots `inspect.signature()` for every **public** function in `core/graph/entity_store.py` that existed at commit-before-this-feature — **12 public functions**, out of 13 module-level `def`s as of 2026-08-22 (the 13th, `_neighbors` at `entity_store.py:312`, is private and is excluded from the snapshot) — and asserts each is present with an identical string repr. Any rename, reorder, or default change fails the test.
- [ ] `.venv/bin/python -m pytest tests/test_deterministic.py tests/test_blocking.py tests/test_scoring.py tests/test_disposition.py -x --tb=short` passes unchanged (exit 0).
- [ ] After `resolve_match` on a confirmed match: exactly one new `entity_aliases` row exists for `(canonical_id, value, source)`; `entity_edges` has exactly one row for `(source_node, target_node, relationship)` with `source_category`, `target_category`, `weight`, and `approved_by` all non-NULL and equal to the values passed in.
- [ ] **Alias idempotency:** calling `resolve_match` twice with identical input yields `SELECT COUNT(*) FROM entity_aliases` unchanged after the second call, and the same `canonical_id` returned both times.
- [ ] **Edge idempotency:** the second `resolve_match` leaves `SELECT COUNT(*) FROM entity_edges` unchanged and increments that edge's `approval_count` by exactly 1.
- [ ] **Transaction atomicity:** a test injects an exception after the alias insert and before the edge insert; after the rollback, `SELECT COUNT(*)` on both `entity_aliases` and `entity_edges` equals the pre-call value.
- [ ] After `create_new_entity`: a `canonical_entities` row exists with a generated non-empty `canonical_id`, `entity_type` and `entity_category` passing the schema CHECK constraints, and one `system_references` row per supplied source.
- [ ] **Index-rebuild visibility:** after `resolve_match` adds alias `"pacrim tech"` to `CLIENT_XXXX`, rebuilding `TokenIndex` from the same connection and querying `"pacrim tech"` returns `CLIENT_XXXX`. (Asserts the write is visible to a rebuild; incremental index mutation is out of scope.)
- [ ] **Index staleness signal:** `from core.graph.resolution import mark_indices_stale` succeeds and `mark_indices_stale()` returns `True` after a `resolve_match` or `create_new_entity` call that mutated the graph, and `False` after a `reject_match` (which performs no graph mutation) with no preceding mutation in the same test. Asserted directly in `tests/test_resolution.py`.
- [ ] `core/matching/training_data.py` exposes `TrainingPair` with exactly the fields `entity_pair`, `signal_breakdown`, `graph_evidence`, `category_pair`, `disposition`, `reasoning_trace` — asserted via `dataclasses.fields()`.
- [ ] `store_training_pair` inserts one `llm_training_data` row whose `call_id` starts with `resolution:`, whose `category_pair` matches `^[a-z_]+:[a-z_]+$`, and whose `llm_response_json` round-trips through `json.loads` to a dict containing all six `TrainingPair` fields.
- [ ] A rejected match produces a `llm_training_data` row with `disposition == "REJECTED"` inside `llm_response_json` and a non-empty `signal_breakdown`.
- [ ] `store_training_pair` writes **no** person identifier in the clear: for a person-entity pair, the stored `redacted_prompt` contains none of the input name, email, or employee_id substrings (uses the shipped `core/matching/redaction.py`).
- [ ] `training_data.py` and `resolution.py` contain **zero** occurrences of `UPDATE llm_training_data` or `DELETE FROM llm_training_data` — grep-level assertion in the test file. Training capture is append-only in code.
- [ ] `grep -rniE "psycopg|DATABASE_URL|postgres" core/graph/resolution.py core/matching/training_data.py core/graph/entity_store.py` returns no matches; `git diff requirements.txt` is empty.
- [ ] `.venv/bin/python -m pytest tests/test_resolution.py -x --tb=short` passes (exit 0) with `DATABASE_URL` unset and no Postgres running.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` passes (exit 0) with no regression to the shipped suite.

---

## Dependencies

- [ ] **Feature 9 (threshold-llm-fallback) — SHIPPED.** Input is `Disposition` from `core/matching/types.py:159-181`; thresholds live at `core/matching/disposition.py:54-56`. There is no `core/matching/confidence.py`.
- [ ] **Feature 7 (deterministic-blocking) — SHIPPED.** Delivered `core/graph/entity_store.py` (557 lines, read-only) — the module this feature extends. *(Corrected 2026-08-22: the prior brief called row 7 "entity store read methods"; the queue row is `deterministic-blocking`.)*
- [ ] **Feature 2 (canonical-schema) — SHIPPED.** `db/schema_sqlite.sql`: exactly 4 tables — `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references`. These are the graph write targets.
- [ ] **`llm_training_data` SQLite table — SHIPPED** with feature 9 (`db/migrations/002_llm_training_data_sqlite.sql`, verified 2026-08-22; written to at `core/matching/llm_fallback.py:415`). The training-capture write target. Tests must load this file explicitly on top of `schema_sqlite.sql`.
- [ ] **`core/matching/redaction.py` — `[BUILT]`** (rules §10), reused for the `redacted_prompt` column.
- **No Postgres dependency.** Feature 10a is *not* a prerequisite of this feature; 10a depends on 10, not the reverse.

---

## Estimated Complexity

**Rating:** M

**Rationale:** Six write functions added to a 557-line module that is read-only by convention, one new orchestration module, one new dataclass module, and one test file — all against tables that already exist in the engine every code path already uses. The load-bearing risks are (a) not disturbing the 12 shipped public read signatures (13 module-level defs, one of them the private `_neighbors`), and (b) getting idempotency right where the schema does *not* help: `entity_aliases` and `system_references` carry UNIQUE constraints, but `entity_edges` does not, so duplicate-edge prevention is a hand-written SELECT-then-branch inside the transaction. Rated M rather than L precisely because the Postgres path, the driver pin, the migration runner, the integration tier, and the split-store transaction problem were all removed to feature 10a — this feature touches one engine and one transaction.

---

## PROJECT CONTEXT

### Pipeline position

```
Stage 4 Threshold / Stage 5 LLM Fallback  →  Disposition
Stage 6 Resolution (THIS FEATURE, SQLite):
    resolve_match      → entity_aliases + entity_edges (upsert/increment) + llm_training_data
    create_new_entity  → canonical_entities + system_references + entity_edges + llm_training_data
    reject_match       → llm_training_data only (hard negative)
    → indices marked stale; caller (feature 12) rebuilds TokenIndex / NgramIndex / EmbeddingIndex
```

### Resolution Outputs (from spec Section 9, Stage 6)

| Decision | Graph Action | Training Data |
|----------|-------------|---------------|
| Match confirmed | Add alias, upsert edge, mark indices stale | Positive pair: entity_pair + signals + disposition |
| New entity | Create canonical node, initialize edges, mark indices stale | Negative pairs: all rejected candidates |
| Match rejected | No graph change | Hard negative: rejected pair + full signal breakdown |

### Idempotency Requirements

- Same entity resolved twice → same `canonical_id`, no duplicate alias (`UNIQUE (canonical_id, value, source)`).
- Edge `approval_count` increments on re-confirmation; no second edge row (`entity_edges` has no UNIQUE constraint — enforced in code).
- Training pairs are append-only — a duplicate resolution produces a *new* `llm_training_data` row (captures the temporal signal). The `call_id` hash includes the timestamp so the PRIMARY KEY does not collide.

### Implementation Notes (constraints for the build)

1. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` import fails to collect.
2. **Tests build their schema from files, not from Python DDL.** Load `db/schema_sqlite.sql` then `db/migrations/002_llm_training_data_sqlite.sql`, exactly as `tests/test_llm_fallback.py:44-45` does. Never `CREATE TABLE` inside a test.
3. **Named tradeoff — training pairs land in `llm_training_data`, not `approval_decisions`.** `approval_decisions` is Postgres-only DDL that has never run; `llm_training_data` is the only real SQLite training table and its purpose (capture for V2+ fine-tuning) is the same. The column mapping in Scope is the contract. When feature 10a lands, back-filling `resolution:`-prefixed rows into `approval_decisions` is a mechanical migration — the `resolution:` `call_id` prefix exists to make those rows selectable.
4. **`tenant_id` stays nullable everywhere.** SQLite convention is nullable/NULL tenant (`schema_sqlite.sql:8`, `entity_store.py:12-16`, `llm_training_data.tenant_id TEXT`). Do not add NOT NULL or an FK to `tenants` — that table does not exist in SQLite.
5. **Use `created_at`, never `approved_at`.** Rules §4 names `approved_at`; the shipped DDL does not have it. `INSERT ... approved_at` raises `no such column`.
6. **One connection, one transaction, per resolution.** Writers take `conn` and do not commit individually; `resolution.py` owns the commit/rollback boundary.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — the only engine any code uses. All Stage 6 writes in this feature go here.
- `[BUILT]` `llm_training_data` — the only real SQLite training table; the write target for training capture.
- `[BUILT]` LLM redaction (`core/matching/redaction.py`) — reused before persisting any pair text.
- `[PLANNED → feature 10a]` Postgres, `approval_decisions`, `audit_log`, `DATABASE_URL`, any driver. Per §0, treat as **not existing**. This feature must not reference them.
- `[PLANNED]` RLS. No `CREATE POLICY` anywhere. `tenant_id` is a column on `canonical_entities` only; on `entity_aliases` / `entity_edges` / `system_references` it is a join-through-parent filter, never an inserted column (see Out of Scope).
- `[BUILT]` Training data capture from Day 1 — load-bearing for V2+ fine-tuning.

### Relevant Spec Sections

- Section 9: Stage 6 — Resolution + Graph Update
- Section 8: System Architecture (idempotency everywhere, audit trail non-negotiable — the durable audit table lands with feature 10a)
