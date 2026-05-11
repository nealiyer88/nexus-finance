# Feature Brief: Entity Normalizer (Pipeline Stage 0)

**Author:** Neal Iyer
**Date:** 2026-05-09
**Status:** Approved
**Complexity:** M
**FP&A Phase:** 1 (Entity Resolution)

---

## Problem Statement

The 6-stage matching pipeline cannot operate on raw connector output. QuickBooks returns `"Cenlar, LLC."` — with trailing period, legal suffix, and mixed case. RUDDR returns `"cenlar-fsb"` — a URL-safe slug with hyphens. Stage 1 (deterministic match) does exact alias lookups against the canonical registry. If the incoming string hasn't been normalized, `"Cenlar, LLC."` won't match the existing alias `"cenlar llc"` even though they're the same entity. Every subsequent stage (blocking, scoring, LLM fallback) also depends on normalized input.

The normalizer is not matching logic — it's preprocessing. But matching cannot function without it. It lives at `core/ingestion/normalizer.py`, not in the matcher directory.

---

## Scope

### In Scope

- Create `core/ingestion/normalizer.py` implementing `normalize_entity(raw: RawEntity) -> NormalizedEntity`
- **NormalizedEntity dataclass** preserving both `raw_name` (for display in approval queue and entity browser) and `normalized_name` (for matching)
- **Organizational normalization rules:**
  - Case folding: everything lowercase
  - Unicode normalization: NFD → strip diacritics → NFC (handles `"André"` → `"andre"`)
  - Punctuation stripping: remove periods, commas, parentheses, quotes (`"Apex Logistics Co."` → `"apex logistics co"`)
  - Legal suffix normalization: `LLC`, `L.L.C.`, `Inc.`, `Inc`, `Corp.`, `Ltd.`, `LLP`, `Limited Liability Company` → configurable strip or normalize to canonical form
  - Whitespace normalization: collapse multiple spaces, trim leading/trailing
  - Ampersand normalization: `&` → `and`
  - "The" prefix stripping: `"The Briarwood Group"` → `"briarwood group"`
  - Regional qualifier stripping: remove parenthesized qualifiers like `"(Northeast)"`
- **Person normalization rules:**
  - Name inversion detection and normalization: `"Chen, Michael"` → `"michael chen"`
  - Honorific/suffix stripping: `Jr.`, `Sr.`, `III`, `Dr.`, `Mr.`, `Ms.` → stripped from normalized form, preserved in raw
  - Middle initial handling: `"Sarah J. Martinez"` → `"sarah martinez"` (normalized), `"Sarah J. Martinez"` (raw)
  - Email extraction: if entity record contains email, extract and store as separate normalized field (near-deterministic cross-category join key)
- **Output:** `NormalizedEntity` with fields: `raw_name`, `normalized_name`, `entity_category` (organization|person), `source` (quickbooks|ruddr), `category` (accounting|psa), `source_id`, `email` (optional), `raw_record` (full original record for downstream stages)
- **Test suite:** `tests/test_normalizer.py` using fixture data from `tests/fixtures/` — run every QB and RUDDR entity through the normalizer and assert normalized output matches expected patterns
- All 13 org naming-entropy patterns and 8 person fragmentation patterns from fixtures must produce correct normalized output

### Out of Scope

- Matching logic (Stage 1+) — normalizer only preprocesses
- Connector code — normalizer receives already-fetched records, doesn't call APIs
- Database writes — normalizer is a pure function, no persistence
- Nickname resolution (`Robert` → `Bob`) — that's Stage 1 alias lookup, not normalization
- Abbreviation expansion (`MCG` → `Meridian Consulting Group`) — that's Stage 3 scoring, not normalization

---

## Success Criteria

- [ ] `core/ingestion/normalizer.py` exists with `normalize_entity()` function
- [ ] `NormalizedEntity` dataclass defined with `raw_name`, `normalized_name`, `entity_category`, `source`, `category`, `source_id`, `email`, `raw_record`
- [ ] All 46 QB fixture entities normalize without errors
- [ ] All 45 RUDDR fixture entities normalize without errors
- [ ] `"Cenlar, LLC."` → normalized: `"cenlar llc"` (or `"cenlar"` if legal suffix stripped)
- [ ] `"Chen, Michael"` → normalized: `"michael chen"`
- [ ] `"André Dubois"` → normalized: `"andre dubois"`
- [ ] `"The Briarwood Group, LLC"` → normalized: `"briarwood group"`
- [ ] `"Pinnacle Engineering (Northeast)"` → normalized: `"pinnacle engineering"`
- [ ] `"Beck & Howell Consulting Group"` → normalized: `"beck and howell consulting group"`
- [ ] `"Marcus Williams Jr."` → normalized: `"marcus williams"`
- [ ] `python -c "from core.ingestion.normalizer import normalize_entity"` exits 0
- [ ] `pytest tests/test_normalizer.py` passes with 91 entities processed, 0 errors

---

## Dependencies

- [ ] Rules file populated (feature: rules-file-population) — CC needs normalizer spec visible
- [ ] Canonical schema deployed (feature: canonical-schema) — NormalizedEntity fields must align with schema columns
- [ ] Synthetic fixture data exists (DONE — `tests/fixtures/qb_entities.json`, `ruddr_entities.json`)

---

## Estimated Complexity

**Rating:** M

**Rationale:** Pure Python, no external dependencies beyond standard library + unicodedata. But 21 distinct normalization rules across two entity categories, each with edge cases visible in the fixtures. Test suite must cover all 21 patterns. Complexity is in coverage, not in any single rule.

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
├── core/ingestion/
│   ├── __init__.py
│   ├── normalizer.py  ◄── THIS FEATURE
│   └── pipeline.py
├── tests/
│   ├── test_normalizer.py  ◄── THIS FEATURE
│   └── fixtures/
│       ├── qb_entities.json
│       ├── ruddr_entities.json
│       └── canonical_ground_truth.json
```

### Relevant Spec Sections

- Matcher Pipeline Architecture: Stage 0 — Normalization (pre-matching) — full rule list
- Section 9: Knowledge Graph (NormalizedEntity feeds into entity store)
- Section 17: V1 Build Scope
