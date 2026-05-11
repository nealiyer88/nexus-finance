# Feature Brief: RUDDR Connector

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

RUDDR is the second V1 connector (category: PSA/labor). Without it, the product is a single-system reader, not a cross-category resolution engine. The cross-category thesis requires data from a structurally different system category. RUDDR encodes entity identity via project codes, client slugs, and resource IDs — none of which share schema with QB's CustomerRef/VendorRef. This is a new build with no prior art.

---

## Scope

### In Scope

- Create `connectors/ruddr.py` implementing `ConnectorInterface` with `category = "psa"`
- **Auth:** API key authentication (RUDDR uses API key, not OAuth2). Key stored encrypted per tenant
- **`read_entities()`:** Pull Client, Resource (person) records. Output as `NormalizedEntity` with `source="ruddr"`, `category="psa"`, slug and display_name preserved in `raw_record`
- **`read_transactions()`:** Pull Time Entry records with date range filtering. Include project code, hours, billing rate
- **`read_operational_records()`:** Pull Project records with budget_hours, logged_hours, status, department, billing rate
- **`export_csv_fallback()`:** Generate CSV export of entities and operational records
- **`execute_write()`:** Returns Shadow Ledger preview only
- **Entity extraction:** Parse RUDDR-specific fields — client slug, project code structure (e.g., "CEN-GENAI-SOW3"), resource display_name, email, department, billing rate
- **Project-to-client relationship:** Extract which projects belong to which clients (this is a graph edge: CLIENT → HAS_PROJECT → PROJECT)
- **Rate limiting:** Respect RUDDR API limits. Implement retry with exponential backoff
- **Multi-tenant:** All API calls scoped to tenant's RUDDR API key
- **Test suite:** `tests/test_ruddr_connector.py` with mocked responses using `tests/fixtures/ruddr_entities.json`

### Out of Scope

- QB connector — separate feature (already built)
- Webhook receiver — V1 uses polling
- Write-back to RUDDR — Shadow Ledger only
- Budget vs. actual analysis — that's AR reconciliation (feature 12)
- Resource utilization calculations — V2 with Gusto connector

---

## Success Criteria

- [ ] `connectors/ruddr.py` exists, implements `ConnectorInterface`
- [ ] `category` property returns `"psa"`
- [ ] `read_entities("client", {})` returns `List[NormalizedEntity]` with slug, display_name, tags
- [ ] `read_entities("person", {})` returns `List[NormalizedEntity]` with email, department, billing_rate
- [ ] `read_operational_records("project", {})` returns project records with code, budget_hours, logged_hours, status
- [ ] Project-to-client relationships extractable from output (project record contains client reference)
- [ ] All RUDDR fixture entities (45 records) load through connector entity mapping without errors
- [ ] `execute_write()` returns Shadow Ledger preview, never calls RUDDR write endpoints
- [ ] `export_csv_fallback()` produces valid CSV
- [ ] `python -c "from connectors.ruddr import RUDDRConnector"` exits 0
- [ ] `pytest tests/test_ruddr_connector.py` passes with mocked API responses

---

## Dependencies

- [ ] ConnectorInterface base class (feature 4)
- [ ] Normalizer shipped (feature 3)
- [ ] Canonical schema deployed (feature 2)
- [ ] `.env.example` has RUDDR_API_KEY, RUDDR_BASE_URL (DONE)

---

## Estimated Complexity

**Rating:** M

**Rationale:** Simpler auth than QB (API key vs. OAuth2), but new build with no prior art. RUDDR API documentation must be consulted. Project-to-client relationship extraction adds graph-aware logic. Test suite with mocked responses.

---

## PROJECT CONTEXT

### RUDDR API Entity Mapping

| RUDDR Entity | Maps To | Key Fields |
|-------------|---------|------------|
| Client | NormalizedEntity (type=client) | slug, display_name, tags, industry |
| Resource | NormalizedEntity (type=person) | display_name, email, department, billing_rate, role |
| Project | NormalizedRecord (type=project) | code, name, client_id, budget_hours, logged_hours, status, department |
| Time Entry | NormalizedTransaction | resource_id, project_code, hours, date, billing_rate |

### Cross-Category Signal: Project Code Structure

RUDDR project codes like `CEN-GENAI-SOW3` encode client identity in the prefix (`CEN`). This is a category-specific heuristic the matcher uses in Stage 3 — the connector must preserve the full code structure so the matcher can parse it.

### V1 Hard Constraints

- No connectors beyond QB + RUDDR
- No write-back — Shadow Ledger only
- API keys encrypted at rest with customer-specific keys
- Every database query RLS-scoped to tenant_id

### Target Files

```
connectors/
├── base.py (feature 4)
├── quickbooks.py (feature 5)
├── ruddr.py  ◄── THIS FEATURE
tests/
├── test_ruddr_connector.py  ◄── THIS FEATURE
```

### Relevant Spec Sections

- Section 11: Multi-System Connector Architecture (RUDDR entity mapping)
- Section 4: V1 Product Definition (RUDDR as cross-category V1 pair)
