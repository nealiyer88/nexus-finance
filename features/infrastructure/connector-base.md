# Feature Brief: ConnectorInterface Base Class

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** S
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

Both the QB connector and RUDDR connector depend on a shared interface contract. Without the base class, each connector invents its own method signatures, return types, and error handling. The spec defines `ConnectorInterface` with 7 methods and a `category` field. This must exist as an importable abstract base class before either connector is built.

---

## Scope

### In Scope

- Create `connectors/base.py` with `ConnectorInterface` abstract base class
- Fields: `category: str` (one of: accounting, psa, ap, payments, crm, expense, payroll)
- Abstract methods: `authenticate()`, `read_entities()`, `read_transactions()`, `read_operational_records()`, `validate_write()`, `execute_write()`, `rollback_write()`, `export_csv_fallback()`
- `execute_write()` must return Shadow Ledger preview only in V1 — include docstring enforcing this
- Data classes: `AuthToken`, `NormalizedEntity`, `NormalizedTransaction`, `NormalizedRecord`, `ValidationResult`, `WriteResult`, `RollbackResult`, `CSVExport` — defined as dataclasses/Pydantic models with fields from spec
- `NormalizedEntity` must align with the normalizer's output dataclass from feature 3
- Type hints on every method signature

### Out of Scope

- QB connector implementation — separate feature
- RUDDR connector implementation — separate feature
- OAuth flow logic — lives in auth router, not connector base
- Any API calls to external systems
- Webhook handling

---

## Success Criteria

- [ ] `connectors/base.py` exists with `ConnectorInterface` as ABC
- [ ] `category` field accepts only valid category strings
- [ ] All 7 abstract methods defined with full type hints
- [ ] `execute_write()` docstring states "V1: Shadow Ledger preview only. Never performs live mutations."
- [ ] All data classes importable: `from connectors.base import NormalizedEntity, AuthToken, WriteResult`
- [ ] `python -c "from connectors.base import ConnectorInterface"` exits 0
- [ ] `pytest tests/test_connector_base.py` passes — test verifies ABC cannot be instantiated directly

---

## Dependencies

- [ ] Canonical schema deployed (feature 2) — data classes must align with schema column types
- [ ] Normalizer shipped (feature 3) — `NormalizedEntity` fields must match normalizer output

---

## Estimated Complexity

**Rating:** S

**Rationale:** Single file, no external dependencies, no business logic. Pure interface definition with data classes. Under 200 lines.

---

## PROJECT CONTEXT

### System Architecture

- **Connector abstraction is load-bearing.** Category-specific normalization lives inside connectors, not the core engine. Every connector needs a CSV fallback.
- **Core product never calls source systems directly.** Connectors are the only boundary where external API calls happen.
- **V1: `execute_write` returns Shadow Ledger preview only.** No live mutations until post-90-day approval.

### ConnectorInterface Contract (from spec Section 11)

```python
class ConnectorInterface:
    category: str  # "accounting" | "psa" | "ap" | "payments" | "crm" | "expense"
    
    def authenticate(self) -> AuthToken: ...
    def read_entities(self, entity_type, filters) -> List[NormalizedEntity]: ...
    def read_transactions(self, date_range) -> List[NormalizedTransaction]: ...
    def read_operational_records(self, record_type, filters) -> List[NormalizedRecord]: ...
    def validate_write(self, proposal) -> ValidationResult: ...
    def execute_write(self, approved_proposal) -> WriteResult: ...  # V1: disabled
    def rollback_write(self, write_result) -> RollbackResult: ...
    def export_csv_fallback(self, entity_type, date_range) -> CSVExport: ...
```

### V1 Hard Constraints

- No connectors beyond QB + RUDDR
- No write-back — Shadow Ledger only
- No agent orchestration framework — sequential Python functions
- Every database query RLS-scoped to tenant_id
- OAuth tokens encrypted at rest with customer-specific keys, per system category

### Target Files

```
connectors/
├── __init__.py
├── base.py  ◄── THIS FEATURE
├── quickbooks.py  (future)
└── ruddr.py  (future)
```

### Relevant Spec Sections

- Section 11: Multi-System Connector Architecture (interface contract, implementation map)
- Section 8: System Architecture (principle 3: connector abstraction is load-bearing)
