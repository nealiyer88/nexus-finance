# Feature Brief: Overview Dashboard + Entity Registry Browser

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

The matching engine resolves entities but there's no way to see the results. The Controller needs two views: (1) an overview with KPI cards showing system health (entities resolved, auto-match rate, pending approvals, cross-category coverage), and (2) an entity registry browser showing every canonical entity with its cross-category aliases and graph relationships. Without these, the product is a black box.

---

## Scope

### In Scope

- Update `dashboard/pages/overview.py`:
  - 4 KPI cards: Entities Resolved (count), Auto-Match Rate (%), Pending Approvals (count with badge), Cross-Category Coverage (% of entities resolved across 2+ categories)
  - KPIs queried from canonical entity store and approval queue
  - Refresh on page load

- Update `dashboard/pages/entity_graph.py` — Entity Registry Browser:
  - Searchable table of all canonical entities: canonical_id, canonical_name, entity_type, entity_category, confidence, alias count, connected categories
  - Expandable detail row per entity: aliases grouped by system category, system references, last transaction date
  - Cross-category relationship visualization: simple graph showing entity → project → client connections (Dash Cytoscape or Plotly network graph)
  - Filter by entity_type, entity_category, confidence range, source category
  - Search by name (fuzzy — uses RapidFuzz on canonical_name and aliases)

- **API endpoints** to support dashboard queries:
  - `GET /entities/stats` — returns KPI values for current tenant
  - `GET /entities/` — paginated entity list with filters
  - `GET /entities/{canonical_id}` — entity detail with aliases, references, edges

- **Test suite:** `tests/test_entity_api.py`
  - Assert: stats endpoint returns correct counts
  - Assert: entity list respects tenant RLS
  - Assert: entity detail includes aliases from all connected categories

### Out of Scope

- AR reconciliation view — separate feature
- Real-time WebSocket updates — V1 refreshes on page load
- Entity merge UI (manually merge two canonical entities) — V2
- Export to CSV/Excel from browser — V2

---

## Success Criteria

- [ ] `dashboard/pages/overview.py` renders 4 KPI cards with real data
- [ ] `dashboard/pages/entity_graph.py` renders searchable entity table
- [ ] Entity detail shows aliases grouped by category (e.g., "accounting: Cenlar, LLC." / "psa: cenlar-fsb")
- [ ] Cross-category coverage metric: counts entities with aliases from ≥2 different categories
- [ ] Entity search returns results using fuzzy matching on canonical_name
- [ ] All API endpoints enforce tenant RLS
- [ ] `pytest tests/test_entity_api.py` passes

---

## Dependencies

- [ ] Resolution + Graph Update (feature 10) — graph must have resolved entities to display
- [ ] Canonical schema (feature 2) — queries run against canonical_entities, entity_aliases, entity_edges
- [ ] Approval queue (feature 11) — pending approval count for KPI card

---

## Estimated Complexity

**Rating:** M

**Rationale:** Three API endpoints (straightforward), two Dash pages with DataTables and expandable rows (moderate), one graph visualization component (Dash Cytoscape or Plotly — moderate). The entity browser's fuzzy search across aliases is the trickiest UI element.

---

## PROJECT CONTEXT

### Dashboard Navigation (from spec Section 14)

```
Sidebar
├── Overview ◄── THIS FEATURE
├── Entity Graph ◄── THIS FEATURE
├── Approval Queue (feature 11)
├── Modules
│   └── AR Reconciliation (feature 15)
└── System
    ├── Connectors (feature 16)
    └── Audit Log (feature 16)
```

### KPI Definitions

| Metric | Calculation |
|--------|------------|
| Entities Resolved | COUNT(canonical_entities) |
| Auto-Match Rate | COUNT(auto_approved) / COUNT(total_resolved) over last 30 days |
| Pending Approvals | COUNT(approvals WHERE status='pending') |
| Cross-Category Coverage | COUNT(entities with aliases from ≥2 categories) / COUNT(canonical_entities) |

### Relevant Spec Sections

- Section 14: Product UI — V1 Feature Set (overview metric cards, entity detail view)
- Section 4: V1 Product Definition (configuration UI)
