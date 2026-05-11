# Feature Brief: Rules File Population

**Author:** Neal Iyer
**Date:** 2026-05-09
**Status:** Approved
**Complexity:** S
**FP&A Phase:** Infrastructure

---

## Problem Statement

`.claude/rules/01-nexus-finance-v1.md` contains `<!-- paste content here -->`. Every Claude Code session loads this file automatically via `.claude/rules/` directory convention. With no content, CC has zero architectural guardrails — it doesn't know V1 scope boundaries, schema contracts, matching pipeline stages, confidence thresholds, or what NOT to build. Every CC session is flying blind, which means every CC session can silently violate V1 constraints.

---

## Scope

### In Scope

- Populate `01-nexus-finance-v1.md` with V1-scoped architectural rules extracted from the v3 product spec
- Content must include: V1 connector scope (QB + RUDDR only), canonical entity node schema, graph edge schema, confidence thresholds, 6-stage pipeline stage summary, V1 NOT-scope list (no Neo4j, no fastText, no XGBoost, no self-hosted LLM, no agent framework), data security constraints, ConnectorInterface contract, entity types enum, person-specific matching heuristics list
- File must be under 300 lines — CC loads the full file every session, so bloat degrades performance

### Out of Scope

- V2+ architecture (fastText layers, XGBoost, GraphRAG, self-hosted LLM details) — mentioned only as NOT-scope items with one-line "replaced by X in V2+" notes
- `00-global.md` — separate task, not blocking
- Any code changes — this is a documentation-only task

---

## Success Criteria

- [ ] `01-nexus-finance-v1.md` contains V1 scope, schemas, thresholds, pipeline stages, NOT-scope, security rules
- [ ] File is under 300 lines
- [ ] Running `wc -l .claude/rules/01-nexus-finance-v1.md` returns between 150 and 300
- [ ] No references to V2+ implementations except as NOT-scope items
- [ ] Canonical entity node schema matches the v3 spec Section 9 structure exactly
- [ ] Graph edge schema includes `source_category`, `target_category`, `approval_count`, `weight` fields

---

## Dependencies

- [ ] v3 product spec available (it is — `nexus_product_spec_v3.md` in project knowledge)
- [ ] No code dependencies — this is the first task in the chain

---

## Estimated Complexity

**Rating:** S

**Rationale:** Single file, content extraction from existing spec, no code changes. One CC session, under 30 minutes.

---

## PROJECT CONTEXT

### System Architecture

- **Pipeline:** 6-stage matcher: Normalization → Deterministic Match → Blocking → Pairwise Scoring → Threshold/Disposition + Cluster Conflict Detection → LLM Fallback → Resolution/Graph Update
- **V1 matching stack:** RapidFuzz (token_set_ratio, partial_ratio, Jaro-Winkler), n-gram Jaccard, graph-corroborated adaptive scoring (deterministic SQL joins against entity graph), category-pair weight dispatch via Dict[Tuple[str, str], WeightConfig]
- **Graph store:** SQLite with explicit edge tables carrying category metadata. Edges store: source_category, target_category, weight, approval_count, last_transaction, approved_by
- **LLM fallback:** Claude API, Tier 3 only (<15% of entities, confidence 0.50–0.70 zone), MANDATORY redaction (strip all identifiers, preserve category metadata), never auto-approves — always routes to human review queue
- **Training data capture:** Every resolution decision (approve, reject, correct) produces a structured training pair: (entity_pair, signal_breakdown, graph_evidence, category_pair, disposition, reasoning_trace)

### V1 Connectors

- **QuickBooks Online** (category: accounting) — Customer, Vendor, Invoice, Payment, Bill, Class, Item, Service
- **RUDDR** (category: psa) — Client, Project, Time Entry, Resource, Billing Rate, Budget

### V1 Hard Constraints

- No connectors beyond QB + RUDDR
- No Neo4j — SQLite only
- No fastText — n-gram Jaccard is the V1 bridge signal (fastText is V2+)
- No XGBoost — deterministic category-pair weight dispatch (XGBoost is V2+)
- No self-hosted LLM — Claude API only
- No agent orchestration framework — sequential Python functions
- No write-back — Shadow Ledger only
- No payroll cost rates — person entities from QB employee records + RUDDR resource records only
- Execute_write returns Shadow Ledger preview only

### Entity Types

- **Organizational:** client, vendor, project, pl_unit, cost_center, contract
- **Person:** person (name inversion detection, email as near-deterministic join key, legal vs. preferred name handling)

### Confidence Thresholds

- AUTO_APPROVE: 0.90
- SURFACE (human review): 0.70
- NO_MATCH: 0.50
- AMOUNT_TOLERANCE: min(TotalAmt * 0.02, $500)
- CONFIDENCE_DECAY: 18 months (cross-category edges decay faster)

### Data Security

- OAuth tokens encrypted at rest with customer-specific keys, per system category
- Every database query RLS-scoped to tenant_id
- Audit log: append-only, no UPDATE/DELETE, tagged by system category
- LLM calls: redacted (category metadata preserved for org entities, ALL identifiers stripped for person entities)
- Person entity PII access-controlled separately from organizational entity data
- All credential files in .gitignore before first commit

### Target File Structure

```
nexus-finance/
├── api/routers/ .............. entities, approvals, connectors, reconciliation, auth
├── core/matching/ ............ engine, fuzzy, confidence, llm_fallback
├── core/graph/ ............... entity_store, registry, decay
├── core/ingestion/ ........... pipeline, normalizer
├── connectors/ ............... base (ConnectorInterface), quickbooks, ruddr
├── dashboard/pages/ .......... overview, entity_graph, approval_queue, ar_reconciliation, connectors, audit_log
├── workers/ .................. ingestion_worker
├── db/ ....................... migrations, schema.sql
└── .claude/rules/ ............ 00-global.md, 01-nexus-finance-v1.md
```

### Relevant Spec Sections

- Section 4: V1 Product Definition
- Section 8: System Architecture (6 principles)
- Section 9: Knowledge Graph — Technical Design (entity node, edge schema)
- Section 17: V1 Build Scope (hard scope boundaries)
- Section 18: Tech Stack
- Matcher Pipeline Architecture (Stage 0–6)
