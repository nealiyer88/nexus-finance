# CC Prompt: normalizer (Pipeline Stage 0)

Generated: 2026-05-09
Source brief: features/normalizer.md
Debate-adjusted: yes — 9 overrides (see RUN_LOG.md)
Depends on: rules-file-population (1c9db51), canonical-schema (4b62cb2 + 7eed801)

---

```
Implement Pipeline Stage 0 entity normalizer at core/ingestion/normalizer.py with golden-file tests. Branch feature/normalizer from main.

SITUATION:
Read features/normalizer.md (full brief + PROJECT CONTEXT). Input fixtures: tests/fixtures/qb_entities.json (46 entities, display_name primary), tests/fixtures/ruddr_entities.json (45, display_name primary, slug fallback). core/ingestion/ does not yet exist. Features 1 + 2 shipped.

OVERRIDES TO BRIEF (precedence):
1. Legal suffix: STRIP, hardcoded.
2. Rule order (in normalizer.py module docstring, applied EXACTLY): NFD unicode → strip combining marks → NFC → case fold → strip "The" prefix → strip honorifics/suffixes (Jr.|Sr.|III|Mr.|Ms.|Dr.) → strip legal suffixes (LLC|L.L.C.|Inc.|Inc|Corp.|Ltd.|LLP|"Limited Liability Company") → strip parenthesized qualifiers → "&"→"and" → strip remaining punctuation → collapse whitespace → trim.
3. Person inversion: exactly-one-comma raw "Last, First" → "First Last", detected pre-comma-strip.
4. NormalizedEntity fields: raw_name, normalized_name, entity_category, source, category, source_id, email, email_is_person bool, raw_record dict (opaque passthrough — normalizer must not inspect inner fields), rules_applied List[str].
5. Email extracted for ALL records with email field; email_is_person = (entity_category=="person").
6. Empty/null name → raise NormalizationError(source_id, reason).
7. Source→category map: quickbooks→accounting, ruddr→psa.
8. Compile regexes at module load.

ANCHOR CASES (every one MUST match exactly):
"Cenlar, LLC."→"cenlar"; "Chen, Michael"→"michael chen"; "André Dubois"→"andre dubois"; "The Briarwood Group, LLC"→"briarwood group"; "Pinnacle Engineering (Northeast)"→"pinnacle engineering"; "Beck & Howell Consulting Group"→"beck and howell consulting group"; "Marcus Williams Jr."→"marcus williams"; "Sarah J. Martinez"→"sarah martinez"; "GreenField Analytics, LLC"→"greenfield analytics".

OUTPUTS (6 files):
- core/ingestion/__init__.py (empty package marker).
- core/ingestion/normalizer.py (≤300 lines): dataclass NormalizedEntity, NormalizationError, normalize_entity(raw: dict) → NormalizedEntity. Rule order in module docstring.
- tests/test_normalizer.py: pytest parametrized over 9 anchor cases AND over all 91 fixtures asserting against tests/fixtures/normalizer_expected.json.
- tests/test_normalizer_perf.py: asserts processing all 91 fixtures < 500ms.
- tests/regenerate_normalizer_expected.py: runs normalizer over fixtures, writes expected.json. Top comment: "Regeneration requires human review of diff before commit."
- tests/fixtures/normalizer_expected.json: {fixture_id: expected_normalized_name} for all 91 entities.

NON-GOALS:
1. No matching/fuzzy/scoring logic (Stage 1+).
2. No DB writes, API calls, or persistence.
3. Do not modify .claude/rules/, roadmap.md, TEMPLATE.md, features/*.md, SHIPPED.md, DEBUG.md, RUN_LOG.md, db/, or existing fixture JSONs.
4. No nickname resolution (Robert↔Bob), abbreviation expansion, or preferred-name handling — matching layer.
5. Do not import RapidFuzz, fastText, or any matching library.

VERIFICATION (run all 7, report PASS/FAIL with actuals):
1. python3 -c "from core.ingestion.normalizer import normalize_entity, NormalizedEntity, NormalizationError" exit 0.
2. pytest tests/test_normalizer.py -v passes (anchors + 91 fixtures).
3. All 9 anchor cases produce expected output (report each).
4. tests/fixtures/normalizer_expected.json exists, length == 91.
5. pytest tests/test_normalizer_perf.py -v passes.
6. wc -l core/ingestion/normalizer.py ≤ 300.
7. git diff --stat: only files under core/ingestion/ and tests/.

EXECUTION:
ONE STEP AT A TIME. Step 1: branch from main; write __init__.py + normalizer.py (rule-order docstring + 9 anchor-case inline unit tests). Step 2: write regenerate script, run it, verify all 9 anchors match; if any mismatch, fix rules and regenerate. Step 3: write test_normalizer.py and test_normalizer_perf.py. Step 4: run 7 verifications, report. STOP. Do not commit.
```
