# Feature Brief: QuickBooks Online Connector

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** L
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

The matching pipeline has no data to match without connectors. QuickBooks Online is the first V1 connector (category: accounting). The prior single-tenant QB connector exists as prior art but needs multi-tenant refactoring: tenant-scoped OAuth token storage, RLS enforcement, category tagging on all output records, and implementation of the ConnectorInterface contract. This connector must output `NormalizedEntity` records that the normalizer can process and the matcher can consume.

---

## Scope

### In Scope

- Create `connectors/quickbooks.py` implementing `ConnectorInterface` with `category = "accounting"`
- **OAuth2 flow:** Token acquisition, refresh, encrypted storage per tenant. Redirect URI configuration via `.env`
- **`read_entities()`:** Pull Customer, Vendor records. Output as `NormalizedEntity` with `source="quickbooks"`, `category="accounting"`, raw QB fields preserved in `raw_record`
- **`read_transactions()`:** Pull Invoice, Payment, Bill records with date range filtering
- **`read_operational_records()`:** Pull Class, Item, Service hierarchies (dimensional taxonomy)
- **`export_csv_fallback()`:** Generate CSV export of entities and transactions for offline processing
- **`execute_write()`:** Returns Shadow Ledger preview only — stub that formats proposed changes without executing
- **Rate limiting:** Respect QB API rate limits (500 req/min). Implement retry with exponential backoff
- **Multi-tenant:** All API calls scoped to `realm_id` (QB company ID). Token storage keyed by `(tenant_id, provider)`
- **Entity extraction:** Parse QB dimensional fields — Class hierarchy (e.g., "Technology.Cloud.Rivera"), Customer/Vendor DisplayName, balance, email, address
- **Error handling:** Graceful degradation on API errors. Log to structured logger. Never expose raw QB error messages to end user
- **Test suite:** `tests/test_qb_connector.py` with mocked QB API responses using fixture data from `tests/fixtures/qb_entities.json`

### Out of Scope

- RUDDR connector — separate feature
- Webhook receiver for QB push notifications — V1 uses polling
- Write-back to QB — Shadow Ledger only
- QB Sandbox environment setup — use mock responses for testing
- Invoice line-item parsing — V1 reads invoice headers only
- QB Desktop (non-Online) support

---

## Success Criteria

- [ ] `connectors/quickbooks.py` exists, implements `ConnectorInterface`
- [ ] `category` property returns `"accounting"`
- [ ] `read_entities("customer", {})` returns `List[NormalizedEntity]` with correct field mapping
- [ ] `read_entities("vendor", {})` returns `List[NormalizedEntity]` with correct field mapping
- [ ] `execute_write()` returns Shadow Ledger preview, never calls QB API write endpoints
- [ ] `export_csv_fallback()` produces valid CSV with headers matching NormalizedEntity fields
- [ ] All QB fixture entities (46 records) can be loaded through the connector's entity mapping without errors
- [ ] OAuth token refresh logic handles expired tokens without crashing
- [ ] Rate limiter caps at 500 req/min with exponential backoff
- [ ] `python -c "from connectors.quickbooks import QuickBooksConnector"` exits 0
- [ ] `pytest tests/test_qb_connector.py` passes with mocked API responses

---

## Dependencies

- [ ] ConnectorInterface base class (feature 4) — must implement this interface
- [ ] Normalizer shipped (feature 3) — NormalizedEntity output must be compatible
- [ ] Canonical schema deployed (feature 2) — entity field types must align
- [ ] `.env.example` has QB_CLIENT_ID, QB_CLIENT_SECRET, QB_REDIRECT_URI, QB_REALM_ID (DONE)

---

## Estimated Complexity

**Rating:** L

**Rationale:** OAuth2 flow, multi-tenant token management, 5 QB API entity types to map, rate limiting, error handling, CSV fallback, test suite with mocked responses. Most complex single feature in V1 infrastructure.

---

## PROJECT CONTEXT

### QB API Entity Mapping

| QB Entity | Maps To | Key Fields |
|-----------|---------|------------|
| Customer | NormalizedEntity (type=client) | DisplayName, Id, Balance, PrimaryEmailAddr, BillAddr, Class |
| Vendor | NormalizedEntity (type=vendor) | DisplayName, Id, Balance, PrimaryEmailAddr, BillAddr |
| Invoice | NormalizedTransaction | TotalAmt, DueDate, CustomerRef, ClassRef, Line items |
| Payment | NormalizedTransaction | TotalAmt, PaymentDate, CustomerRef |
| Bill | NormalizedTransaction | TotalAmt, DueDate, VendorRef |
| Class | Dimensional taxonomy | Name, SubClass, FullyQualifiedName (hierarchical: "A.B.C") |
| Employee | NormalizedEntity (type=person) | DisplayName, Id, PrimaryEmailAddr, Department |

### V1 Hard Constraints

- No connectors beyond QB + RUDDR
- No write-back — Shadow Ledger only
- OAuth tokens encrypted at rest with customer-specific keys
- Every database query RLS-scoped to tenant_id
- Person entity PII access-controlled separately from organizational entity data
- All credential files in .gitignore

### Target Files

```
connectors/
├── base.py (feature 4)
├── quickbooks.py  ◄── THIS FEATURE
tests/
├── test_qb_connector.py  ◄── THIS FEATURE
```

### Relevant Spec Sections

- Section 11: Multi-System Connector Architecture (QB entity mapping, auth requirements)
- Section 4: V1 Product Definition (QB as first connector)
- Section 8: System Architecture (principle 3, principle 6: category-specific normalization)
