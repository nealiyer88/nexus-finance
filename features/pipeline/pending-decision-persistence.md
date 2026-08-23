# Feature Brief: Pending Decision Persistence (Stage 4 → Approval Queue Handoff) — SQLite

**Author:** Neal Iyer
**Date:** 2026-08-22
**Status:** Draft
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 10b

---

## Problem Statement

Stage 4 produces a `Disposition` (the dataclass in `core/matching/types.py`, built by `core/matching/disposition.py`). Its own docstring says it plainly: **"In-memory only — Stage 4 does not write to SQLite."** When `action == "QUEUE_FOR_REVIEW"`, that decision has nowhere to go. Nothing in the tree writes a row saying *"a human must look at this"* — not feature 10 (`core/graph/resolution.py` writes only after a decision has already been made), not feature 9 (`llm_training_data` captures LLM prompts, not pending work), and not feature 10a (`features/infrastructure/postgres-store-bootstrap.md`, which owns `approval_decisions` — a record of **completed** human decisions, per its Postgres `disposition` CHECK vocabulary `approved`/`rejected`/`corrected`).

Feature 11 (`features/dashboard/approval-queue.md`) assumes it can call `GET /approvals/pending` and get a list back, then hand an approved item to `resolve_match()` / `reject_match()`. There is no table behind that list and no mechanism that could reconstruct the arguments those functions demand. **This feature is that missing row.** It persists a pending decision at Stage 4 exit, and makes it rehydratable into precisely the inputs the shipped Stage 6 writers require.

This feature is scoped to the **SQLite store that every runtime and test path in the repo already uses** — the only `sqlite3.connect` targets in the tree are the test in-memory connections, and rules §0 marks SQLite `[BUILT]` and Postgres `[PLANNED]`. It must be buildable, runnable, and fully verifiable on a machine with **no Postgres present** and no `DATABASE_URL` set. Feature 10a is the feature that stands Postgres up and is **not** shipped; per rules §0 it is therefore treated as not existing.

---

## Scope

### In Scope

- **Create `db/migrations/<next-unused-prefix>_pending_decisions_sqlite.sql`** — a SQLite-only migration following the shape of `db/migrations/002_llm_training_data_sqlite.sql`. The numeric prefix is **derived at build time** by listing `db/migrations/` and taking the next prefix not already in use; it is not written into this brief. `CREATE TABLE IF NOT EXISTS pending_decisions (...)` with:

  | Column | Purpose |
  |---|---|
  | `pending_id` TEXT PRIMARY KEY | `"pending:" + uuid4().hex`; the id feature 11's routes address |
  | `tenant_id` TEXT | nullable, SQLite convention (see Tenant Scoping) |
  | `decision_key` TEXT NOT NULL UNIQUE | the idempotency key (see Idempotency) |
  | `status` TEXT NOT NULL DEFAULT `'pending'` | CHECK IN (`'pending'`, `'approved'`, `'rejected'`, `'corrected'`) — the terminal vocabulary is deliberately the same one 10a's `approval_decisions.disposition` CHECK uses, so 10a can join without translating |
  | `source_entity_id` TEXT NOT NULL | `Disposition.source_entity_id` |
  | `action` TEXT NOT NULL | the `Action` value at enqueue time |
  | `top_canonical_id` TEXT | `top_match.canonical_id`, NULL when `top_match` is None |
  | `top_score` REAL | `top_match.score`, NULL when `top_match` is None — backs "sorted by confidence desc" |
  | `category_pair` TEXT NOT NULL | `f"{a}:{b}"`, same format `llm_fallback.py` and `training_data.py` use |
  | `cluster_conflict` INTEGER NOT NULL DEFAULT 0 | 0/1 |
  | `abbreviation_rescue` INTEGER NOT NULL DEFAULT 0 | 0/1 — lets feature 11 show *why* a sub-SURFACE item is queued without re-deriving bands, which is the stated purpose of that field on `Disposition` |
  | `llm_call_id` TEXT | `llm_assessment.call_id` or NULL — the join key into `llm_training_data` |
  | `entity_json` TEXT NOT NULL | serialized `NormalizedEntity` (identifying — see Privacy) |
  | `disposition_json` TEXT NOT NULL | serialized `Disposition`, candidates and all |
  | `proposal_json` TEXT NOT NULL | the pipeline-derived Stage 6 write arguments (see Rehydration) |
  | `created_at` TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP | |
  | `resolved_at` / `resolved_by` / `outcome_canonical_id` | NULL until a terminal transition |

  Plus indices on `(tenant_id, status)` and `(status, top_score)`.

  **No foreign key on `top_canonical_id`.** The graph tables cascade on `ON DELETE CASCADE`; a pending row is decision history and must not vanish because a candidate canonical was deleted. Deliberate divergence from the graph tables' FK style.

  **No Postgres counterpart file, and no edit to `db/schema.sql` or `db/schema_sqlite.sql`.** `tests/test_schema_parity.py` compares only the tables named in its own `SHARED_TABLES` literal — read that literal at build time and confirm `pending_decisions` is absent from it (it must stay absent; do not add it). A SQLite-only migration table is invisible to parity, exactly as `llm_training_data` is today. Mirroring this table into Postgres is feature 10a's business.

- **Create `core/matching/pending_store.py`** — module-level functions only, no class wrapper, `conn: sqlite3.Connection` first and `tenant_id: Optional[str] = None` last, mirroring the convention every function in `core/graph/entity_store.py` follows:
  - `PendingDecision` — frozen dataclass mirroring the table's columns, with the three JSON columns exposed as already-parsed objects.
  - `enqueue_pending(conn, disposition, entity, proposal, tenant_id=None) -> Optional[str]` — writes at most one row, returns `pending_id` or `None`.
  - `list_pending(conn, tenant_id=None, limit=..., offset=...) -> list[PendingDecision]` — `status = 'pending'` only, ordered by `top_score` descending then `pending_id` ascending for deterministic paging.
  - `get_pending(conn, pending_id, tenant_id=None) -> Optional[PendingDecision]`
  - `rehydrate(pending) -> tuple[Disposition, NormalizedEntity, dict]` — pure function, no `conn`; rebuilds the in-memory shapes from the three JSON columns.
  - `mark_decided(conn, pending_id, status, resolved_by, outcome_canonical_id=None, tenant_id=None) -> None` — the terminal transition. Never deletes.
  - Like `core/graph/entity_store.py`'s write functions and `core/matching/training_data.py`, **this module never calls `conn.commit()` or `conn.rollback()`** — the caller owns the transaction boundary, so an approval can enqueue-and-resolve atomically alongside `core/graph/resolution.py`'s writes. Grep-asserted in the test file.

- **Enqueue gate.** `enqueue_pending` writes a row **only** when `disposition.action == "QUEUE_FOR_REVIEW"`. Any other member of the `Action` `Literal` alias in `core/matching/types.py` returns `None` and writes nothing. `AUTO_APPROVE` needs no human; `NO_MATCH` has no candidate to show; `LLM_FALLBACK` is a pre-Stage-5 action that Stage 5 always converts to `QUEUE_FOR_REVIEW` before a human ever sees it (an `LLMAssessment` never auto-approves — rules §1). Derive the accepted-action set from the `Action` alias at build time rather than hardcoding a list.

- **Rehydration: the row stores a serialized snapshot, not an id-addressed reference.** Justification, in one sentence: nothing in any shipped table persists a `ScoredMatch` — `score`, `weight_profile_id`, the `SignalBreakdown` fields including its `b_boosts` entries, and `GraphEvidence` are all computed in memory by Stage 3 and written nowhere, so an id-addressed reference would have nothing to dereference and re-deriving them would require re-running Stages 2–3 against a graph that the pending decision's own eventual resolution mutates. Concretely:
  - `disposition_json` is `json.dumps(..., sort_keys=True)` over the full `Disposition` including every element of `candidates_ranked` with its complete `signal_breakdown` (nested boost entries included) and `graph_evidence`, plus `llm_assessment` when present.
  - `entity_json` is the serialized `NormalizedEntity` — `resolve_match` / `create_new_entity` / `reject_match` each take one, and its `raw_name` / `normalized_name` / `source` / `category` / `email` / `raw_record` feed both the alias write and the forbidden-token derivation in `core/matching/training_data.py`.
  - `proposal_json` carries the pipeline-derived write arguments recoverable from **neither** of the above: the edge shape (`source_node`, `target_node`, `relationship`, `source_category`, `target_category`, `weight`), `alias_confidence`, and the `system_refs` tuple for the new-entity path.
  - Tuples serialize as lists and **must** be restored as tuples; the frozen dataclasses compare by value, so a correct round-trip is testable by `==` against the original object (see Success Criteria).
  - `approved_by`, `reasoning_trace`, `conn` and `tenant_id` are supplied by the approving caller at decision time, never by the row.
  - **Signature coverage is derived, not enumerated.** A test `inspect.signature`s each of `resolve_match`, `create_new_entity`, `reject_match` as actually imported from `core.graph.resolution`, subtracts the caller-supplied names (`conn`, `disposition`, `entity`, `approved_by`, `reasoning_trace`, `tenant_id`) and every parameter that carries a default, and asserts each remaining name is a key of the rehydrated `proposal` dict. It hardcodes no parameter list and no count, so it keeps passing if a writer gains a defaulted parameter and fails loudly if a writer gains a required one.

- **Privacy — the constraint deliberately differs from training data, and this is a decision, not an omission.** `core/matching/training_data.py` never persists an entity pair: its module docstring, its `TrainingPair` docstring, and its `store_training_pair` `leak_check` gate over both persisted text columns all exist because `llm_training_data` is a durable corpus intended for V2+ fine-tuning and therefore travels beyond the moment and the tenant that produced it. **`pending_decisions` is the opposite kind of table.** Its entire product purpose (feature 11: *"We found 'MCG' in RUDDR and 'Meridian Consulting Group, LLC' in QuickBooks — are these the same client?"*, and spec §14's "Both entity names (raw, not normalized)") is to show a human the real names. A redacted pending row is a useless pending row. Therefore:
  - `entity_json` stores the raw, unredacted `NormalizedEntity` verbatim, `raw_record` included. **No `redact_org` / `redact_person` call, and no `leak_check` gate on this table.**
  - The countervailing guards are containment, not redaction:
    - `pending_decisions` is **never a training source**. `core/matching/training_data.py` must not read it, and `core/matching/pending_store.py` must not write to `llm_training_data` — both directions grep-asserted in the test file. The training path's own no-raw-pairs invariant is untouched by this feature.
    - `llm_assessment.reasoning` is stored as carried on the `Disposition`; Stage 5 already outbound-scrubs it via `leak_check`, so no new exposure is introduced by persisting it.
    - Every read is tenant-scoped (below), and the row is deleted on tenant offboarding under the same GDPR right-to-erasure carve-out `002_llm_training_data_sqlite.sql`'s header comment already states for training data.
  - Stated plainly so no later reader has to guess: **feature 10's "raw entity pairs are never stored" rule applies to the training corpus and does not apply to `pending_decisions`.**

- **Tenant scoping, matching the shipped convention exactly.** `core/graph/entity_store.py`'s module docstring is the contract: *"every read takes `tenant_id: Optional[str] = None`. When `None`, no WHERE filter is applied … When set, queries filter on `canonical_entities.tenant_id`."* `pending_decisions` differs from `entity_aliases` / `entity_edges` / `system_references` in one respect — it **has its own nullable `tenant_id` column**, like `canonical_entities` and `llm_training_data`, because a pending decision may reference no canonical at all (the create-new-entity case) and so cannot join through a parent. Consequently:
  - `tenant_id` is written as a real column value by `enqueue_pending`, nullable, never NOT NULL and never FK'd to a `tenants` table (that table does not exist in SQLite).
  - `list_pending`, `get_pending` and `mark_decided` apply `AND tenant_id = ?` when a tenant is passed and no filter when it is `None` — the same two-branch shape `lookup_alias_exact` uses.
  - When a pending row does name a `top_canonical_id`, `enqueue_pending` verifies that canonical is in tenant scope before writing, reusing `core/graph/entity_store.py`'s existing private tenant-scope assertion rather than re-implementing the check.
  - No `CREATE POLICY`, no row-level security — rules §10 marks RLS `[PLANNED]`.

- **Idempotency invariant.** `decision_key` is `sha256(f"{tenant_id}|{entity.source}|{entity.source_id}|{top_canonical_id or ''}")`, deliberately excluding scores, timestamps and the candidate list so that ordinary score drift between runs does not mint a second row. The invariant, stated once:

  > **For a given `decision_key` there is at most one row, ever.** Re-running the pipeline over the same entity with the row still `pending` refreshes `disposition_json` / `proposal_json` / `top_score` / `action` in place and returns the **existing** `pending_id`. Re-running it after the row has reached any terminal `status` writes nothing, mutates nothing, and returns `None` — a decided item never re-enters the queue.

  Enforced by the shipped-in-this-migration `UNIQUE (decision_key)` constraint plus a `SELECT`-then-branch inside `enqueue_pending`, not by a bare `INSERT OR REPLACE` (which would silently discard a terminal row's `resolved_by` and `resolved_at`).

- **Test suite `tests/test_pending_decisions.py`** — pure SQLite, no database server, no network, no `DATABASE_URL`. It builds its schema from files, never from Python DDL: `db/schema_sqlite.sql`, then `db/migrations/002_llm_training_data_sqlite.sql`, then this feature's new migration — following the `SQLITE_SCHEMA` / `TRAINING_MIGRATION` path-constant pattern already in `tests/test_llm_fallback.py` and `tests/test_resolution.py`.

### Out of Scope

- **The approval API and dashboard — feature 11.** This feature ships no `api/routers/approvals.py`, no Dash page, no HTTP route, no badge count.
- **Recording completed decisions to `approval_decisions`, and the `audit_log` table — feature 10a.** Both are Postgres-only DDL that has never run. `mark_decided` records the terminal state on the pending row itself; back-filling those rows into `approval_decisions` once 10a lands is a mechanical migration, which is why this table's `status` vocabulary was chosen to match 10a's CHECK.
- **Calling `enqueue_pending` from a pipeline orchestrator — feature 12.** No Stage 0–6 orchestrator exists (rules §12 marks `core/matching/engine.py` `[PLANNED]`). This feature delivers the function and its contract; feature 12 wires the call site.
- **Any change to `core/graph/resolution.py`, `core/graph/entity_store.py`, `core/matching/training_data.py`, `core/matching/disposition.py` or `core/matching/types.py`.** This feature is purely additive: one migration, one module, one test file. No shipped public signature moves.
- **All Postgres work.** This feature adds no entry to `requirements.txt`, imports no driver, and reads no new environment variable.
- Batch enqueue, notifications, delegation, correction workflow semantics beyond recording a `corrected` status — feature 11 and later.
- Confidence decay on pending rows — feature 13.
- A Postgres mirror of the `pending_decisions` DDL, and any edit to `tests/test_schema_parity.py`'s `SHARED_TABLES`.

---

## Success Criteria

Every criterion below runs with **no `DATABASE_URL` set and no Postgres installed**.

- [ ] The new migration file exists under `db/migrations/` with the next-unused numeric prefix (derived by listing the directory, not read from this brief), and `executescript`ing it on top of `db/schema_sqlite.sql` succeeds on a fresh in-memory connection.
- [ ] `PRAGMA table_info(pending_decisions)` on that loaded connection lists a `tenant_id` column, and `PRAGMA index_list` shows a UNIQUE index covering `decision_key`. Asserted by reading the loaded DB, never by line number.
- [ ] `from core.matching.pending_store import PendingDecision, enqueue_pending, list_pending, get_pending, rehydrate, mark_decided` succeeds.
- [ ] **Enqueue gate:** a `QUEUE_FOR_REVIEW` disposition produces one row and a non-`None` `pending_id`. For every other value in the `Action` `Literal` alias — the set derived at runtime via `typing.get_args`, not hardcoded — `enqueue_pending` returns `None` and `SELECT COUNT(*) FROM pending_decisions` is unchanged.
- [ ] **Round-trip equality:** `rehydrate(get_pending(conn, pid))` returns a `Disposition` that compares `==` to the `Disposition` originally enqueued, and a `NormalizedEntity` that compares `==` to the original — including `candidates_ranked` being a `tuple` of `ScoredMatch` (not a list of dicts), each carrying a `SignalBreakdown` whose boost entries survive, and a `GraphEvidence`. A disposition with `llm_assessment=None` and one with a populated `LLMAssessment` both round-trip.
- [ ] **Signature coverage (derived, not enumerated):** for each of `resolve_match`, `create_new_entity`, `reject_match` imported from `core.graph.resolution`, the test computes `inspect.signature(fn).parameters`, removes the caller-supplied names and every parameter with a default, and asserts the remainder is a subset of the rehydrated `proposal` dict's keys. The test hardcodes no parameter names beyond the caller-supplied set and no counts.
- [ ] **End-to-end handoff:** a test enqueues a `QUEUE_FOR_REVIEW` disposition, reads it back with `get_pending`, rehydrates, calls the real `resolve_match` with the rehydrated triple plus a human `approved_by`, and asserts the resulting `entity_aliases` and `entity_edges` state — proving the row carried enough to drive Stage 6 unaided.
- [ ] **Idempotency — live row:** calling `enqueue_pending` twice with the same entity and top candidate leaves `SELECT COUNT(*) FROM pending_decisions` unchanged after the second call, returns the same `pending_id` both times, and updates `top_score` to the second call's value.
- [ ] **Idempotency — terminal row:** after `mark_decided(..., status='approved', ...)`, a third `enqueue_pending` with the same key returns `None`, leaves the row count unchanged, and leaves `status`, `resolved_by` and `resolved_at` untouched.
- [ ] **Tenant scoping:** rows enqueued under two distinct `tenant_id` values are each returned by `list_pending` for their own tenant and never for the other; `get_pending` with the wrong tenant returns `None`; `mark_decided` with the wrong tenant mutates nothing. `list_pending(conn, tenant_id=None)` returns rows for both, matching the shipped no-filter-when-None convention.
- [ ] **Ordering:** `list_pending` returns rows sorted by `top_score` descending, ties broken by ascending `pending_id`, and honours `limit` / `offset` deterministically.
- [ ] **Transaction neutrality:** `grep -n "\.commit(\|\.rollback(" core/matching/pending_store.py` returns nothing; a test that enqueues and then rolls back on the caller's connection leaves `SELECT COUNT(*) FROM pending_decisions` at its pre-call value.
- [ ] **Privacy containment (both directions):** `grep -n "llm_training_data" core/matching/pending_store.py` returns nothing, and `grep -n "pending_decisions" core/matching/training_data.py` returns nothing. A test additionally asserts `SELECT COUNT(*) FROM llm_training_data` is unchanged across an `enqueue_pending` call.
- [ ] **Privacy stance is asserted, not assumed:** a test enqueues a person entity carrying a name and email, reads `entity_json` back, and asserts the raw name and email **are** present — locking in the deliberate divergence so a later refactor cannot quietly redact the queue into uselessness. The same test asserts `core/matching/pending_store.py` imports nothing from `core.matching.redaction`.
- [ ] `grep -rniE "psycopg|DATABASE_URL|postgres|approval_decisions|audit_log" core/matching/pending_store.py db/migrations/*pending_decisions_sqlite.sql` returns no matches; `git diff requirements.txt` is empty.
- [ ] `.venv/bin/python -m pytest tests/test_pending_decisions.py --collect-only -q` reports a **collected count greater than zero** — asserted on the reported count, not on the exit code, because an unregistered marker silently deselects everything and still exits 0.
- [ ] `.venv/bin/python -m pytest tests/test_pending_decisions.py -x --tb=short` passes.
- [ ] `.venv/bin/python -m pytest tests/test_schema_parity.py tests/test_resolution.py tests/test_llm_fallback.py tests/test_disposition.py -x --tb=short` passes unchanged.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` passes with no regression to the shipped suite, and the collected count is **greater than or equal to** the count collected on the commit before this feature (both measured at build time; no number is written into this brief).

---

## Dependencies

- [ ] **Feature 10 (resolution-graph-update) — SHIPPED.** `core/graph/resolution.py` exposes `resolve_match` / `create_new_entity` / `reject_match`; their signatures are whatever `HEAD` holds at build time and are read with `inspect.signature`, never copied. This feature must not modify that module.
- [ ] **Feature 9 (threshold-llm-fallback) — SHIPPED.** Supplies the `Disposition`, `LLMAssessment`, `Action`, `ScoredMatch`, `SignalBreakdown` and `GraphEvidence` shapes in `core/matching/types.py`, and the `llm_training_data` SQLite table (`db/migrations/002_llm_training_data_sqlite.sql`) that `llm_call_id` points into.
- [ ] **Feature 3 (normalizer) — SHIPPED.** Supplies `NormalizedEntity` in `core/ingestion/normalizer.py`, the shape serialized into `entity_json`.
- [ ] **Feature 2 (canonical-schema) — SHIPPED.** Supplies `db/schema_sqlite.sql`, which the new migration is layered on top of and which this feature does not edit.
- [ ] **Feature 7 (deterministic-blocking) — SHIPPED.** Supplies `core/graph/entity_store.py`, whose tenant-scoping convention and private tenant-scope assertion this feature reuses. No signature in that module changes.
- [ ] **Feature 10a (postgres-store-bootstrap) — queue-declared dependency, NOT a build prerequisite.** 10b sits between 10a and 11 in the queue ordering, but per rules §0 nothing in 10a exists at runtime, so nothing in this feature may reference Postgres, `approval_decisions`, `audit_log`, or a driver. If 10a lands first, this feature is unaffected; if it lands after, the `status` vocabulary chosen here is what lets 10a join. **Everything in this brief must build and pass with 10a unshipped.**
- **Downstream, not a dependency:** feature 11 consumes `list_pending` / `get_pending` / `rehydrate` / `mark_decided`; feature 12 calls `enqueue_pending` at Stage 4 exit.

---

## Estimated Complexity

**Rating:** M

**Rationale:** One migration, one module of module-level functions, one test file — all against an engine every code path already uses, with no shipped module edited. The load-bearing risks are (a) **exact** round-trip fidelity of nested frozen dataclasses through JSON, where a tuple silently restored as a list breaks `==` and, worse, would break Stage 6 downstream in a way no shallow test catches; (b) the idempotency branch, where the naive `INSERT OR REPLACE` quietly destroys terminal-state columns; and (c) staying inside the caller's transaction so an approval can enqueue-and-resolve atomically. Rated M rather than L because the round-trip contract is against shapes owned by another feature and must be derived at build time rather than transcribed.

---

## PROJECT CONTEXT

### Pipeline position

```
Stage 3 Scoring → Stage 4 Threshold → Disposition (in-memory)
    action == QUEUE_FOR_REVIEW
        → THIS FEATURE: enqueue_pending → pending_decisions row
                                          (entity_json + disposition_json + proposal_json)
    action == LLM_FALLBACK → Stage 5 → Disposition (QUEUE_FOR_REVIEW) → enqueue_pending
    action == AUTO_APPROVE / NO_MATCH → no pending row

    ... human, eventually (feature 11) ...

    get_pending → rehydrate → (Disposition, NormalizedEntity, proposal)
        → Stage 6 (feature 10): resolve_match / create_new_entity / reject_match
        → mark_decided(status, resolved_by, outcome_canonical_id)
```

### What "enough information" means

The pending row is the **only** carrier of state between Stage 4 and an approval that may happen days later, across process restarts. It must therefore survive without the pipeline: no in-memory index, no re-scoring, no assumption that the graph is unchanged. That is the whole argument for the serialized snapshot over an id-addressed reference — there is no id to address, because Stage 3's output is never persisted anywhere.

### Named tradeoff — snapshot staleness

A serialized snapshot can go stale: if the graph changes between enqueue and approval, the stored `graph_evidence` reflects the older graph. This is **accepted and deliberate**. The alternative — re-deriving signals at approval time — would show the human a different explanation than the one that queued the item, and would make the queue's contents depend on when it was opened. The `created_at` column exists so feature 11 can surface the snapshot's age, and the live-row refresh branch of the idempotency rule means a re-run of the pipeline naturally freshens any still-pending item.

### Idempotency Requirements

- One row per `decision_key`, ever. Live rows refresh in place and return the same `pending_id`; terminal rows are inert and return `None`.
- `decision_key` excludes scores, timestamps and candidate identity beyond the top candidate, so ordinary drift does not duplicate.
- `mark_decided` is a one-way transition. There is no un-decide, no delete, and no `INSERT OR REPLACE` anywhere in the module.

### Implementation Notes (constraints for the build)

1. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` import fails to collect.
2. **A criterion that only checks an exit code is not a criterion.** Assert on the reported collected count; an unregistered marker deselects everything and exits 0.
3. **Tests build their schema from files, not from Python DDL.** Load `db/schema_sqlite.sql`, then the training migration, then this feature's migration. Never `CREATE TABLE` inside a test — the schema files are the single source of truth and gain tables from other features.
4. **Derive, do not transcribe.** Stage 6 signatures come from `inspect.signature`, the `Action` values from `typing.get_args`, the migration prefix from listing the directory, the parity table set from the test's own literal. Nothing about the tree's current state is written into this brief as a number.
5. **Never commit or roll back inside `pending_store.py`.** The caller owns the boundary, exactly as `core/matching/training_data.py` and the Stage 6 write functions in `core/graph/entity_store.py` already do.
6. **Cite symbols, not coordinates.** Every fact this brief asserts about the tree is a symbol name, a docstring phrase, a DDL constraint or a grep — never `file.py:NNN`, never a count. Upstream features land between briefing and build.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` SQLite graph store — the only engine any code uses. The `pending_decisions` table lands here, via a migration, in the SQLite dialect.
- `[BUILT]` `llm_training_data` — untouched by this feature in either direction.
- `[BUILT]` LLM redaction (`core/matching/redaction.py`) — **intentionally not used here.** The approval queue's purpose is to show a human the real names; redaction's scope is the training corpus. See Privacy in Scope.
- `[PLANNED → feature 10a]` Postgres, `approval_decisions`, `audit_log`, `DATABASE_URL`, any driver. Per §0, treat as not existing. This feature must not reference them.
- `[PLANNED]` RLS. No `CREATE POLICY` anywhere. `pending_decisions` carries its own nullable `tenant_id` column, filtered in the query when a tenant is passed, matching the shipped `entity_store.py` convention.
- `[BUILT]` Human-in-the-loop — this feature is the row that makes it durable.

### Relevant Spec Sections

- Section 9: Stage 4 — Threshold / Cluster Conflict Detection (the `QUEUE_FOR_REVIEW` band this feature persists)
- Section 14: Product UI — approval queue (raw entity names are shown to the human, which is why this table is not redacted)
- Section 8: System Architecture (idempotency everywhere; audit trail — the durable audit table lands with feature 10a)
