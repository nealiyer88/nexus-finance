# CC Prompt: canonical-schema

Generated: 2026-05-09
Source brief: features/canonical-schema.md
Debate-adjusted: yes — 9 overrides (see RUN_LOG.md)
Depends on: rules-file-population (b6b1b59)

---

```
Replace db/schema.sql and create canonical V1 schemas, view, parity test, fixture-load test. Branch feature/canonical-schema from main.

SITUATION:
Read features/canonical-schema.md (full brief + Success Criteria column lists), tests/fixtures/canonical_ground_truth.json (44 entities), and .claude/rules/01-nexus-finance-v1.md (entity dict shape). Current db/schema.sql is 60-line generic Postgres, to be replaced. Feature 1 shipped b6b1b59.

OVERRIDES TO BRIEF (precedence):
1. canonical_entities adds match_pattern TEXT NULL and match_signals JSONB/TEXT NULL.
2. approval_decisions adds confidence_at_decision NUMERIC(3,2).
3. system_references UNIQUE: Postgres (tenant_id, source, external_id); SQLite (source, external_id). Brief's (canonical_id, source, category) is wrong (one canonical = many QB records).
4. Postgres GIN index on approval_decisions.signal_breakdown.
5. SQLite schema file present but DORMANT in V1 (no V1 ingestion targets it).
6. SQLite excludes approval_decisions and audit_log (Postgres-only).
7. audit_log adds category TEXT.
8. Fixture-load asserts entity_aliases count == 0 (populated by feature 3, not fixture).

OUTPUTS (6 files):
- db/schema.sql (replace, Postgres): tenants, connectors, canonical_entities, entity_aliases, entity_edges, system_references, approval_decisions, audit_log + GIN/btree indexes.
- db/schema_sqlite.sql (new, single-tenant): canonical_entities, entity_aliases, entity_edges, system_references.
- db/migrations/001_canonical_schema.sql (new): Postgres DDL replacing current schema.
- db/views/v_canonical_entities_with_refs.sql (new): Postgres view aggregating system_references per canonical_id as JSON keyed by source.
- tests/test_schema_parity.py (new): parses both SQL files; asserts column NAMES match for canonical_entities, entity_aliases, entity_edges, system_references.
- tests/test_fixture_loads.py (new): loads fixture into in-memory SQLite via db/schema_sqlite.sql; asserts 44 canonical rows, all source records in system_references, entity_aliases count == 0.

Enums: entity_type CHECK ('client','vendor','project','pl_unit','cost_center','contract','person'); entity_category ('organization','person'); disposition ('approved','rejected','corrected').

NON-GOALS:
1. No app code beyond the two test files. No ORM, ingestion, API, dashboard.
2. Do not modify .claude/rules/, roadmap.md, TEMPLATE.md, any features/*.md, SHIPPED.md, DEBUG.md, RUN_LOG.md.
3. Do not populate entity_aliases (feature 3 owns it).
4. Do not add Neo4j, fastText, XGBoost, embeddings, or self-hosted LLM hooks.
5. Do not auto-generate Pydantic/SQLAlchemy classes.

VERIFICATION (run all 6, report PASS/FAIL with actuals):
1. sqlite3 :memory: < db/schema_sqlite.sql — exit 0.
2. python -c "import sqlparse; sqlparse.parse(open('db/schema.sql').read())" — exit 0.
3. pytest tests/test_schema_parity.py -v — passes.
4. pytest tests/test_fixture_loads.py -v — passes.
5. wc -l db/schema.sql db/schema_sqlite.sql — each ≤ 200.
6. git diff --stat — only db/ and tests/ files.

EXECUTION:
ONE STEP AT A TIME. Step 1: branch from main; pip install sqlparse if missing; write all 6 files. Step 2: run all 6 verifications and report. STOP. Do not commit.
```
