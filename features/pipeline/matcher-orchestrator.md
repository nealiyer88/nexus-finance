# Feature Brief: Matcher Orchestrator (End-to-End Pipeline)

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

Stages 0–6 exist as individual modules but nothing wires them together. The matcher needs an orchestrator that takes raw connector output and runs the complete pipeline: normalize → deterministic match → blocking → scoring → threshold/LLM fallback → resolution. Without this, each stage must be called manually in sequence. The orchestrator is the `match()` function from spec Section 9 — the single entry point for entity resolution.

---

## Scope

### In Scope

- Create `core/matching/engine.py` implementing the `match()` function:
  ```python
  def match(incoming: NormalizedEntity, ctx: MatchContext) -> MatchResult
  ```

- **`MatchContext` — the run context replacing the brief's original `EntityRegistry`.** No `EntityRegistry` type exists anywhere in the tree and no shipped stage takes one. Every shipped stage takes an open `sqlite3.Connection` plus, for blocking, caller-built index objects. Define `MatchContext` in `core/matching/types.py` alongside the other shared shapes, carrying exactly the union of what the shipped stage signatures demand — derive the field list by reading those signatures at build time, do not copy it from prose. At minimum it carries the open connection, the optional `tenant_id` threaded through every stage, the token index, the n-gram index, the optional embedding index, and the optional LLM client injected into Stage 5. It is a mutable dataclass, not frozen: the index fields are replaced in place by the rebuild policy below.
  - `embedding_index` is `None` when the fastText model file is absent; `EmbeddingIndex.build` yields an empty index in that case and the Stage 2c caller contract is to pass `None`. The orchestrator must tolerate both.

- **`MatchResult` — define it in `core/matching/types.py`, next to `Disposition`.** No such type exists today. It carries ONLY fields that have a shipped producer:
  - `source_entity_id` — from the incoming `NormalizedEntity`
  - `canonical_id: Optional[str]` — the deterministic hit, the resolved canonical, or the id returned by the new-entity writer
  - `confidence: float` — the deterministic match confidence or the top scored match's score
  - `match_type` — a `Literal` over the paths the orchestrator can actually take (deterministic / scored / llm / new / none). The original brief's `human` value has no producer here: the orchestrator never has a human in the loop, and the human-review queue is a separate feature.
  - `action` — the `Disposition.action` value that decided the outcome
  - `signal_breakdown: Optional[SignalBreakdown]` — from `ScoredMatch.signal_breakdown`; `None` on the deterministic and empty-candidate paths, which never reach scoring
  - `disposition: Optional[Disposition]` — the Stage 4/5 result when one was produced
  - `reasoning_trace: str` — see V1 Hard Constraints
  - **`audit_entry` is dropped.** It had no producer, no table, and no writer anywhere in the tree.

- Calls stages sequentially: Stage 1 → if unresolved → Stage 2 → Stage 3 → Stage 4 → if LLM_FALLBACK → Stage 5 → Stage 6.

- **Index rebuild policy (DECIDED — a stated requirement, not a builder choice).** Stage 6 exposes a module-level staleness dirty bit and there is no incremental index update path; the caller must rebuild `TokenIndex` / `NgramIndex` / `EmbeddingIndex` from the store. **The orchestrator checks the staleness bit before Stage 2 for every incoming entity, and when it is set, rebuilds all three indices from the connection and resets the flag.** Rationale: deferring the rebuild to end-of-batch makes every entity written earlier in the same run invisible to blocking, so a run's results would depend on connector ordering and the cross-connector pairs this product exists to find would never be scored; and the indices module documents a sub-millisecond rebuild at V1 scale, so the per-entity check is affordable. Because the bit is only set by writers that actually mutated the graph, the rebuild is skipped for entities that produced no write.

- **Auto-approval identity (DECIDED).** The Stage 6 writers require an `approved_by` argument and auto-approvals have no human. Use a single fixed system-actor identity, defined as an `UPPER_CASE` module constant in `core/matching/engine.py` (per CLAUDE.md naming) and imported by any other caller — never an inline string literal, so it is greppable and stable across features. The builder does not invent a value: the constant's name and value are fixed here as `AUTO_APPROVAL_ACTOR = "system:matcher-orchestrator"`.

- **Stage 6 dispatch.** There is no `resolve(disposition)` function. Stage 6 ships three decision-specific writers — one for a confirmed match, one for a new canonical entity, one for a rejection — each demanding materially more than a `Disposition` carries (approver identity, alias confidence, edge endpoints, relationship, category metadata, weight, canonical name, entity type, system references). The requirement is: **dispatch to the Stage 6 writer appropriate to the disposition's action, supplying the arguments that writer's signature requires at build time.** Read the signatures; do not assume the argument list from this brief.
  - Note the two distinct category axes the writers straddle: `NormalizedEntity.entity_category` is the organization/person axis that Stage 1 and Stage 5 branch on, while `NormalizedEntity.category` is the accounting/psa source axis that Stage 3's weight dispatch uses. The new-entity writer's `entity_category` argument feeds the column that downstream person-detection reads. Confirm which axis each argument wants against the shipped readers before wiring.
  - Similarly, connector `read_entities` takes a source-specific entity type while the canonical id prefix is minted from the canonical entity type. Establish the mapping explicitly in `pipeline.py`.

- **Stage 5 failure handling.** Stage 5 raises on an unconfigured client and raises when its per-run call budget is exhausted. The orchestrator must catch both, record the entity as queued for review with no assessment, and continue the batch — a batch run must never abort because the LLM path was unavailable or exhausted.

- Create `core/ingestion/pipeline.py` implementing batch processing:
  - `run_ingestion(connector: ConnectorInterface, tenant_id: str)` → pulls entities from the connector for each supported entity type, normalizes, matches each against the graph via `match()`
  - Handles both org and person entity types
  - Produces a summary whose buckets are **mutually exclusive and exhaustive** over the entities ingested, keyed off the final `MatchResult`, and which also reports the ingested total. Every entity lands in exactly one bucket.
  - Resets the Stage 5 call budget at the start of each run
  - Sequential processing in V1 (no parallel/async)

- **Training-pair persistence: IN SCOPE only as a pass-through, with no new code.** The Stage 6 writers already call the training-pair store on every path, and that store self-gates — it writes nothing unless the disposition carries a Stage 5 `call_id` with a matching Stage 5 row. The orchestrator's entire obligation is therefore to pass its reasoning trace into the Stage 6 writer and let the existing call happen; it must neither suppress the call nor add a second persistence path. Any change to the training-pair writer itself is out of scope (that module shipped with the resolution feature).

- **Test suite:** `tests/test_engine.py`, `tests/test_pipeline.py`
  - End-to-end test: load every fixture entity from both shipped connectors into an empty graph via the pipeline. Derive the expected total by counting what the connectors return at test time — do not hard-code it.
  - **Stage 5 must be exercised with an injected fake `LLMClient` and a reset call budget.** No test may construct the default client and **no test makes a live API call** — the fake is passed on every Stage 5 invocation, and the test asserts against the fake's own recorded call count. The per-run budget is reset at the start of each run so it never bounds the batch.
  - Reset the Stage 6 index-staleness flag between runs so run two starts from a known state.
  - Simulate approvals, re-run the pipeline, and assert the graph learned — expressed as a proportion derived at test time (see Success Criteria), not a written-in number.

### Out of Scope

- Celery/Redis async queue — V1 runs synchronously
- Webhook-triggered ingestion — V1 uses manual/scheduled trigger
- Multi-connector orchestration (run QB + RUDDR in sequence) — that's the ingestion worker (feature 14)
- Dashboard integration — separate features
- **A persisted audit table or audit writer.** There is no audit table in the live SQLite store and no runtime audit writer anywhere; the audit DDL exists in the Postgres schema only and its middleware is a stub. Feature 10a (`postgres-store-bootstrap`) owns standing that path up, and it is BLOCKED. Feature 12 must not create one.
- Human review queue mechanics — a separate feature owns the queue surface.

---

## Success Criteria

- [ ] `core/matching/engine.py` exists with a `match()` function and the `AUTO_APPROVAL_ACTOR` constant
- [ ] `core/ingestion/pipeline.py` exists with `run_ingestion()`
- [ ] `MatchContext` and `MatchResult` are defined in `core/matching/types.py` and `MatchResult` has no `audit_entry` field
- [ ] End-to-end test: every fixture entity returned by both shipped connectors is processed through the full pipeline without errors, where the expected total is obtained by counting the connectors' output at test time
- [ ] **Bucket accounting invariant:** the run summary's buckets are pairwise disjoint, every processed entity appears in exactly one, and the bucket counts sum to the ingested total. No per-bucket count is written into the test.
- [ ] **First-run invariant, NOT a per-bucket number.** The original zero-auto-approvals-on-an-empty-graph criterion is false by construction and has been removed. Verified against the shipped code and fixtures: Stage 1's exact-alias lookup queries SQLite directly and does not consult the in-memory blocking indices, and it treats a canonical name as a seed alias at full confidence. The fixture sets from the shipped connectors contain exact normalized-name collisions. So once the first connector's entities are written, later entities with a colliding normalized name match deterministically above the auto-approve threshold on the *same* run. Assert only the invariant: every entity is accounted for, and no bucket count is asserted against a literal.
- [ ] **Second-run criterion as a derived proportion:** capture the first run's summary, simulate approvals, re-run, and assert the auto-approved share of the second run is strictly greater than the first run's — comparing the two runs' own numbers. No target number is written into the test.
- [ ] Match type distribution tracked across the paths the orchestrator can emit
- [ ] Pipeline summary reports entity counts per bucket plus the ingested total
- [ ] The repo's documented test invocation, scoped to the new test modules, passes — and the assertion is a **collected count greater than zero** parsed from the collection output, not a bare exit code. A deselected-everything run exits 0, which is exactly the silent pass this criterion exists to catch. See CLAUDE.md for the invocation form; bare `pytest` is not on PATH and the direct `.venv/bin/pytest` form fails collection.
- [ ] Index rebuild policy is implemented as stated: the staleness bit is checked per entity before blocking, and a set bit triggers a full three-index rebuild and a flag reset
- [ ] No test reads an API key or constructs the default LLM client

---

## Dependencies

- [ ] All pipeline stages shipped: deterministic+blocking (7), scoring (8), the fastText signal retrofit (8a), the transactions table and amount signal (8b), threshold+LLM (9), resolution (10) — matching FEATURE_QUEUE.md's row for this feature
- [ ] Both connectors shipped (5, 6) — pipeline needs real connector output
- [ ] Normalizer (3) — first step in pipeline

---

## Estimated Complexity

**Rating:** M

**Rationale:** Orchestration logic is straightforward — sequential function calls. Complexity is in the end-to-end test: seeding an empty graph, running the full pipeline, simulating approvals, re-running, and verifying the graph learned from approvals. This is the integration test for the entire matching engine.

---

## PROJECT CONTEXT

### Pipeline Flow (from spec Section 9, corrected to the shipped APIs)

```python
def match(incoming: NormalizedEntity, ctx: MatchContext) -> MatchResult:
    hit = deterministic_match(incoming, ctx.conn, ctx.tenant_id)   # Stage 1
    if hit: return MatchResult(...)  # deterministic path; still writes via Stage 6

    rebuild_indices_if_stale(ctx)                                  # see rebuild policy
    candidates = generate_candidates(incoming, ...)                # Stage 2
    if not candidates.candidates: return MatchResult(...)          # new-entity path

    scored = score_candidate_set(incoming, candidates, ...)        # Stage 3
    disposition = apply_thresholds(incoming.source_id, scored, ...)  # Stage 4

    if disposition.action == "LLM_FALLBACK":
        disposition = llm_assess(disposition, incoming, ctx.conn,   # Stage 5
                                 ctx.tenant_id, client=ctx.llm_client)
        # Stage 5 returns a NEW Disposition already set to QUEUE_FOR_REVIEW.

    # Stage 6: dispatch to the writer appropriate to disposition.action,
    # supplying that writer's required arguments, with AUTO_APPROVAL_ACTOR
    # as the approver on the auto path and the reasoning trace threaded in.
    return MatchResult(...)
```

Argument lists above are elided deliberately. Read the shipped signatures.

### V1 Hard Constraints

- Sequential processing — no async/parallel
- No agent orchestration framework
- **Every resolution carries an in-memory reasoning trace, passed to the Stage 6 writer via the `reasoning_trace` parameter those writers already expose.** The original "every resolution logged to audit trail" constraint is unbuildable in this feature: the live store has no audit table and the tree has no runtime audit writer. A persisted audit trail is explicitly out of scope here and is owned by feature 10a.

### Relevant Spec Sections

- Section 9: Complete Pipeline — `matcher.py` code block
- Section 13: Workflow — Customer Data Flow
