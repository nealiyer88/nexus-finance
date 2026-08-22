# Nexus Finance Rules v1

Architectural guardrails for V1 Claude Code sessions. This file is loaded every session — keep it lean.

## 0. BUILT vs PLANNED — read this before assuming anything exists

- GENERAL RULE: this file describes the TARGET architecture. Anything not marked `[BUILT]` must be treated as NOT EXISTING. A feature brief must not claim an unmarked or `[PLANNED]`/`[PARTIAL]` item as a satisfied dependency. Markers: `[BUILT]` = verified in tree, `[PARTIAL]` = partly real, `[PLANNED]` = does not exist.
- `[BUILT]` SQLite is the ONLY real engine in V1 — every runtime and test path uses `sqlite3`.
- `[PLANNED]` `db/schema.sql` and `db/migrations/001` (Postgres/Supabase) are ASPIRATIONAL: no code imports psycopg or reads them; only `tests/test_schema_parity.py` parses `schema.sql` as text.
- `[PLANNED]` Tables `approval_decisions` and `audit_log` exist in the Postgres schema ONLY — never created or queried at runtime.
- `[BUILT]` `db/schema_sqlite.sql` has exactly 4 tables: `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references`. The only other real SQLite table is `llm_training_data`.
- `[PLANNED]` There is NO auth: `supabase==2.9.0` is pinned but never imported; `api/routers/auth.py`, `api/middleware/tenant.py`, `api/middleware/audit.py` are stubs; `api/main.py` exposes only `/health`.

## 1. V1 SCOPE

- Connectors: QuickBooks Online (category: accounting) + RUDDR (category: psa). No others.
- Write path: Shadow Ledger preview only. No live write-back to source systems.
- Matching stack: RapidFuzz (token_set_ratio, partial_ratio, Jaro-Winkler) + pre-trained fastText cosine similarity (Signal Set C) + n-gram Jaccard (supplementary) + graph-corroborated adaptive scoring (Signal Set B — six enumerated signals B1–B6; B1/B2/B4/B5/B6 shipped in 8a, B3 lands with feature 8b's transactions table; total boost capped at +0.20, all boosts logged) + category-pair weight dispatch via `Dict[Tuple[str, str], WeightConfig]`.
- Blocking: TokenIndex + trigram n-gram index + pre-trained fastText ANN (Stage 2c). Pre-trained vectors only — zero corpus dependency.
- Graph store: SQLite with explicit edge tables carrying category metadata.
- LLM fallback: Claude API, redacted, Tier 3 only (<15% of entities, 0.50–0.70 confidence band; abbreviation-heuristic pairs in that band skip the LLM and route straight to human review — see §5). Never auto-approves.

## 2. CROSS-CATEGORY EXAMPLE

```
CLIENT_0042  Cenlar FSB  (entity_type=client)
├── accounting (quickbooks)
│   └── CustomerRef=123  DisplayName="Cenlar, LLC"  Class="Commercial.GenAI.Sands"
├── psa (ruddr)
│   ├── client_id="cenlar-fsb"
│   ├── project_codes=["CEN-GENAI-SOW3"]
│   └── billing_rate=200.00
├── payments (stripe)            [out-of-scope V1, illustrative only]
│   └── customer_id="cust_3kF9x"  email="billing@cenlarfsb.com"
└── crm (salesforce)             [out-of-scope V1, illustrative only]
    └── account_id="001Dn00000Xyz"  account_name="Cenlar Federal Savings Bank"

Resolution thesis: a single canonical_id binds heterogeneous source-
system identifiers across categories. Cross-category edges are the
corroborating evidence — accounting↔psa proves the V1 thesis.
```

## 3. CANONICAL ENTITY SCHEMA

```python
# [PLANNED] No CanonicalEntity class exists. core/graph/entity_store.py exists but is a read-only
# query interface and does not define it. The canonical DDL lives in db/schema_sqlite.sql:6-58;
# this literal remains the byte-exact spec shape until a CanonicalEntity type actually ships.
{
    "canonical_id": "CLIENT_0042",
    "canonical_name": "Cenlar FSB",
    "entity_type": "client",
    "created_at": "2024-01-15T00:00:00Z",
    "confidence": 0.97,
    "system_references": {
        "quickbooks": {"category": "accounting", "CustomerRef": "123", "DisplayName": "Cenlar, LLC", "Class": "Commercial.GenAI.Sands"},
        "ruddr": {"category": "psa", "client_id": "cenlar-fsb", "project_codes": ["CEN-GENAI-SOW3"], "billing_rate": 200.00},
        "stripe": {"category": "payments", "customer_id": "cust_3kF9x", "email": "billing@cenlarfsb.com"},
        "salesforce": {"category": "crm", "account_id": "001Dn00000Xyz", "account_name": "Cenlar Federal Savings Bank"}
    },
    "aliases": [
        {"value": "Cenlar, LLC", "source": "quickbooks", "category": "accounting", "confidence": 0.99},
        {"value": "Cenlar FSB", "source": "canonical", "category": "canonical", "confidence": 1.0},
        {"value": "CEN", "source": "internal", "category": "internal", "confidence": 0.95},
        {"value": "cenlar-fsb", "source": "ruddr", "category": "psa", "confidence": 0.97},
        {"value": "cust_3kF9x", "source": "stripe", "category": "payments", "confidence": 0.91}
    ]
}
```

## 4. GRAPH EDGE SCHEMA

```python
{
    "edge_id": "EDGE_0089",
    "source_node": "CLIENT_0042",
    "target_node": "PROJECT_0089",
    "relationship": "HAS_PROJECT",
    "source_category": "accounting",
    "target_category": "psa",
    "weight": 0.97,
    "approved_by": "user_id",
    "approved_at": "2024-03-15T14:22:00Z",
    "approval_count": 47,
    "last_transaction": "2026-03-01"
}
```

## 5. CONFIDENCE THRESHOLDS

- `AUTO_APPROVE = 0.90`
- `SURFACE = 0.70` (route to human review queue)
- `NO_MATCH = 0.50`
- Abbreviation rescue (8a, approved 2026-07-05): PSA↔Accounting pairs whose abbreviation heuristic fired and whose score lands in [0.50, 0.70) route to the human review queue instead of the LLM (`Disposition.abbreviation_rescue = True`).
- `AMOUNT_TOLERANCE = min(TotalAmt * 0.02, $500)`
- `CONFIDENCE_DECAY = 18 months` (cross-category edges decay faster)

## 6. 6-STAGE PIPELINE

<!-- TODO: Owner: core/matching/engine.py once normalizer ships -->

- Stage 0: Normalization
- Stage 1: Deterministic Match
- Stage 2: Blocking (token + trigram + pre-trained fastText ANN)
- Stage 3: Pairwise Scoring (string metrics + fastText cosine Signal Set C + graph corroboration Signal Set B + category-pair dispatch)
- Stage 4: Threshold / Cluster Conflict Detection
- Stage 5: LLM Fallback
- Stage 6: Resolution / Graph Update

## 7. CONNECTORINTERFACE

```python
class ConnectorInterface:
    category: str  # "accounting" | "psa" | "ap" | "payments" | "crm" | "expense"
    def authenticate(self) -> AuthToken: ...
    def read_entities(self, entity_type, filters) -> List[NormalizedEntity]: ...
    def read_transactions(self, date_range) -> List[NormalizedTransaction]: ...
    def read_operational_records(self, record_type, filters) -> List[NormalizedRecord]: ...
    def validate_write(self, proposal) -> ValidationResult: ...
    def execute_write(self, approved_proposal) -> WriteResult: ...  # V1: Shadow Ledger preview only
    def rollback_write(self, write_result) -> RollbackResult: ...
    def export_csv_fallback(self, entity_type, date_range) -> CSVExport: ...
```

## 8. ENTITY TYPES

- Organizational: `client`, `vendor`, `project`, `pl_unit`, `cost_center`, `contract`
- Person: `person`

## 9. PERSON MATCHING HEURISTICS

- Name inversion detection (`Neal Iyer` <-> `Iyer, Neal`).
- Email as near-deterministic join key.
- Employee ID as deterministic anchor when present.
- Legal name vs. preferred name handling (e.g., `Robert` vs. `Bob`).
- Initials and abbreviation expansion (`N. Iyer` <-> `Neal Iyer`).

## 10. DATA SECURITY (target posture — status marked per line)

- `[PLANNED]` OAuth tokens encrypted at rest with customer-specific keys, scoped per system category. Today: tokens sit in a plaintext in-memory dict (`connectors/quickbooks.py:73`). No crypto/KMS anywhere in the tree.
- `[PLANNED]` Every database query RLS-scoped by `tenant_id`. Today: a `tenant_id` COLUMN exists (`db/schema_sqlite.sql:8`, `db/schema.sql:38`) but zero queries filter on it; `core/` has no `tenant_id` references; `connectors/` use it only as a credential-lookup key. No `CREATE POLICY` / row-level security exists.
- `[PARTIAL]` Audit log: append-only (no UPDATE/DELETE), each row tagged by system category. Today: table DDL exists in the POSTGRES schema only (`db/schema.sql:106-117`); "append-only" is a SQL comment, not a trigger/REVOKE/RULE; the writer `api/middleware/audit.py` is a 4-line "Not yet implemented" stub.
- `[BUILT]` LLM redaction: organizational entities — strip identifiers, preserve category metadata; person entities — strip ALL identifiers including names, emails, and IDs. (`core/matching/redaction.py`, wired into the live path at `core/matching/llm_fallback.py:538`/`:552` — inbound `leak_check`, outbound scrub; covered by `tests/test_redaction.py`.)
- `[BUILT]` All credential files listed in `.gitignore` before first commit (`.gitignore:2-4`, `:39-41`).

## 11. NOT-SCOPE

- No Neo4j: SQLite + edge tables sufficient at <50K nodes V1. Re-evaluate when: >50K canonical entities per tenant OR cross-tenant graph queries become a product feature.
- No FINE-TUNED fastText: V1 uses PRE-TRAINED fastText (Common Crawl/Wikipedia) as Stage 2c blocking ANN and Stage 3 Signal Set C cosine similarity — the signal that bridges 80% (RapidFuzz alone) to the 95% auto-match gate, and is IN SCOPE. Fine-tuning fastText on resolution data is V2+. Re-evaluate fine-tuning when: corpus volume justifies (~20+ customers).
- No XGBoost: deterministic category-pair weight dispatch via `Dict[Tuple[str,str], WeightConfig]` is interpretable and tunable. Re-evaluate when: weight tuning produces conflicting signals across 3+ category pairs.
- No self-hosted LLM: Claude API redacted Tier 3 only at <15% of entities. Re-evaluate when: LLM cost >15% of COGS OR enterprise customer requires data residency.
- No agent orchestration framework: sequential Python functions are sufficient at V1 pipeline complexity. Re-evaluate when: pipeline reaches 5+ concurrent stages with cross-stage dependencies.
- No write-back: Shadow Ledger only for first 90 days per customer. Re-evaluate when: 3 customers complete 90-day shadow with <2% divergence.
- No payroll cost rates: V1 person matching uses QB employee + RUDDR resource records only. Re-evaluate when: per-person profitability becomes a top-3 customer ask.
- No connectors beyond QB + RUDDR: cross-category thesis must prove on accounting↔PSA before expansion. Re-evaluate when: 3+ paying customers with QB+RUDDR shipped.

## 12. OWNER MAP

<!-- TODO: Migrate inline schemas/pipeline/contract/thresholds out of this rules file once the owning modules below ship; this section becomes the redirect index. -->

- Schemas (canonical entity, edge) -> `[BUILT]` canonical DDL is `db/schema_sqlite.sql:6-58`. `core/graph/entity_store.py` exists (557 lines) but is a read-only query interface for Stages 1–2, NOT the schema.
- Pipeline (Stage 0–6) -> `[PLANNED]` `core/matching/engine.py` DOES NOT EXIST; no Stage 0–6 orchestrator exists. Stages are separate unconnected modules (`core/ingestion/normalizer.py` = Stage 0; `blocking.py`, `scoring.py`, `disposition.py`, `llm_fallback.py`). Stage 6 is unbuilt. Feature 12 (matcher-orchestrator) is the queued feature that will create this file.
- Connector contract -> `[BUILT]` `connectors/base.py` (`ConnectorInterface` ABC at `:171`).
- Confidence thresholds -> `[BUILT]` `core/matching/disposition.py:54-56` (`AUTO_APPROVE_THRESHOLD = 0.90`, `SURFACE_THRESHOLD = 0.70`, LLM/NO_MATCH boundary `0.50`). `core/matching/confidence.py` `[PLANNED]` — does not exist. §5's threshold VALUES remain correct; only the file pointer was wrong.

## 13. SESSION GUARDRAILS

- Pre-trained fastText is IN SCOPE (sections 1, 6, 11) for Stage 2c blocking and Stage 3 Signal Set C. Neo4j, FINE-TUNED fastText, XGBoost, and GraphRAG appear only in section 11 (NOT-SCOPE).
- Section 6 is one line per stage — no narrative pipeline summary.
- All schemas in this file are byte-exact to spec; do not paraphrase shapes or rename fields.
- V1 connector set is QB + RUDDR; do not scaffold connectors outside this set without re-opening scope.
- Shadow Ledger is the only write surface in V1 — `execute_write` returns a preview, never a live mutation.
