# Feature Brief: Approval Queue (API + Dashboard)

**Author:** Neal Iyer
**Date:** 2026-05-10 (rewritten 2026-08-22 against 10a / 10b)
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)
**Feature #:** 11

---

## Problem Statement

The matcher produces dispositions — some auto-approved, others queued for human review. Without an approval queue, the human-in-the-loop pattern doesn't exist. A Controller needs to see pending cross-category matches, understand why the system thinks they match (signal breakdown, LLM reasoning if applicable), and approve/reject/correct with one click. Every approval trains the graph and compounds institutional knowledge.

Until feature 10b, this feature was unbuildable: Stage 4's `Disposition` is an in-memory dataclass that nothing persisted, so `GET /approvals/pending` had no source of truth and the Stage 6 writers in `core/graph/resolution.py` had nothing to be reconstructed from. **Feature 10b (`features/pipeline/pending-decision-persistence.md`) closes that hole.** It ships the `pending_decisions` table in the live SQLite store, `core/matching/pending_store.py`, and — critically — a serialized snapshot (`entity_json` / `disposition_json` / `proposal_json`) that rehydrates into exactly the arguments the shipped Stage 6 writers demand. This feature is the human surface over that store, and it consumes 10b's contract **as 10b defines it**. It invents no table, no column, and no parallel schema.

One thing this feature must NOT assume: **Postgres does not exist at runtime.** `approval_decisions` and `audit_log` are declared in the Postgres schema only and are never created — per `.claude/rules/01-nexus-finance-v1.md` §0 they are `[PLANNED]` and must be treated as not existing. Feature 10a (`features/infrastructure/postgres-store-bootstrap.md`) owns standing that path up and is not shipped. Everything here builds, runs, and is verified with **no `DATABASE_URL` set and no Postgres installed**, against SQLite alone.

---

## Scope

### In Scope

- **Fill in `api/routers/approvals.py`** — the file exists today as a docstring-only "Not yet implemented" stub. This is a fill-in, not a create. It declares an `APIRouter` and exposes:
  - `GET /approvals/pending` — list pending items for the resolved tenant, paginated, sorted by confidence descending. Backed by `pending_store.list_pending`, whose ordering contract (`top_score` descending, ties broken by ascending `pending_id`, honouring `limit` / `offset`) this endpoint inherits rather than re-implements. No secondary sort of its own.
  - `GET /approvals/{pending_id}` — one item with full context: signal breakdown, graph evidence, LLM reasoning when present, plus `created_at` so the UI can surface snapshot age (10b's named staleness tradeoff). Backed by `pending_store.get_pending`. Returns `404` when the id is absent **or** out of tenant scope — the two cases are indistinguishable to the caller by design.
  - `POST /approvals/{pending_id}/approve` — see the rehydration contract below. Dispatches to `resolve_match`, then `mark_decided(status='approved')`.
  - `POST /approvals/{pending_id}/reject` — dispatches to `reject_match`, then `mark_decided(status='rejected')`.
  - `POST /approvals/{pending_id}/correct` — the human supplies a canonical id. Dispatches to `resolve_match` with that id, then `mark_decided(status='corrected')`.
  - The terminal status strings are the ones 10b's `status` `CHECK` constraint actually permits — read them out of 10b's shipped migration at build time; do not transcribe a vocabulary out of this brief.
  - Every route is idempotent against a decided row: because `mark_decided` is a one-way transition and 10b's rows are never deleted, a second POST against an already-terminal item returns `409` and performs no write.

- **Register the router in `api/main.py`.** Verified at authoring time: `api/main.py` contains no `include_router` call and no `api/routers/*` module is reachable, so without this the endpoints do not exist at any URL. This feature adds the registration for **its own** router and nothing else — it installs no middleware and touches no other router's wiring. (Feature 16 registers the connectors router separately; both edits are additive `include_router` lines and do not conflict.)

- **The rehydration contract — this is the load-bearing part of the feature.** An approval id is not enough to drive Stage 6; the shipped writers take a live `Disposition`, a live `NormalizedEntity`, and a set of write arguments that exist nowhere else. Each write path therefore:
  1. `get_pending(conn, pending_id, tenant_id)` — tenant-scoped, `404` on miss.
  2. `rehydrate(pending)` → `(Disposition, NormalizedEntity, proposal)` — 10b's pure function. This feature does not parse the JSON columns itself and does not reconstruct dataclasses of its own.
  3. Dispatch to the Stage 6 writer, passing the rehydrated triple plus the caller-supplied values that never live on the row: the connection, `approved_by` (the resolved actor), `reasoning_trace`, and `tenant_id`.
  4. `mark_decided(...)` with the terminal status, the actor, and the resulting canonical id.

  **Argument coverage is derived at build time, never transcribed.** The build reads `inspect.signature` of each writer as actually imported from `core.graph.resolution`, subtracts the caller-supplied names and every parameter carrying a default, and sources the remainder from the `proposal` dict. No parameter list, and no count of parameters, appears in this brief or in any code or test derived from it. This mirrors 10b's own signature-coverage criterion; if a writer gains a required parameter, both features fail loudly rather than silently mis-calling.

- **Per-path specifics that are not symmetric and must not be treated as such:**
  - **Reject.** `reject_match` raises `ValueError` when the rejected canonical id is absent from `disposition.candidates_ranked`. The route therefore validates membership against the **rehydrated** `candidates_ranked` first and returns `400` on a miss; a `ValueError` must never escape as a `500`. The default rejected id is the row's top candidate.
  - **Correct.** The human's canonical id replaces the top candidate throughout the write arguments, not just in the `canonical_id` slot: any `proposal` value that names the row's `top_canonical_id` is substituted with the human's id, located by comparing against `top_canonical_id` rather than by assuming which positional field holds it. The human's id is **not** required to appear in `candidates_ranked` — `resolve_match` does not validate membership, and a correction to an entity the matcher never surfaced is the whole point of the path.
  - **Approve.** Straight dispatch with the row's own top candidate; no substitution.
  - `create_new_entity` is **not** wired to a route in this feature. The `/correct` path resolves to an *existing* canonical id. A "none of these — create a new entity" flow is a separate correction mode and is out of scope.

- **Transaction ordering, stated because it is not what a reader would assume.** The Stage 6 writers in `core/graph/resolution.py` own their own transaction boundary — each one commits on success and rolls back on failure. `core/matching/pending_store.py` deliberately does neither. Consequently the writer's commit lands **before** `mark_decided`, and the two are not one transaction. The route therefore: dispatches the writer first, then issues `mark_decided` and commits it. The failure window is a resolved graph with a still-`pending` row — visible, and safe to re-drive because the Stage 6 writers are idempotent (a repeat inserts no duplicate alias and increments the existing edge's approval count instead of inserting a second edge). The inverse ordering — marking decided first — would produce a decided row with no graph mutation, which is silent and unrecoverable, and is forbidden.

- **Tenant scoping — the shipped convention, not RLS.** No row-level security exists anywhere in this project, no `CREATE POLICY` is written by this feature, and `api/middleware/tenant.py` is a docstring-only stub owned by feature 16. The word RLS does not appear in this feature. What ships instead:
  - The router resolves a tenant per request from the `X-Nexus-Tenant` header, else the `NEXUS_TENANT_ID` environment variable — the same precedence feature 16 will later formalize in middleware, minus 16's constant fallback (that constant is defined by 10a, which does not exist).
  - **A request with no resolvable tenant is rejected** with `400` and performs no read and no write. There is no default-tenant fallback in this feature.
  - Every call into `pending_store` and into `core/graph/resolution.py` passes that resolved tenant explicitly. `pending_store` and `core/graph/entity_store.py` both apply no filter when `tenant_id` is `None` (the documented single-tenant SQLite default), so passing `None` would silently return unscoped rows. This feature never passes `None`.
  - Stated plainly, as feature 16 states it: this is **data shaping, not isolation**. The header is unauthenticated and any caller may assert any tenant. No code, test, comment, or criterion in this feature may describe the result as tenant isolation, RLS, multi-tenancy, or access control.

- **Fill in `dashboard/pages/approval_queue.py`** — the file exists today as a `dash.register_page` call plus an empty `html.Div` and a TODO. **Preserve its currently registered path and name exactly as the file declares them today**; feature 16's shell asserts that every page module registers at the path its own source declares and that pre-existing modules keep the path they had. Changing either breaks a downstream criterion this feature does not own. The body gains:
  - A `dash_table.DataTable` of pending items: incoming entity name, candidate entity name, source categories, confidence score, match type.
  - An expandable detail view: full signal breakdown, graph evidence, LLM reasoning when the item carries a Stage 5 assessment.
  - Approve / Reject / Correct actions per row.
  - Filters on `entity_category` (`organization` | `person`), confidence range, and category pair. Note the distinct fields on `NormalizedEntity`: `entity_category` is the person/organization axis this filter uses; `category` is the `accounting` | `psa` axis the "source categories" column displays. Do not conflate them.
  - A pure `apply_filters(rows, **filters) -> list[dict]` helper that performs the filtering, exercised directly on fixture rows — the same structure contract feature 16 uses for its pages, so filtering is testable without a browser or a running app.
  - A **pending-count helper** exposed at module level, returning the count for a resolved tenant. The page renders it as an in-page badge itself.

- **Forward dependency on the Dash application shell (feature 16), described by contract rather than assumed.** There is **no Dash application object anywhere in the tree** — `dashboard/` contains only `pages/`, and every page module registers against an application that does not exist. Feature 16 creates `dashboard/app.py` (`use_pages=True`) and builds its sidebar by iterating `dash.page_registry`, explicitly excluding callbacks and shared state from the shell. This feature therefore satisfies a contract rather than building a shell:
  - The page continues to call `dash.register_page` with the path and name its source already declares, so the shell picks it up and links to it automatically with no edit to the shell.
  - The badge count is exposed as the module-level helper above, so a shell that later wants a live sidebar badge has a function to call.
  - **A live badge in sidebar navigation is not built by this feature and is not a criterion of it.** It requires the shell plus a callback the shell deliberately does not host. Every render criterion below is asserted against the page module directly, not against a running application.

- **Approval context display must show cross-category alias information:** "This RUDDR entity 'cenlar-fsb' may match QB entity 'Cenlar, LLC.' — here's why we think so." The raw, un-normalized names are available because 10b stores the `NormalizedEntity` unredacted, deliberately — see 10b's Privacy section. This feature must not redact, hash, or normalize away the names it exists to display.

- **Test suite `tests/test_approvals_api.py`** — pure SQLite, no database server, no network, no `DATABASE_URL`, no browser. It builds its schema from files (`db/schema_sqlite.sql`, then the training migration, then 10b's pending-decisions migration located by glob, not by number), following the path-constant pattern the shipped test modules already use. Never `CREATE TABLE` inside a test.

### Out of Scope

- **The `pending_decisions` table, `core/matching/pending_store.py`, and the Stage 4 enqueue call — feature 10b.** This feature reads and transitions rows; it ships no DDL, no migration, and no `enqueue_pending` call site. Any change to 10b's schema or module signatures belongs to 10b.
- **A persisted audit log — feature 10a.** `audit_log` is Postgres-schema-only, has never been executed, and its writer `api/middleware/audit.py` is a stub. This feature writes no audit table and creates none. What it *does* record is 10b's own resolved-by / resolved-at columns on the pending row (see V1 Hard Constraints).
- **Recording completed decisions into `approval_decisions` — feature 10a.** 10b chose its terminal `status` vocabulary to match 10a's `CHECK` set precisely so that back-filling is mechanical once 10a lands. This feature does not anticipate it.
- **The Dash application shell, sidebar, and any live sidebar badge — feature 16.**
- **Tenant middleware — feature 16.** This feature resolves a tenant inside its own router. It installs no middleware and defines no `DEFAULT_TENANT_ID`.
- **Auth, roles, permissions, and any per-person visibility rule.** No auth exists anywhere in the tree; `api/routers/auth.py` is a stub and rules §0 marks auth `[PLANNED]`. Real login arrives with feature 17.
- **A create-new-entity correction mode.** `/correct` resolves to an existing canonical id only.
- Batch approve (approve all above threshold X) — V2.
- Email notifications for new pending items — uses Resend, deferred.
- Mobile-optimized approval UI.
- Approval delegation (assign to another user).

---

## Success Criteria

Every criterion below runs with **no `DATABASE_URL` set, no Postgres installed, and no browser**.

- [ ] `api/routers/approvals.py` declares an `APIRouter` and exposes the routes named in Scope; the route set is asserted by iterating the router's own registered routes, not by counting lines.
- [ ] **The router is reachable:** after importing `api.main`, the set of `(method, path)` pairs contributed by the approvals router is a subset of the paths registered on `api.main.app` — derived from the router object and from `app.routes` at test time, with no path list written into the test beyond the ones this feature owns.
- [ ] `GET /approvals/pending` returns only rows for the resolved tenant: rows seeded under two distinct tenants are each returned for their own tenant and never for the other, and the response order matches `pending_store.list_pending`'s documented ordering for the same inputs.
- [ ] **No unscoped call is possible:** a test asserts that every call this router makes into `core.matching.pending_store` and `core.graph.resolution` receives a non-`None` tenant argument (assert on the recorded call arguments, e.g. via a spy, not by grep alone).
- [ ] **A request with no resolvable tenant is rejected:** with no `X-Nexus-Tenant` header and `NEXUS_TENANT_ID` unset, every route on the approvals router returns `400`, and the pending-row count is unchanged across the attempt.
- [ ] `grep -rniE "\bRLS\b|CREATE POLICY|row.level security" api/routers/approvals.py dashboard/pages/approval_queue.py tests/test_approvals_api.py` returns no matches.
- [ ] **Approve drives Stage 6 for real, from the row alone:** a test seeds a `QUEUE_FOR_REVIEW` row via 10b's `enqueue_pending`, POSTs to `/approve`, and asserts the resulting `entity_aliases` and `entity_edges` state — proving the route reconstructed the writer's arguments from the snapshot with no pipeline in memory. The row's `status` is then terminal and its `resolved_by` / `resolved_at` are populated.
- [ ] **Argument coverage is derived, not enumerated:** for each Stage 6 writer this router dispatches to, the test computes `inspect.signature(fn).parameters`, removes the caller-supplied names and every parameter with a default, and asserts each remaining name is sourced from the rehydrated `proposal`. The test hardcodes no parameter names beyond the caller-supplied set and no counts.
- [ ] **Reject with a candidate the disposition does not contain returns `400`,** not `500`: the `ValueError` `reject_match` raises for an unknown canonical id never escapes the handler, and no row transitions.
- [ ] **Reject writes a negative training pair when — and only when — the item carries a Stage 5 call id.** Two asserted cases, both required: (a) an item whose rehydrated disposition carries an `LLMAssessment` with a `call_id` backed by a matching `llm_training_data` row increases the `llm_training_data` row count by one, with the rejected disposition value; (b) an item with no Stage 5 call id produces **zero** additional training rows — `SELECT COUNT(*) FROM llm_training_data` is unchanged across the call — and the route still returns success and still transitions the row to rejected. Case (b) is the common one: the queue's main population is the band above the LLM band, which never invokes Stage 5, plus abbreviation-rescue items that route around it by design. The documented no-write behavior is asserted explicitly so a later reader cannot mistake it for a bug.
- [ ] **Correct resolves to the human-supplied canonical id throughout:** a test corrects to an id that is *not* the row's top candidate and asserts the written alias and edge name the human's id — including every `proposal`-derived field that previously named `top_canonical_id` — and that the row transitions to the corrected status with that id recorded as its outcome.
- [ ] **A second POST against a terminal row returns `409` and writes nothing:** row count, `status`, `resolved_by` and `resolved_at` are all unchanged from the values captured after the first POST (compare before/after; never against a literal).
- [ ] **Ordering under mark-then-write failure:** a test forces the Stage 6 writer to raise and asserts the row is still `pending` (the writer rolled itself back and `mark_decided` was never reached), and a test that forces `mark_decided` to fail after a successful write asserts the graph mutation stands and the row is still `pending` — the documented, re-drivable window, not a silent loss.
- [ ] `dashboard/pages/approval_queue.py` imports without a Dash application present, exposes `layout`, and its `dash.register_page` call declares the same path and name as the version in `git show HEAD:dashboard/pages/approval_queue.py` — asserted by comparing the two sources, so the shell contract cannot drift.
- [ ] The page's table exposes the columns named in Scope, and each filter named in Scope is asserted independently by calling `apply_filters` directly on fixture rows.
- [ ] Signal breakdown and graph evidence appear in the detail view for a fixture item; LLM reasoning appears for an item carrying an `LLMAssessment` and is absent (not empty-stringed, not `"None"`) for one that carries none.
- [ ] The raw, un-normalized names of both sides are present in the rendered detail output for a fixture item — locking in 10b's deliberate no-redaction stance for this table, so a later refactor cannot redact the queue into uselessness.
- [ ] The module-level pending-count helper returns a count matching `SELECT COUNT(*)` over 10b's pending rows for the same tenant, and is scoped: a row under another tenant does not change it.
- [ ] `grep -rniE "psycopg|DATABASE_URL|postgres|approval_decisions|audit_log" api/routers/approvals.py dashboard/pages/approval_queue.py tests/test_approvals_api.py` returns no matches; `git diff requirements.txt` is empty.
- [ ] `.venv/bin/python -m pytest tests/test_approvals_api.py --collect-only -q` reports a **collected count greater than zero** — asserted on the reported count, not on the exit code, because an unregistered marker silently deselects everything and still exits 0.
- [ ] `.venv/bin/python -m pytest tests/test_approvals_api.py -x --tb=short` passes.
- [ ] `.venv/bin/python -m pytest tests/ -x --tb=short` passes with no regression to the shipped suite, and the collected count is **greater than or equal to** the count collected on the commit before this feature (both measured at build time; no number is written into this brief).

---

## Dependencies

- [ ] **Feature 10b (pending-decision-persistence) — HARD PREREQUISITE.** Supplies the `pending_decisions` table in the live SQLite store and `core/matching/pending_store.py` (`PendingDecision`, `list_pending`, `get_pending`, `rehydrate`, `mark_decided`). Without it this feature has no source of truth and no way to reconstruct Stage 6 arguments. **This feature consumes 10b's contract as 10b defines it and modifies nothing in it** — no new column, no parallel table, no second serialization format. 10b's function signatures and its `status` vocabulary are read at build time from the shipped module and migration, never transcribed from either brief.
- [ ] **Feature 10 (resolution-graph-update) — SHIPPED.** Supplies `resolve_match` / `reject_match` in `core/graph/resolution.py`, their transaction ownership, and the LLM-gated training-pair behavior this feature asserts in both directions. This feature does not modify that module.
- [ ] **Feature 9 (threshold-llm-fallback) — SHIPPED.** Supplies `Disposition`, `ScoredMatch`, `SignalBreakdown`, `GraphEvidence`, `LLMAssessment` and the `QUEUE_FOR_REVIEW` band, plus the `llm_training_data` table the negative-pair assertions read.
- [ ] **Feature 3 (normalizer) — SHIPPED.** Supplies `NormalizedEntity`, including the `raw_name` / `source` / `category` / `entity_category` fields the queue displays and filters on.
- [ ] **Feature 10a (postgres-store-bootstrap) — NOT a build prerequisite and must not be treated as one.** It owns Postgres, `approval_decisions`, `audit_log`, tenant provisioning, and `pytest.ini`. Per rules §0 none of it exists at runtime. Nothing in this feature may reference a driver, a DSN, or either Postgres-only table. **Everything here must build and pass with 10a unshipped.**
- [ ] **Feature 16 (connectors-audit-infra) — FORWARD dependency, consumed by contract, not depended on.** It creates `dashboard/app.py` and the sidebar, and it owns `api/middleware/tenant.py`. This feature satisfies 16's page contract (register at the path this module's own source declares) and ships a badge-count helper for a future shell, but builds neither shell nor middleware and asserts nothing against a running application. 16 lands after this feature and must not be blocked by it.

---

## Estimated Complexity

**Rating:** L (raised from M)

**Rationale:** The original M rating called this "standard CRUD." It is not. The load-bearing work is **rehydration**: reconstructing, from a serialized snapshot owned by another feature, the exact argument set each shipped Stage 6 writer demands — derived at build time via `inspect.signature`, because transcribing it is precisely how this brief was wrong before. Around that sit asymmetries that each have their own failure mode: reject validates candidate membership and raises where approve does not; correct must substitute the human's canonical id through every proposal field that named the old one, not just the obvious slot; and the writers own their own commit, so `mark_decided` cannot be atomic with the graph mutation and the ordering had to be chosen for a recoverable failure window rather than for symmetry. Add forward dependencies satisfied by contract rather than by code (16's page-registration and badge seams), a tenant convention that must reject rather than default because the constant it would default to belongs to an unshipped feature, and a negative-training-pair behavior that is correct precisely when it writes nothing. The dashboard work itself — DataTable, expandable rows, a pure filter helper — is the least risky part of the feature.

---

## PROJECT CONTEXT

### Approval Queue UX (from spec Section 14)

The key UX moment: "We found 'MCG' in RUDDR and 'Meridian Consulting Group, LLC' in QuickBooks — are these the same client?" Show:
- Both entity names (raw, not normalized)
- Source system and category for each
- Confidence score with signal breakdown
- Graph evidence (shared person entities, shared transactions)
- LLM reasoning if Tier 3
- One-click approve/reject/correct

This is exactly why 10b stores the entity unredacted. A redacted pending row is a useless pending row.

### Pipeline position

```
Stage 4 → Disposition (QUEUE_FOR_REVIEW)
    → feature 10b: enqueue_pending → pending_decisions row
                                     (entity_json + disposition_json + proposal_json)

    ... human, eventually — THIS FEATURE ...

    list_pending / get_pending  → queue table + detail view
    rehydrate → (Disposition, NormalizedEntity, proposal)
        → Stage 6 (feature 10): resolve_match / reject_match   [commits itself]
        → mark_decided(status, resolved_by, outcome_canonical_id)  [separate commit]
```

### Implementation Notes (constraints for the build)

1. **Every test command is `.venv/bin/python -m pytest ...`.** Bare `pytest` is not on PATH, and `.venv/bin/pytest` does not put the repo root on `sys.path`, so every `from core...` import fails to collect.
2. **A criterion that only checks an exit code is not a criterion.** Assert on the reported collected count.
3. **Both target files already exist.** `api/routers/approvals.py` is a docstring-only stub; `dashboard/pages/approval_queue.py` is a `register_page` call plus an empty `Div` and a TODO. Fill them in; preserve the page's registered path and name.
4. **Derive, do not transcribe.** Stage 6 argument sets come from `inspect.signature`; 10b's status vocabulary from its shipped `CHECK` constraint; 10b's migration from a glob, not a number; the page's registered path from its own source at `HEAD`. Nothing about the tree's current state is written into this brief as a number.
5. **Cite symbols, not coordinates.** Every fact this brief asserts about the tree is a symbol name, a docstring phrase, a DDL constraint, or a grep — never a file-and-line coordinate, never a count. Upstream features land between briefing and build.
6. **The Stage 6 writers commit; `pending_store` does not.** Write first, mark second. Do not "fix" the asymmetry by making `pending_store` commit — that is 10b's deliberate contract and changing it is out of this feature's ownership.
7. **Reject's silence is the specified behavior, not a bug.** Most queue items have no Stage 5 call id and their rejection writes no training pair. Assert it; do not work around it by writing an ungated pair, which would be a behavior change to shipped feature 10.

### V1 Hard Constraints (status-marked per `.claude/rules/01-nexus-finance-v1.md` §0)

- `[BUILT]` **SQLite is the only engine.** The queue reads and transitions 10b's `pending_decisions` rows there. No Postgres, no `DATABASE_URL`, no driver.
- `[BUILT]` **Every query carries an explicit tenant scope, and a request with no resolvable tenant is rejected.** This replaces the former "every endpoint RLS-scoped to tenant_id" constraint, which was unverifiable: no policies exist and the tenant middleware is a stub. This is data shaping, not isolation — see Scope.
- `[BUILT via 10b]` **Who resolved an item and when is recorded** on the pending row itself, via `mark_decided`'s resolved-by / resolved-at / outcome columns. This replaces the former "audit log entry for every approval decision" constraint. A **persisted, append-only audit log is out of scope and owned by feature 10a** — `audit_log` is Postgres-schema-only and its writer is a stub.
- `[BUILT, conditional]` **Training data is captured for a decision only when the item carries a Stage 5 call id backed by a matching `llm_training_data` row.** The former constraint claimed capture for *every* decision; that is false and cannot be made true here — the training-pair writer self-gates and returns without writing otherwise, and the queue's main population never invoked Stage 5. Both branches are asserted.
- `[BUILT]` **Human-in-the-loop** — this feature is the surface that makes it usable.
- `[PLANNED → feature 16]` The Dash application shell, sidebar navigation, live sidebar badge, and tenant middleware. Satisfied by contract here; built there.
- `[PLANNED → feature 17]` Auth. There are no users, roles, or permissions in the tree, so no visibility rule can be enforced or checked. (The former person-level cost-rate permissions constraint has been **deleted**, not deferred: rules §11 puts payroll cost rates out of V1 scope entirely, so there is no data for such a rule to gate.)

### Relevant Spec Sections

- Section 14: Product UI — V1 Feature Set (approval queue description)
- Section 9: Stage 4 / Stage 6 — the band this queue drains and the writers it dispatches to
- Section 4: V1 Product Definition (configuration UI, human-in-the-loop)
