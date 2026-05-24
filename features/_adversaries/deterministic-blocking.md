# Hardened Design — Deterministic Match + Blocking (Stages 1–2)

**Source brief:** `features/pipeline/deterministic-blocking.md`
**Reconciled:** 2026-05-23
**Status:** Ready for prompt-gen

---

## Disagreements & decisions

### D1. Number of indices: three (token + Metaphone + trigram) vs one (token only)

- **Design**: keep all three. Trigrams catch typos; Metaphone catches "Catherine"↔"Kathryn", "Robert"↔"Bob".
- **Skeptic**: keep all three but exposes silent failures (per-token vs whole-string Metaphone, IBM trigram collapse).
- **Engineer**: drop Metaphone and Ngram; ship `TokenIndex` only; add others when measured recall drops <0.95.

**Decision: Engineer wins on Metaphone. Design wins on Ngram. Final = TokenIndex + NgramIndex (no Metaphone).**

Why Metaphone loses: `requirements.txt` has no phonetic library; `jellyfish`/`metaphone`/`phonetics` are not installed and not transitively available. Adding a C-extension dep for 44 fixture canonicals is unjustified. Stage 0 already case-folds and strips diacritics, removing the largest phonetic-variance source. The "Bob ↔ Robert" case is a nickname-table problem, not a phonetic one — Metaphone would not solve it either (`KP` ≠ `RPRT`).

Why Ngram (trigram) survives the cut: zero dependencies (vanilla Python list comprehension), and the fixture explicitly has cases that token-only blocking misses ("Apex Logistics Co." ↔ "Apex Logistics" — token-set works; "TechVentures Inc" ↔ "TechVentures" — token-set works; but the broader V1 thesis needs typo recall and trigram is the cheapest signal). Cost: a `Dict[str, Set[str]]` indexed by 3-char windows.

**Adopted refinement (Design):** wrap every indexed/queried string with `^`/`$` sentinels before trigram extraction, so `"^ibm$"` → 3 trigrams `["^ib", "ibm", "bm$"]` instead of 1. Fixes the short-name collapse the skeptic raised.

### D2. "Indices must be updatable without full rebuild" (brief Success Criterion #10)

- **Design**: silent.
- **Skeptic**: cut as scope creep; consistency-with-DB is Stage 6's job.
- **Engineer**: cut for V1 perf reasons (rebuild is microseconds at 44 entities).

**Decision: cut.** Indices are built once per pipeline invocation from current `canonical_entities` + `entity_aliases`. No `update()` method. Stage 6 (separate feature) owns the write path.

### D3. `entity_store` as class with methods vs module of plain functions

- **Design**: type as `Protocol` (forward-compatible with Stage 6 writable subtype).
- **Engineer**: module of plain functions taking `conn: sqlite3.Connection` first arg.

**Decision: Engineer wins.** Module of plain functions. `core/graph/entity_store.py` exposes top-level functions `lookup_alias_exact(conn, normalized_name, tenant_id=None)`, etc. Easier to test, matches existing pattern in `tests/test_fixture_loads.py`. Stage 6 will add write functions to the same module.

### D4. Tenant scoping on entity_store methods

- **Skeptic**: NON-NEGOTIABLE per rules section 10 ("Every database query RLS-scoped by tenant_id"). Cross-tenant alias bleed is P0.
- **Engineer**: phantom risk at V1 (single-tenant SQLite, nullable column).

**Decision: Skeptic wins on API shape, Engineer wins on default.** Every entity_store read function takes `tenant_id: Optional[str] = None`. When `None`, no filter (V1 single-tenant default — matches `test_fixture_loads.py` which inserts with `tenant_id=NULL`). When set, adds `WHERE tenant_id = ?`. One-line guard; future-proofs the API without forcing fixture rework.

### D5. `lookup_alias` / `lookup_email` return type — scalar vs list

- **Design**: silent (brief says `canonical_id or None`).
- **Skeptic**: MUST return `List[canonical_id]`. Scalar = silent collision bug. Stage 1 declines on multi-hit.

**Decision: Skeptic wins.** Returns `List[str]`. Stage 1 emits a `DeterministicMatch` only when the list has exactly one entry. Multi-hit → fall through to Stage 2.

### D6. Confidence emission on Stage 1a (exact alias) — hardcode 0.95 vs MIN(alias_conf, ceiling)

- **Design**: silent.
- **Skeptic**: hardcoding 0.95 inflates Stage 4 dispositions above the 0.90 auto-approve threshold for aliases stored at 0.91.

**Decision: Skeptic wins.**
- Stage 1a (alias_exact): `confidence = min(alias_row.confidence, 0.99)`. (0.99 ceiling because alias_exact is not stronger than canonical-itself which is 1.0.)
- Stage 1b (email, person entities only): `confidence = 0.99` only if `lookup_email` returns exactly one canonical_id AND target canonical's `entity_category == "person"`.
- Stage 1b (employee_id, person entities only): `confidence = 1.0` only if `lookup_employee_id` returns exactly one canonical_id.

### D7. Stage 1c — canonical_id echo from prior sync

- **Design**: silent.
- **Skeptic**: requires verification against `system_references (source, external_id)` — otherwise forgery vector.
- **Engineer**: `NormalizedEntity` has no canonical_id field; the echo path has no source to read from at V1.

**Decision: cut Stage 1c entirely.** `NormalizedEntity` (in `core/ingestion/normalizer.py`) carries no `canonical_id`. No V1 connector emits one. This sub-stage is speculative scope. Drop. Revisit when a connector produces canonical_id echo. The brief's wording "verify and resolve" is honored by deletion until the producer exists.

### D8. Stage 2d intra-system filter — by `category` or by `(source, source_id)`?

- **Design**: filter by `(source, source_id)` membership in candidate's `system_references`. A canonical with BOTH QB and RUDDR refs must survive when a QB record queries.
- **Engineer**: simple `category != source_category` filter.

**Decision: Design wins.** The cross-category V1 thesis depends on canonicals that span QB+RUDDR being reachable from either side. Filter: a candidate is excluded iff its `system_references` already contains a row with `(source=query.source, external_id=query.source_id)`. (That candidate is the query's own deterministic resolution and belongs to Stage 1, not Stage 2.)

### D9. Candidate cap of 50 — ranking by signals-hit (Design), IDF weighting (Skeptic), or hard truncate with warn (Engineer)?

- **Design**: keep 50, rank by signals-hit, deterministic tiebreak by `canonical_id ASC`.
- **Skeptic**: signals-hit without IDF lets common-token candidates crowd out the rare-token true match.
- **Engineer**: at 44 fixture entities, no cap will ever fire; hard truncate with `logger.warning` if it does.

**Decision: Engineer wins for V1.** Hard cap at 50 with a `logger.warning` if exceeded. No ranking, no IDF. Defer until the cap actually fires at scale. Skeptic's IDF concern is real but premature — there is no observed instance of the cap firing on the V1 fixture, and adding IDF without a tuning corpus is over-engineering.

### D10. Brief says "91 entities"; fixture has 44

- **Engineer**: factual correction — `canonical_ground_truth.json` is 44 canonicals.

**Decision: Engineer wins.** Update the integration test to assert "44 canonicals; ~80 source records resolved end-to-end." The brief's "91" is wrong.

### D11. Person inversion at Stage 1 (brief PROJECT CONTEXT line 129)

- **Skeptic**: cut — normalizer already handles inversion (`_PERSON_INVERSION_RE`).

**Decision: Skeptic wins.** Stage 1 does not re-implement inversion. It consumes `NormalizedEntity.normalized_name` post-Stage-0.

### D12. `get_aliases(canonical_id)` read method (brief line 44)

- **Skeptic**: not called by Stage 1 or 2; cut.

**Decision: Skeptic wins.** Drop. Stage 3 can add when needed.

### D13. Tax ID / EIN matching (brief line 25 "if available")

- **Skeptic**: schema has no EIN column; "if available" invites scope creep.

**Decision: Skeptic wins.** Drop from V1. Explicitly out of scope.

### D14. Type placement for `DeterministicMatch`, `CandidateSet`, `CandidateEntity`

- **Engineer**: put in shared `core/matching/types.py` — Stage 3 will import.

**Decision: Engineer wins.** Create `core/matching/types.py`.

### D15. `CandidateSet` must carry the source entity identifier

- **Engineer**: add `source_entity_id: str` field so Stage 3 can route results back to the originating `NormalizedEntity`.

**Decision: accepted.** `CandidateSet.source_entity_id`.

### D16. `match_key_type` typing

- **Skeptic + Engineer**: `Literal["alias_exact", "email", "employee_id"]` (no `canonical_echo` since 1c cut).

**Decision: accepted.**

---

## Scope adjustments

### Added to scope (refinements that strengthen the brief)
- Trigram sentinel padding (`^`/`$`) so short normalized names (`"ibm"`, `"hp"`) produce non-empty trigram sets.
- `tenant_id: Optional[str] = None` parameter on every entity_store read function.
- `lookup_alias_exact` and `lookup_email` return `List[str]` (collision-safe).
- `CandidateSet.source_entity_id` field for downstream routing.
- Confidence emission rules: alias_exact → `min(alias_conf, 0.99)`; email → `0.99` (single-hit, person-only); employee_id → `1.0` (single-hit).
- Shared `core/matching/types.py` for dataclasses imported by Stages 1, 2 (and later 3).
- Intra-system filter operates on `(source, source_id)` not on `category`.
- Hard truncate at 50 with `logger.warning` (no ranking).

### Removed from scope
- Metaphone / `PhoneticIndex` (no library installed; deferred until measured recall justifies it).
- `update()` method on indices (Stage 6's job).
- Stage 1c canonical_id echo (no producer at V1; pure speculation).
- Tax ID / EIN matching (no schema column).
- `get_aliases(canonical_id)` (unused by Stages 1–2).
- Person-name inversion in Stage 1 (already in Stage 0 normalizer).
- Candidate ranking / IDF weighting (cap never fires at V1 scale).

---

## Implementation constraints (from engineer)

1. **No `rapidfuzz` import in `deterministic.py` or `blocking.py`.** Rapidfuzz belongs to Stage 3 only. Enforce via a grep-style assertion in the test suite.
2. **Function-based `entity_store` module.** All functions take `conn: sqlite3.Connection` as first positional arg; no class state.
3. **Dataclasses are `frozen=True`.** `DeterministicMatch`, `CandidateEntity`, `CandidateSet`.
4. **Indices are pure in-memory `Dict[str, Set[str]]`.** Built once via `build_token_index(conn, tenant_id=None)` / `build_ngram_index(conn, tenant_id=None)`.
5. **Indices index both `canonical_name` (as a seed alias) AND every row of `entity_aliases`.** The fixture currently seeds no aliases; tests must insert aliases inline or rely on `canonical_name` as the sole index seed.
6. **Tokenization is whitespace-split on `normalized_name`.** Identical to what Stage 0 produces; do not re-tokenize raw input.
7. **No new pip dependencies.** Stdlib + existing requirements only.
8. **All code is Python 3.10+ (PEP 604 unions, `from __future__ import annotations`).**

---

## Real risks (skeptic) that became test cases

1. Alias collision (`"cenlar"` appears on two canonicals) → Stage 1 must return `None` and fall through, not pick one.
2. Shared inbox email (`ap@parent.com`) on two canonicals → Stage 1 email path must decline (multi-hit).
3. Email match on org canonical when query is person entity → reject (person-email anchor is person-only).
4. Stage 1a with stored alias confidence 0.91 → emitted match confidence ≤ 0.91.
5. `normalized_name="ibm"` (one trigram pre-padding) → with sentinel padding produces 3 trigrams; Stage 2 returns IBM canonical.
6. `normalized_name=""` or all-stripped degenerate input → blocking returns `CandidateSet(candidates=())` without exception.
7. Cross-tenant: tenant_A canonical with alias `"acme"`, query bearing `tenant_id=B` → no match.
8. Stage 2d filter: query `(source="quickbooks", source_id="QB-001")` against a candidate canonical that has both QB and RUDDR refs → candidate **survives** (not excluded).
9. Stage 2d filter: query `(source="quickbooks", source_id="QB-001")` against a candidate canonical whose only QB ref is exactly `QB-001` → candidate **excluded** (this IS Stage 1's job).
10. Empty `CandidateSet.candidates` (tuple, not list, not None) for "no blocking signal overlap" outcome.
11. Unicode round-trip: alias stored as `"andré dubois"` vs query normalized to `"andre dubois"` → must hit. This is verified by ensuring aliases are stored in their already-normalized form (entity_store seeding contract).

## Phantom risks dismissed (engineer)
- Concurrency / thread-safety: V1 is sequential Python, single-process.
- SQLite connection pooling: read-only, one connection per pipeline.
- Index memory footprint: kilobytes at 44 entities.
- Unicode lowercasing edge cases: Stage 0 normalizer owns this; Stage 1+2 consume already-normalized strings.

---

## Test surface (the suite the build prompt must require)

**`tests/test_deterministic.py`** — Stage 1 unit + integration:
- Exact alias hit → returns `DeterministicMatch(match_key_type="alias_exact")` with confidence = min(stored, 0.99).
- Alias collision → returns `None`.
- Person email hit (single canonical) → confidence 0.99, `match_key_type="email"`.
- Email shared across canonicals → returns `None`.
- Person query, email matches an organization canonical → returns `None`.
- No alias / no email match → returns `None`.
- Tenant scoping: explicit `tenant_id` mismatch → returns `None`.

**`tests/test_blocking.py`** — Stage 2 unit + integration:
- Token-only hit → `CandidateSet` non-empty.
- Trigram-only hit (typo case: query "cenlarr" against stored "cenlar") → candidate surfaces.
- Short name with sentinel padding (query "ibm") → trigrams non-empty, candidate surfaces if stored.
- Empty `normalized_name` (defensive) → `CandidateSet(candidates=())` with no exception.
- Intra-system exclusion: query's own `(source, source_id)` already in a canonical's system_references → that canonical excluded.
- Intra-system non-exclusion: canonical with refs in BOTH categories → survives even when query touches one of them.
- Candidate cap: synthetic fixture with 60 token collisions → result truncated to 50, `logger.warning` recorded.
- No-signal entity (no tokens, no trigrams in any stored entity) → empty `CandidateSet`.

**`tests/test_blocking.py` also covers**:
- End-to-end Stage 0 → Stage 1 → Stage 2 over all fixture entities. Asserts: every canonical resolves OR yields a non-empty candidate set under at least one of its source-record queries. Asserts no candidate set exceeds 50.

**Grep-style guard** (in either test file or a new `tests/test_no_rapidfuzz_at_blocking.py`):
- Read `core/matching/deterministic.py` and `core/matching/blocking.py` as text; assert `"rapidfuzz"` not in source.

---

## File manifest (exact paths)

Create:
- `core/matching/__init__.py` (empty)
- `core/matching/types.py`
- `core/matching/indices.py`
- `core/matching/deterministic.py`
- `core/matching/blocking.py`
- `core/graph/__init__.py` (empty)
- `core/graph/entity_store.py`
- `tests/test_deterministic.py`
- `tests/test_blocking.py`

Modify: none. (No edits to existing files.)

---

## Lines neither adversary ceded that I overruled

- **Skeptic wanted IDF weighting on candidate cap.** Overruled: cap doesn't fire at V1 scale; ranking without a tuning corpus is guesswork.
- **Design wanted Metaphone.** Overruled: no library in requirements; cost > benefit at 44 entities.
- **Design wanted `entity_store` as a `Protocol`.** Overruled: function module is simpler and matches existing test pattern.

All other suggestions from each adversary were incorporated above.
