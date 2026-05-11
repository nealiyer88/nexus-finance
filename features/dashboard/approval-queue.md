# Feature Brief: Approval Queue (API + Dashboard)

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

The matcher produces dispositions — some auto-approved, others queued for human review. Without an approval queue, the human-in-the-loop pattern doesn't exist. A Controller needs to see pending cross-category matches, understand why the system thinks they match (signal breakdown, LLM reasoning if applicable), and approve/reject/correct with one click. Every approval trains the graph and compounds institutional knowledge.

---

## Scope

### In Scope

- Create `api/routers/approvals.py` — REST endpoints:
  - `GET /approvals/pending` — list pending approvals for tenant, paginated, sorted by confidence desc
  - `GET /approvals/{id}` — single approval with full context (signal breakdown, graph evidence, LLM reasoning)
  - `POST /approvals/{id}/approve` — confirm match, triggers resolution (feature 10)
  - `POST /approvals/{id}/reject` — reject match, triggers negative training pair
  - `POST /approvals/{id}/correct` — human provides correct canonical_id, triggers resolution with correction
  - All endpoints RLS-scoped to tenant_id

- Create `dashboard/pages/approval_queue.py` — Dash page:
  - Table of pending approvals: incoming entity name, candidate entity name, source categories, confidence score, match type
  - Expandable detail row: full signal breakdown, graph evidence, LLM reasoning (if Tier 3)
  - Approve / Reject / Correct action buttons per row
  - Live badge count in sidebar navigation
  - Filter by entity_category (organization | person), confidence range, category pair

- Approval context display must show cross-category alias information: "This RUDDR entity 'cenlar-fsb' may match QB entity 'Cenlar, LLC.' — here's why we think so"

- **Test suite:** `tests/test_approvals_api.py`
  - Assert: pending approvals list returns only current tenant's items
  - Assert: approve triggers resolution and removes from pending
  - Assert: reject creates negative training pair
  - Assert: correct creates resolution with human-specified canonical_id

### Out of Scope

- Batch approve (approve all above threshold X) — V2 feature
- Email notifications for new pending approvals — uses Resend, deferred
- Mobile-optimized approval UI
- Approval delegation (assign to another user)

---

## Success Criteria

- [ ] `api/routers/approvals.py` exists with 5 endpoints
- [ ] `dashboard/pages/approval_queue.py` renders pending approvals table
- [ ] Approve action triggers `resolve_match()` from feature 10
- [ ] Reject action triggers `reject_match()` from feature 10
- [ ] Signal breakdown visible in approval detail view
- [ ] LLM reasoning displayed when present (Tier 3 entities)
- [ ] All endpoints enforce tenant RLS
- [ ] Live badge count updates on page load
- [ ] `pytest tests/test_approvals_api.py` passes

---

## Dependencies

- [ ] Resolution + Graph Update (feature 10) — approve/reject trigger resolution functions
- [ ] Disposition (feature 9) — produces QUEUE_FOR_REVIEW items that populate the queue
- [ ] Canonical schema (feature 2) — approval_decisions table

---

## Estimated Complexity

**Rating:** M

**Rationale:** API endpoints are standard CRUD. Dashboard requires Dash DataTable with expandable rows, action buttons with callbacks, and live badge. The cross-category context display is the UX-critical element — if the Controller can't understand why the system suggests a match, they can't make an informed decision.

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

### V1 Hard Constraints

- Every endpoint RLS-scoped to tenant_id
- Audit log entry for every approval decision
- Training data captured for every decision (approve, reject, correct)
- Person entity approvals: cost rate data not visible unless payroll-tier permissions

### Relevant Spec Sections

- Section 14: Product UI — V1 Feature Set (approval queue description)
- Section 4: V1 Product Definition (configuration UI, human-in-the-loop)
