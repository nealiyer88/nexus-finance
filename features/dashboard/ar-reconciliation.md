# Feature Brief: AR Reconciliation Module

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 2 (AR/AP Recon)

---

## Problem Statement

The first FP&A feature built on the canonical entity graph. For each resolved client entity, cross-reference RUDDR labor hours against QB invoiced revenue. "RUDDR shows 450 hrs × $200/hr = $90K for Cenlar FSB. QB shows $65K invoiced. $25K unbilled." This query is impossible without cross-category entity resolution — RUDDR knows "cenlar-fsb" and QB knows "Cenlar, LLC." but neither knows they're the same client. The canonical entity graph does.

---

## Scope

### In Scope

- Create `api/routers/reconciliation.py`:
  - `GET /reconciliation/ar` — AR reconciliation report for tenant, showing per-client: RUDDR labor total, QB invoiced total, variance, status (matched | unbilled | overbilled)
  - `GET /reconciliation/ar/{canonical_id}` — per-entity detail: line-item breakdown of RUDDR time entries vs QB invoices

- Update `dashboard/pages/ar_reconciliation.py`:
  - Summary table: canonical client name, RUDDR labor $, QB invoiced $, variance $, variance %, status flag
  - Color-coded status: green (matched within tolerance), amber (unbilled >$1K), red (variance >10%)
  - Expandable detail: RUDDR projects/time entries on left, QB invoices on right, connected by canonical_id
  - Amount tolerance: min(TotalAmt × 0.02, $500) — from spec thresholds

- Reconciliation logic in `core/reconciliation/ar.py`:
  - For each resolved client entity, aggregate RUDDR labor (hours × billing_rate) and QB invoiced amounts
  - Calculate variance and classify: MATCHED, UNBILLED, OVERBILLED
  - Handle partial matches: RUDDR shows 3 projects for client, QB shows invoices covering only 2

- **Test suite:** `tests/test_ar_reconciliation.py`
  - Seed graph with resolved entities from fixtures, add mock transaction data
  - Assert: variance calculated correctly for matched clients
  - Assert: unbilled labor flagged when RUDDR hours exist but no corresponding QB invoice
  - Assert: amount tolerance applied correctly

### Out of Scope

- AP reconciliation — V2 with Bill.com connector
- Cash flow forecasting — Phase 3
- Write-back of reconciliation results to QB or RUDDR — Shadow Ledger only
- Historical trend analysis — V2

---

## Success Criteria

- [ ] `core/reconciliation/ar.py` exists with reconciliation logic
- [ ] `api/routers/reconciliation.py` exists with 2 endpoints
- [ ] `dashboard/pages/ar_reconciliation.py` renders summary table with variance data
- [ ] Per-client variance = RUDDR labor total - QB invoiced total (calculated correctly)
- [ ] Amount tolerance applied: variance within min(TotalAmt × 0.02, $500) = MATCHED
- [ ] Unbilled labor flagged: RUDDR project exists, no corresponding QB invoices
- [ ] Expandable detail shows RUDDR entries and QB invoices side by side
- [ ] All endpoints enforce tenant RLS
- [ ] `pytest tests/test_ar_reconciliation.py` passes

---

## Dependencies

- [ ] Overview + Entity Browser (feature 14) — dashboard framework exists
- [ ] Both connectors shipped (5, 6) — need transaction data from both categories
- [ ] Matcher orchestrator (feature 12) — entities must be resolved before reconciliation
- [ ] Canonical schema (feature 2) — queries join across entity_aliases and transaction data

---

## Estimated Complexity

**Rating:** M

**Rationale:** Reconciliation logic is aggregation + comparison — not algorithmically complex. The cross-category join (RUDDR labor matched to QB invoices via canonical_id) is the novel part, and the canonical entity graph makes it a simple SQL join. Dashboard rendering with expandable detail rows and color-coded status is moderate Dash work.

---

## PROJECT CONTEXT

### The Cross-Category Query This Enables

```sql
SELECT 
  ce.canonical_name,
  SUM(ruddr_hours * ruddr_rate) as labor_total,
  SUM(qb_invoice_amount) as invoiced_total,
  SUM(ruddr_hours * ruddr_rate) - SUM(qb_invoice_amount) as variance
FROM canonical_entities ce
JOIN system_references sr_ruddr ON ce.canonical_id = sr_ruddr.canonical_id AND sr_ruddr.category = 'psa'
JOIN system_references sr_qb ON ce.canonical_id = sr_qb.canonical_id AND sr_qb.category = 'accounting'
-- join to transaction data via system references
GROUP BY ce.canonical_id
```

This query is impossible without the canonical entity graph. It's the product thesis in SQL.

### Relevant Spec Sections

- Section 7: FP&A Feature Roadmap — Phase 2 (AR/AP Reconciliation)
- Section 14: Product UI — AR Reconciliation description
- Section 10: Agent 4 — Reconciliation Agent (cross-category reconciliation)
