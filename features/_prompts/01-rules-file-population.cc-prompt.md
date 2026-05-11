# CC Prompt: rules-file-population

Generated: 2026-05-09
Source brief: features/rules-file-population.md
Debate-adjusted: yes (see RUN_LOG.md for debate synthesis)

---

```
Populate .claude/rules/01-nexus-finance-v1.md with V1 architectural rules. Documentation only. Branch: feature/rules-file-population.

SITUATION:
Read features/rules-file-population.md (full brief + PROJECT CONTEXT) and current placeholder at .claude/rules/01-nexus-finance-v1.md. Project instructions in this repo carry the v3 spec; schemas must be byte-exact to that spec — do not paraphrase field names.

OVERRIDES TO BRIEF (post-debate, take precedence):
1. Line target tightened to 150-250 (was 150-300).
2. Section order is prescribed: V1 SCOPE -> CROSS-CATEGORY EXAMPLE -> CANONICAL ENTITY SCHEMA -> GRAPH EDGE SCHEMA -> CONFIDENCE THRESHOLDS -> 6-STAGE PIPELINE -> CONNECTORINTERFACE -> ENTITY TYPES -> PERSON HEURISTICS -> DATA SECURITY -> NOT-SCOPE -> OWNER MAP.
3. Cross-category example required (~15-line ASCII tree): CLIENT_0042 "Cenlar FSB" linked across QB customer ref, RUDDR project codes (CEN-GENAI-SOW3), Stripe customer_id, Salesforce account_id.
4. 6-STAGE PIPELINE section is one-line-per-stage list, no narrative: Stage 0 Normalization, Stage 1 Deterministic, Stage 2 Blocking, Stage 3 Pairwise Scoring, Stage 4 Threshold/Cluster Conflict, Stage 5 LLM Fallback, Stage 6 Resolution/Graph Update.
5. NOT-SCOPE format is 8 items, each as "- No X: <one-sentence rationale>. Re-evaluate when: <trigger>." Items: Neo4j, fastText, XGBoost, self-hosted LLM, agent orchestration framework, write-back, payroll cost rates, connectors beyond QB+RUDDR.
6. OWNER MAP section maps each future-owned content area: schemas -> core/graph/entity_store.py; pipeline -> core/matching/engine.py; connector contract -> connectors/base.py; thresholds -> core/matching/confidence.py.
7. Inline TODOs required: canonical entity schema ("Replace with core.graph.entity_store.CanonicalEntity reference once canonical-schema ships"), pipeline section ("Owner: core/matching/engine.py once normalizer ships"), plus OWNER MAP entries.
8. Entity types: Organizational = client, vendor, project, pl_unit, cost_center, contract. Person = person.

NON-GOALS:
1. No .py file modifications. Documentation only.
2. Do not modify roadmap.md, TEMPLATE.md, SHIPPED.md, DEBUG.md, RUN_LOG.md, or any feature brief.
3. Do not modify .claude/rules/00-global.md.
4. Mentions of Neo4j, fastText, XGBoost, or GraphRAG appear ONLY in the NOT-SCOPE section.
5. Do not add narrative pipeline summary — section 6 is one line per stage, hard rule.

VERIFICATION:
1. wc -l .claude/rules/01-nexus-finance-v1.md returns 150-250.
2. grep -cE "canonical_id|source_category|target_category|AUTO_APPROVE|RapidFuzz|Shadow Ledger|RUDDR" .claude/rules/01-nexus-finance-v1.md returns >=7.
3. grep -c "TODO" .claude/rules/01-nexus-finance-v1.md returns >=3.
4. awk '/NOT-SCOPE/{exit} {print}' .claude/rules/01-nexus-finance-v1.md | grep -E "Neo4j|fastText|XGBoost|GraphRAG" returns no output.
5. git diff --stat shows only .claude/rules/01-nexus-finance-v1.md modified.

EXECUTION:
ONE STEP AT A TIME. Step 1: create branch feature/rules-file-population from main and write the file. Step 2: run all 5 verifications and report each result. Wait for acknowledgment before commit/push. If any check fails, fix in place and rerun — do not proceed past a failed check.
```
