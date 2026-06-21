# Feature Brief: [FEATURE NAME]

**Author:** [name]
**Date:** [YYYY-MM-DD]
**Status:** Draft | In Review | Approved | In Progress | Shipped
**Complexity:** S | M | L
**FP&A Phase:** 1 (Entity Resolution) | 2 (AR/AP Recon) | 3 (Cash Flow) | 4 (P&L) | 5 (Anomaly) | Infrastructure

---

## Problem Statement

[What user pain does this solve? Who feels it? How often? What do they do today instead? Be specific — name the role (Controller, FP&A Analyst), the system category gap, and the manual workaround this eliminates.]

---

## Scope

### In Scope

- [Concrete deliverable 1]
- [Concrete deliverable 2]

### Out of Scope

- [Thing that seems related but is NOT part of this feature]
- [Adjacent improvement we are explicitly deferring]
- [V2+ capability this feature does NOT include]

---

## Success Criteria

[Measurable. Binary. No "improved" or "better" — state the threshold.]

- [ ] [Criterion 1: e.g., "Auto-match rate ≥95% across both connected categories for ≥3 customers over 3 consecutive cycles"]
- [ ] [Criterion 2: e.g., "Zero false auto-approvals in production over 30 days"]
- [ ] [Criterion 3: e.g., "P95 ingestion latency <5s for ≤500 entities"]

---

## Dependencies

- [ ] [Feature or infrastructure this requires — e.g., "Canonical entity schema deployed (roadmap priority 1)"]
- [ ] [External dependency — e.g., "RUDDR API access confirmed, rate limits documented"]
- [ ] [Spec decision — e.g., "Category-pair weight profiles finalized in architecture project"]

---

## Estimated Complexity

**Rating:** S / M / L

**Rationale:** [One sentence justifying the rating. Reference file count, integration surface, or schema changes.]

---

## PROJECT CONTEXT

> This block provides the architectural constraints an autonomous agent (CC session or agent debate) needs to reason about this feature correctly. It must be detailed enough that the agent can identify scope violations, dependency gaps, and contradictions without access to the full v3 spec.

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

[List the v3 spec sections an agent should read before implementing this feature. Example:]

- Section 9: The Knowledge Graph — Technical Design (entity node structure, edge schema)
- Section 17: V1 Build Scope (hard scope boundaries)
- Matcher Pipeline Architecture (Stage 0–6 descriptions, confidence thresholds)
