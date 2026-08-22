# Feature Brief: Resolution + Graph Update (Pipeline Stage 6) — SQLite

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved (narrowed 2026-08-22; Postgres infrastructure split out to feature 10a. De-pinned 2026-08-22 after feature 8b landed: all counts/line-numbers/offsets replaced with runtime-derived invariants; redaction and `llm_response_json` mappings corrected.)
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10

---

## Problem Statement

After disposition (auto-approve or human approval), the graph must be updated: new aliases added, edges created with category metadata, inverted indices rebuilt so the next lookup sees the new alias, and structured training pairs stored. Without this stage, approvals don't compound — the graph never learns, and every sync cycle starts from scratch. Stage 4 already ships anticipating this write path — the `are_clustered` docstring in `core/graph/entity_store.py` says "shipped now so Stage 6 can populate `entity_edges` rows", and the `core/matching/indices.py` module docstring says "Stage 6 owns the write path." Both are locatable by `grep -n "Stage 6" core/graph/entity_store.py core/matching/indices.py`.

This feature is scoped to the **SQLite store that every runtime and test path in the repo already uses**. It must be buildable, runnable, and fully verifiable on a machine with **no Postgres present**. Standing up Postgres — driver pin, `DATABASE_URL`, migration runner, integration test tier, and the Postgres-only `approval_decisions` / `audit_log` tables — is **feature 10a** (`features/infrastructure/postgres-store-bootstrap.md`) and is deliberately **not** a dependency of this feature.

---

## Scope

### In Scope

- **Create `core/graph/resolution.py`** implementing Stage 6 over SQLite. Input is the `Disposition` dataclass imported from `core.matching.types` plus a resolved human/auto decision:
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

  **Constraint:** `entity_store.py` is read-only by convention today — every symbol is a module-level query helper, there is no class wrapper and no shared state, and the module docstring states "no write path. Stage 6 (resolution / graph update) will add write functions to this same module." The authoritative baseline is **whatever `git show HEAD:core/graph/entity_store.py` contains at build time**, not any count written here. **No function present in that baseline may change name, parameter order, parameter names, or defaults**, and no baseline function may be removed. Only the docstring's "no write path" sentence is updated. (The module is several hundred lines and grows with upstream features; do not treat any length or function count as a contract.)

- **Column names are taken from the shipped DDL, not from the rules file.** The `entity_edges` `CREATE TABLE` in `db/schema_sqlite.sql` declares `created_at` and **no `approved_at`**, despite rules §4 naming `approved_at`. Writers use `created_at`. Check with `grep -n "approved_at" db/schema_sqlite.sql` (must return nothing). Adding `approved_at` is out of scope — `tests/test_schema_parity.py` parity-checks `entity_edges` across `schema.sql` and `schema_sqlite.sql` and a one-sided column would break it.

- **Idempotency, enforced by the schema that already exists:**
  - The `entity_aliases` table in `db/schema_sqlite.sql` declares `UNIQUE (canonical_id, value, source)` — `add_alias` uses `INSERT ... ON CONFLICT DO NOTHING` and returns the existing `alias_id`.
  - The `system_references` table declares `UNIQUE (source, external_id)` — `add_system_reference` upserts on that key.
  - Both constraints are verified at build time by reading the shipped DDL (or `PRAGMA index_list` on a loaded in-memory DB), never by line number.
  - `entity_edges` has **no** uniqueness constraint. `upsert_edge` therefore does a `SELECT` on `(source_node, target_node, relationship)` and, when a row exists, calls `increment_approval_count` instead of inserting a second edge.
  - The whole of `resolve_match` runs inside **one SQLite transaction** — a single `conn`, one `BEGIN`/`COMMIT`, rollback on exception. One engine, one transaction: there is no split-store problem in this feature.

- **Create `core/matching/training_data.py`:**
  - `TrainingPair` dataclass: `entity_pair`, `signal_breakdown`, `graph_evidence`, `category_pair`, `disposition`, `reasoning_trace`.
  - `store_training_pair(conn, pair, tenant_id=None) -> Optional[str]` writes **at most one row to the SQLite `llm_training_data` table**, which already exists (`db/migrations/002_llm_training_data_sqlite.sql`) and is the only training-capture table any code writes to today (the writer is the `_write_training_row` helper in `core/matching/llm_fallback.py` — locate it by symbol, not by line). **No new DDL is introduced by this feature.** It returns the `call_id` written, or `None` when no row was written.

  - **Redaction: reuse the Stage 5 prompt; do not build a new one.** `core/matching/redaction.py` exposes only `redact_org(...)`, `redact_person(...)` — both take **fixed positional structural arguments** (categories, entity types, code *shapes*, token-overlap counts, score, `forbidden_tokens`) and return a `RedactedPrompt` — plus `leak_check(text, forbidden_tokens)`. **There is no "render an entity pair through the scrubber" API**, and Stage 6 does not hold the structural arguments those two functions require. Therefore:
    - The only legitimate `redacted_prompt` text for Stage 6 is the one **Stage 5 already built and persisted**. `llm_assess` writes `redacted.text` into `llm_training_data.redacted_prompt` under `call_id = <uuid4 hex>`, and returns a `Disposition` whose `llm_assessment.call_id` is that same value. Stage 6 recovers it with `SELECT redacted_prompt, prompt_sha256, category_pair FROM llm_training_data WHERE call_id = ?` using `disposition.llm_assessment.call_id`, on the same `conn`.
    - **A training row is written ONLY for dispositions that actually went through the LLM.** If `disposition.llm_assessment is None`, or the Stage 5 row is absent for that `call_id`, `store_training_pair` writes **no row** and returns `None`. Auto-approved and NO_MATCH dispositions therefore produce no training row — that is the specified behaviour, not a gap.
    - Stage 6 calls **no** `redact_org` / `redact_person`. It calls `leak_check` only, as a second-pass guard (below).

  - Column mapping is fixed and asserted:

    | Column | Value |
    |---|---|
    | `call_id` | `"resolution:" + sha256(f"{canonical_id}|{incoming_entity_raw}|{iso_timestamp}")[:32]` |
    | `tenant_id` | pass-through, nullable (SQLite convention — fixtures load NULL) |
    | `category_pair` | `f"{source_category}:{target_category}"` (same format as `llm_fallback.py`) |
    | `redacted_prompt` | verbatim copy of the Stage 5 `redacted_prompt` recovered by `llm_assessment.call_id` |
    | `prompt_sha256` | verbatim copy of the Stage 5 `prompt_sha256` for that row (already `sha256(redacted_prompt)`; recomputing it must yield the same value) |
    | `llm_response_json` | `json.dumps({...}, sort_keys=True)` over the TrainingPair **with `entity_pair` excluded** — see below |

  - **`llm_response_json` carries no raw entity pair.** `asdict(pair)` would serialize `entity_pair`, i.e. the unredacted names/emails/IDs, into a column no privacy check covers. Instead the payload is the five non-identifying fields — `signal_breakdown`, `graph_evidence`, `category_pair`, `disposition`, `reasoning_trace` — plus an `entity_pair_ref` object `{"canonical_id": ..., "source_call_id": ...}` standing in for `entity_pair`. The `entity_pair` field stays on the in-memory `TrainingPair` dataclass and is used only to derive the `call_id` hash and the forbidden-token set; it is never serialized.
  - **`store_training_pair` must NOT reuse `llm_fallback._write_training_row`.** That helper calls `conn.commit()` as its last statement (verify: `grep -n "conn.commit" core/matching/llm_fallback.py`), which would commit the half-finished Stage 6 transaction and break the atomicity criterion. `store_training_pair` issues its own `conn.execute(INSERT ...)` with the identical column list and **never calls `conn.commit()` or `conn.rollback()`** — `resolution.py` owns the commit boundary. Grep-asserted: `training_data.py` contains no `.commit(` and no `_write_training_row`.
  - **Both persisted columns are leak-checked.** Before the INSERT, `store_training_pair` builds a `forbidden_tokens` frozenset from the raw `entity_pair` (normalized name, raw name, email, employee-id-like keys, canonical name and aliases — the same field set `_build_forbidden_tokens` in `llm_fallback.py` walks) and runs `leak_check` over **`redacted_prompt` AND `llm_response_json`**. If either fires, the row is **not** inserted and the function raises — a leak is programmer error, matching Stage 5's inbound-leak behaviour.

  - `disposition` on `TrainingPair` uses the **`Action` vocabulary already in the tree** — the `Action` `Literal` alias in `core/matching/types.py`: `AUTO_APPROVE` / `QUEUE_FOR_REVIEW` / `LLM_FALLBACK` / `NO_MATCH` — plus the terminal human outcome (`CONFIRMED` / `REJECTED`). It does **not** use the `disposition` CHECK vocabulary on the Postgres-only `approval_decisions` table (`approved`/`rejected`/`corrected`; find it with `grep -n "disposition" db/schema.sql`) — that table does not exist at runtime and is feature 10a's.

- **Index staleness:** `core/matching/indices.py` has no incremental update path. `resolution.py` exposes `mark_indices_stale()` / returns a boolean so the caller (feature 12's orchestrator) rebuilds `TokenIndex`/`NgramIndex`/`EmbeddingIndex` from the store after a write. This feature does **not** add incremental index mutation.

- **Test suite `tests/test_resolution.py`** — pure SQLite, no database server, no network:
  - Loads `db/schema_sqlite.sql` **and** `db/migrations/002_llm_training_data_sqlite.sql` into an in-memory connection, following the `SQLITE_SCHEMA` / `TRAINING_MIGRATION` path constants in `tests/test_llm_fallback.py`.
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
- Row-level security. No `CREATE POLICY` exists anywhere and no query filters on `tenant_id` unless the caller passes one. **This feature writes to exactly four tables — `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references` — plus `llm_training_data`. Other tables in `db/schema_sqlite.sql` (e.g. `transactions`, which does carry its own `tenant_id` column) are NOT written or read by this feature.** Tenant scoping for each write target, taken from the shipped DDL (verify with `grep -n "tenant_id" db/schema_sqlite.sql` and the `CREATE TABLE` block each hit falls in — do not assume "only one table anywhere has tenant_id"):
  - `canonical_entities` **has** a nullable `tenant_id` column. `create_canonical_entity` writes it as a column value; `update_confidence` scopes with `WHERE canonical_id = ? AND tenant_id = ?` when a tenant is passed.
  - `entity_aliases`, `entity_edges`, and `system_references` have **no `tenant_id` column** — assert this directly (`PRAGMA table_info(<table>)` on a loaded in-memory DB must not list `tenant_id`). Their `tenant_id=` parameter is a **scoping filter, never an inserted value**: it is applied by joining to the parent `canonical_entities` row, exactly as the shipped reads do — the `entity_store.py` module docstring ("alias and system_reference tables join through `canonical_id`"), `get_aliases` (joins `canonical_entities AS c` and filters `c.tenant_id = ?`), and `are_clustered` (requires **both** edge endpoints to match the tenant).
  - `llm_training_data` **has** a nullable `tenant_id` column, written as a pass-through value.
  - Therefore `add_alias`, `upsert_edge`, and `add_system_reference` must **never** put `tenant_id` in an INSERT column list — `INSERT ... tenant_id` on those three tables raises `no such column: tenant_id`. They either ignore the parameter or use it to verify the parent canonical is in tenant scope before writing.
- Migrating the SQLite graph store to Postgres.

---

## Success Criteria

Every criterion below runs with **no `DATABASE_URL` set and no Postgres installed**.

- [ ] `core/graph/resolution.py` exists and `from core.graph.resolution import resolve_match, create_new_entity, reject_match` succeeds.
- [ ] **Read-signature preservation (derived, not enumerated):** a test *derives* the baseline public surface at runtime — it reads `git show HEAD:core/graph/entity_store.py` (the commit before this feature), `ast.parse`s it, and collects every module-level `FunctionDef` whose name does not start with `_` (private helpers such as `_neighbors` are excluded by that rule, not by a list). For each derived name it asserts the name is still importable from `core.graph.entity_store` and that `str(inspect.signature(fn))` equals the baseline signature rendered from the same AST. The test hardcodes **no count and no function list**; it fails on any rename, reorder, removal, parameter-name change, or default change, and passes unchanged when an upstream feature *adds* a function. If `git show` is unavailable the test fails loudly rather than skipping.
- [ ] `.venv/bin/python -m pytest tests/test_deterministic.py tests/test_blocking.py tests/test_scoring.py tests/test_disposition.py -x --tb=short` passes unchanged (exit 0).
- [ ] After `resolve_match` on a confirmed match: exactly one new `entity_aliases` row exists for `(canonical_id, value, source)`; `entity_edges` has exactly one row for `(source_node, target_node, relationship)` with `source_category`, `target_category`, `weight`, and `approved_by` all non-NULL and equal to the values passed in.
- [ ] **Alias idempotency:** calling `resolve_match` twice with identical input yields `SELECT COUNT(*) FROM entity_aliases` unchanged after the second call, and the same `canonical_id` returned both times.
- [ ] **Edge idempotency:** the second `resolve_match` leaves `SELECT COUNT(*) FROM entity_edges` unchanged and increments that edge's `approval_count` by exactly 1.
- [ ] **Transaction atomicity:** a test injects an exception after the alias insert and before the edge insert; after the rollback, `SELECT COUNT(*)` on `entity_aliases`, `entity_edges` **and `llm_training_data`** each equals the pre-call value. Backed by a grep assertion that neither `core/matching/training_data.py` nor the new write functions in `core/graph/entity_store.py` contain `.commit(` or `.rollback(` — only `core/graph/resolution.py` may.
- [ ] After `create_new_entity`: a `canonical_entities` row exists with a generated non-empty `canonical_id`, `entity_type` and `entity_category` passing the schema CHECK constraints, and one `system_references` row per supplied source.
- [ ] **Index-rebuild visibility:** after `resolve_match` adds alias `"pacrim tech"` to `CLIENT_XXXX`, rebuilding `TokenIndex` from the same connection and querying `"pacrim tech"` returns `CLIENT_XXXX`. (Asserts the write is visible to a rebuild; incremental index mutation is out of scope.)
- [ ] **Index staleness signal:** `from core.graph.resolution import mark_indices_stale` succeeds and `mark_indices_stale()` returns `True` after a `resolve_match` or `create_new_entity` call that mutated the graph, and `False` after a `reject_match` (which performs no graph mutation) with no preceding mutation in the same test. Asserted directly in `tests/test_resolution.py`.
- [ ] `core/matching/training_data.py` exposes `TrainingPair` with exactly the fields `entity_pair`, `signal_breakdown`, `graph_evidence`, `category_pair`, `disposition`, `reasoning_trace` — asserted via `dataclasses.fields()`.
- [ ] **LLM-gated capture:** given a `Disposition` that went through Stage 5 (a real `llm_assessment` whose `call_id` has a `llm_training_data` row), `store_training_pair` inserts exactly one row whose `call_id` starts with `resolution:`, whose `category_pair` matches `^[a-z_]+:[a-z_]+$`, and whose `redacted_prompt` and `prompt_sha256` are byte-identical to the Stage 5 row's; and `sha256(redacted_prompt).hexdigest() == prompt_sha256`.
- [ ] **No LLM prompt → no row:** given a `Disposition` with `llm_assessment is None` (e.g. an `AUTO_APPROVE`), `store_training_pair` returns `None` and `SELECT COUNT(*) FROM llm_training_data` is unchanged. Same for an `llm_assessment` whose `call_id` has no Stage 5 row.
- [ ] `llm_response_json` round-trips through `json.loads` to a dict whose keys are exactly `signal_breakdown`, `graph_evidence`, `category_pair`, `disposition`, `reasoning_trace`, `entity_pair_ref` — and **does not** contain the key `entity_pair`.
- [ ] A rejected match **that went through the LLM** produces a `llm_training_data` row with `disposition == "REJECTED"` inside `llm_response_json` and a non-empty `signal_breakdown`.
- [ ] **Privacy covers both persisted columns:** for a person-entity pair, neither the stored `redacted_prompt` nor the stored `llm_response_json` contains any of the input name, raw name, email, or employee_id substrings (case-insensitive). Asserted directly against the two column values, and independently via `leak_check(value, forbidden_tokens) is None` for each, using the shipped `core/matching/redaction.py`.
- [ ] **Leak aborts the write:** a test forces a forbidden token into the payload; `store_training_pair` raises and `SELECT COUNT(*) FROM llm_training_data` is unchanged.
- [ ] `training_data.py` and `resolution.py` contain **zero** occurrences of `UPDATE llm_training_data` or `DELETE FROM llm_training_data` — grep-level assertion in the test file. Training capture is append-only in code.
- [ ] `grep -rniE "psycopg|DATABASE_URL|postgres" core/graph/resolution.py core/matching/training_data.py core/graph/entity_store.py` returns no matches; `git diff requirements.txt` is empty.
- [ ] `.venv/bin/python -m pytest tests/test_resolution.py -x --tb=short` passes (exit 0) with `DATABASE_URL` unset and no Postgres running.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` passes (exit 0) with no regression to the shipped suite.

---

## Dependencies

- [ ] **Feature 9 (threshold-llm-fallback) — SHIPPED.** Input is the `Disposition` dataclass in `core/matching/types.py`; thresholds are the `AUTO_APPROVE_THRESHOLD` / `SURFACE_THRESHOLD` constants in `core/matching/disposition.py`. There is no `core/matching/confidence.py`.
- [ ] **Feature 7 (deterministic-blocking) — SHIPPED.** Delivered `core/graph/entity_store.py`, read-only by convention — the module this feature extends. Its exact size and function list are whatever `HEAD` holds at build time. *(Corrected 2026-08-22: the prior brief called row 7 "entity store read methods"; the queue row is `deterministic-blocking`.)*
- [ ] **Feature 2 (canonical-schema) — SHIPPED.** The graph write targets are the four named tables in `db/schema_sqlite.sql`: `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references`. `db/schema_sqlite.sql` also contains tables owned by other features (`transactions`, shipped with 8b) — **feature 10 must not read or write `transactions`**, and must not assume a fixed table count. Assert with `grep -rn "transactions" core/graph/resolution.py core/matching/training_data.py` returning nothing.
- [ ] **`llm_training_data` SQLite table — SHIPPED** with feature 9 (`db/migrations/002_llm_training_data_sqlite.sql`; the writer is `_write_training_row` in `core/matching/llm_fallback.py`). The training-capture write target. Tests must load this file explicitly on top of `schema_sqlite.sql`.
- [ ] **`core/matching/redaction.py` — `[BUILT]`** (rules §10). Feature 10 uses **`leak_check` only**; the `redacted_prompt` value is the Stage 5 prompt recovered from `llm_training_data`, not a new `redact_org` / `redact_person` call.
- **No Postgres dependency.** Feature 10a is *not* a prerequisite of this feature; 10a depends on 10, not the reverse.

---

## Estimated Complexity

**Rating:** M

**Rationale:** Six write functions added to a module that is read-only by convention, one new orchestration module, one new dataclass module, and one test file — all against tables that already exist in the engine every code path already uses. The load-bearing risks are (a) not disturbing any shipped public read signature in `entity_store.py`, whichever set `HEAD` happens to hold, and (b) getting idempotency right where the schema does *not* help: `entity_aliases` and `system_references` carry UNIQUE constraints, but `entity_edges` does not, so duplicate-edge prevention is a hand-written SELECT-then-branch inside the transaction. Rated M rather than L precisely because the Postgres path, the driver pin, the migration runner, the integration tier, and the split-store transaction problem were all removed to feature 10a — this feature touches one engine and one transaction.

---

## PROJECT CONTEXT

### Pipeline position

```
Stage 4 Threshold / Stage 5 LLM Fallback  →  Disposition
Stage 6 Resolution (THIS FEATURE, SQLite):
    resolve_match      → entity_aliases + entity_edges (upsert/increment) [+ llm_training_data*]
    create_new_entity  → canonical_entities + system_references + entity_edges [+ llm_training_data*]
    reject_match       → llm_training_data* only (hard negative); no graph mutation
    * only when the Disposition carries an llm_assessment with a Stage 5
      llm_training_data row; otherwise no training row is written at all.
    → indices marked stale; caller (feature 12) rebuilds TokenIndex / NgramIndex / EmbeddingIndex
```

### Resolution Outputs (from spec Section 9, Stage 6)

Training-data capture in every row below is **conditional on the Disposition having gone through Stage 5** (see Scope). Dispositions with no LLM prompt perform their graph action and write no training row.

| Decision | Graph Action | Training Data (LLM-gated) |
|----------|-------------|---------------|
| Match confirmed | Add alias, upsert edge, mark indices stale | Positive pair: signals + disposition + entity_pair_ref |
| New entity | Create canonical node, initialize edges, mark indices stale | Negative pairs: all rejected candidates |
| Match rejected | No graph change | Hard negative: full signal breakdown + entity_pair_ref |

### Idempotency Requirements

- Same entity resolved twice → same `canonical_id`, no duplicate alias (`UNIQUE (canonical_id, value, source)`).
- Edge `approval_count` increments on re-confirmation; no second edge row (`entity_edges` has no UNIQUE constraint — enforced in code).
- Training pairs are append-only — a duplicate resolution of an LLM-gated disposition produces a *new* `llm_training_data` row (captures the temporal signal). The `call_id` hash includes the timestamp so the `call_id` PRIMARY KEY does not collide, and the `resolution:` prefix keeps Stage 6 rows distinguishable from Stage 5's uuid4-hex `call_id`s.

### Implementation Notes (constraints for the build)

1. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` import fails to collect.
2. **Tests build their schema from files, not from Python DDL.** Load `db/schema_sqlite.sql` then `db/migrations/002_llm_training_data_sqlite.sql`, exactly as the `SQLITE_SCHEMA` / `TRAINING_MIGRATION` constants in `tests/test_llm_fallback.py` do. Never `CREATE TABLE` inside a test — the schema file is the single source of truth and may gain tables from other features.
3. **Named tradeoff — training pairs land in `llm_training_data`, not `approval_decisions`.** `approval_decisions` is Postgres-only DDL that has never run; `llm_training_data` is the only real SQLite training table and its purpose (capture for V2+ fine-tuning) is the same. The column mapping in Scope is the contract. When feature 10a lands, back-filling `resolution:`-prefixed rows into `approval_decisions` is a mechanical migration — the `resolution:` `call_id` prefix exists to make those rows selectable.
4. **`tenant_id` stays nullable on every table this feature touches.** SQLite convention is nullable/NULL tenant (`canonical_entities.tenant_id TEXT`, `llm_training_data.tenant_id TEXT`, and the "Tenant scoping" paragraph of the `entity_store.py` module docstring). Do not add NOT NULL or an FK to `tenants` — that table does not exist in SQLite.
5. **Use `created_at`, never `approved_at`.** Rules §4 names `approved_at`; the shipped DDL does not have it. `INSERT ... approved_at` raises `no such column`.
6. **One connection, one transaction, per resolution.** Writers take `conn` and do not commit individually; `resolution.py` owns the commit/rollback boundary. This is why `llm_fallback._write_training_row` is off-limits — it commits internally.
7. **Cite symbols, not coordinates.** Every fact this brief asserts about the tree is stated as a symbol name, a docstring phrase, a DDL constraint, or a grep — never as `file.py:NNN`. Upstream features land between briefing and build; line numbers rot within hours. Any bare line number appearing in this document is a non-load-bearing convenience only.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — the only engine any code uses. All Stage 6 writes in this feature go here.
- `[BUILT]` `llm_training_data` — the only real SQLite training table; the write target for training capture.
- `[BUILT]` LLM redaction (`core/matching/redaction.py`) — its `leak_check` gates both persisted text columns; the prompt text itself is Stage 5's already-redacted output.
- `[PLANNED → feature 10a]` Postgres, `approval_decisions`, `audit_log`, `DATABASE_URL`, any driver. Per §0, treat as **not existing**. This feature must not reference them.
- `[PLANNED]` RLS. No `CREATE POLICY` anywhere. Among this feature's write targets, `tenant_id` is a real column on `canonical_entities` and `llm_training_data`; on `entity_aliases` / `entity_edges` / `system_references` it is a join-through-parent filter, never an inserted column (see Out of Scope). Other features' tables may carry their own `tenant_id` — irrelevant here, since this feature does not touch them.
- `[BUILT]` Training data capture from Day 1 — load-bearing for V2+ fine-tuning.

### Relevant Spec Sections

- Section 9: Stage 6 — Resolution + Graph Update
- Section 8: System Architecture (idempotency everywhere, audit trail non-negotiable — the durable audit table lands with feature 10a)
