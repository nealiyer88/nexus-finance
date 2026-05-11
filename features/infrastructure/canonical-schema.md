# Feature Brief: Canonical Entity Schema

**Author:** Neal Iyer
**Date:** 2026-05-09
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

`db/schema.sql` defines a generic `entities` table (`name TEXT, type TEXT, parent_id UUID, metadata JSONB`) that predates the v3 product spec. It cannot store canonical entity IDs, cross-category system references, alias tables with per-source confidence scores, graph edges with category metadata, or structured training pairs from approval decisions. The synthetic test data in `tests/fixtures/` already produces entities with `canonical_id`, `entity_category`, `system_references`, `aliases`, `match_signals`, and `confidence` — none of which the current schema can persist. Every downstream component (matcher, approval queue, entity browser, AR reconciliation) depends on this schema existing correctly.

---

## Scope

### In Scope

- **Replace** `db/schema.sql` with canonical entity schema designed for SQLite (V1 graph store) and PostgreSQL/Supabase (multi-tenant operational store)
- **SQLite graph store tables:** `canonical_entities` (entity nodes), `entity_aliases` (alias table with source, category, confidence), `entity_edges` (graph edges with source_category, target_category, weight, approval_count), `system_references` (per-connector reference records linking source system IDs to canonical IDs)
- **PostgreSQL operational tables:** `tenants`, `connectors` (keep existing), `approval_decisions` (structured training data capture: entity_pair, signal_breakdown, graph_evidence, category_pair, disposition, reasoning_trace), `audit_log` (keep existing, add category field)
- **Approval decisions table** must capture structured training pairs from Day 1 — this is the training data pipeline for V2+ model fine-tuning
- **Entity types enum:** client, vendor, project, pl_unit, cost_center, person, contract
- **Migration file:** `db/migrations/001_canonical_schema.sql`

### Out of Scope

- Neo4j migration scripts — SQLite only in V1
- Vector embedding columns — no fastText in V1
- Ingestion pipeline code — separate feature (normalizer)
- Seed data or fixture loading — test data generation already exists
- API endpoint changes — schema only

---

## Success Criteria

- [ ] `db/schema.sql` replaced with schema matching v3 spec Section 9 entity node and edge structures
- [ ] SQLite graph store schema creates without errors: `sqlite3 test.db < db/schema_sqlite.sql` exits 0
- [ ] PostgreSQL operational schema creates without errors against a local Supabase instance
- [ ] `canonical_entities` table has columns: `canonical_id TEXT PRIMARY KEY`, `canonical_name TEXT`, `entity_type TEXT CHECK(...)`, `entity_category TEXT CHECK(...)`, `confidence REAL`, `created_at`, `updated_at`
- [ ] `entity_aliases` table has columns: `alias_id`, `canonical_id` (FK), `value TEXT`, `source TEXT`, `category TEXT`, `confidence REAL`, `created_at`
- [ ] `entity_edges` table has columns: `edge_id`, `source_node TEXT` (FK), `target_node TEXT` (FK), `relationship TEXT`, `source_category TEXT`, `target_category TEXT`, `weight REAL`, `approved_by TEXT`, `approval_count INTEGER`, `last_transaction TEXT`, `created_at`
- [ ] `system_references` table has columns: `ref_id`, `canonical_id` (FK), `source TEXT`, `category TEXT`, `external_id TEXT`, `external_fields JSONB/TEXT`, `created_at`
- [ ] `approval_decisions` table has columns: `decision_id`, `tenant_id`, `entity_pair_a TEXT`, `entity_pair_b TEXT`, `signal_breakdown TEXT/JSONB`, `graph_evidence TEXT/JSONB`, `category_pair TEXT`, `disposition TEXT`, `reasoning_trace TEXT`, `decided_by TEXT`, `decided_at`
- [ ] Ground truth fixture `canonical_ground_truth.json` entities can be loaded into the schema without field mapping errors (write a verification script)
- [ ] Unique constraint on `(canonical_id, source, category)` in `system_references`
- [ ] Unique constraint on `(canonical_id, value, source)` in `entity_aliases`

---

## Dependencies

- [ ] Rules file populated (feature: rules-file-population) — CC needs V1 constraints visible during this build
- [ ] v3 product spec Section 9 (entity node structure, edge schema) — authoritative source

---

## Estimated Complexity

**Rating:** M

**Rationale:** Two schema targets (SQLite + PostgreSQL), migration file, verification script, must align with existing fixture data structure. No application code, but schema errors here cascade into every downstream feature.

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
├── db/ ....................... migrations, schema.sql, schema_sqlite.sql
└── .claude/rules/ ............ 00-global.md, 01-nexus-finance-v1.md
```

### Relevant Spec Sections

- Section 9: The Knowledge Graph — Technical Design (entity node structure, edge schema, alias structure)
- Section 17: V1 Build Scope
- Section 8: System Architecture (idempotency, audit trail, RLS principles)
- Appendix B: Key Architectural Decisions Log (SQLite edge tables before Neo4j)

### Existing Fixture Schema (must align)

The canonical ground truth JSON uses this structure — the SQL schema must accommodate it:

```json
{
  "canonical_id": "CAN-001",
  "canonical_name": "Cenlar FSB",
  "entity_type": "client",
  "entity_category": "organization",
  "pattern": "legal-suffix-strip",
  "sources": {
    "quickbooks": {"id": "QB-001", "display_name": "Cenlar, LLC."},
    "ruddr": {"id": "RUDDR-001", "slug": "cenlar-fsb", "display_name": "Cenlar FSB"}
  },
  "match_signals": ["name_fuzzy", "name_prefix_match", "suffix_strip"],
  "confidence": 0.97
}
```
