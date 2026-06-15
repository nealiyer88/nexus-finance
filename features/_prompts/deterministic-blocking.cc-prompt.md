# Build Prompt — Deterministic Match + Blocking (Pipeline Stages 1–2)

## SITUATION

You are working in `/Users/nealiyer/code/nexus-finance`, a Python 3.10+ codebase that ships an entity-resolution pipeline against QuickBooks Online (accounting) and RUDDR (PSA) connector data. Six features have shipped before you:

1. rules-file-population — `/Users/nealiyer/code/nexus-finance/.claude/rules/01-nexus-finance-v1.md`
2. canonical-schema — `/Users/nealiyer/code/nexus-finance/db/schema_sqlite.sql` (tables: `canonical_entities`, `entity_aliases`, `entity_edges`, `system_references`)
3. normalizer — `/Users/nealiyer/code/nexus-finance/core/ingestion/normalizer.py` (Stage 0; produces `NormalizedEntity`)
4. connector-base — `/Users/nealiyer/code/nexus-finance/connectors/base.py` (`ConnectorInterface`, re-exports `NormalizedEntity`)
5. qb-connector — `/Users/nealiyer/code/nexus-finance/connectors/quickbooks.py`
6. ruddr-connector — `/Users/nealiyer/code/nexus-finance/connectors/ruddr.py`

Read in full BEFORE writing any code:
- `/Users/nealiyer/code/nexus-finance/CLAUDE.md`
- `/Users/nealiyer/code/nexus-finance/.claude/rules/01-nexus-finance-v1.md`
- `/Users/nealiyer/code/nexus-finance/features/pipeline/deterministic-blocking.md` (the original brief)
- `/Users/nealiyer/code/nexus-finance/features/_adversaries/deterministic-blocking.md` (the HARDENED DESIGN — this overrides the brief where they conflict)
- `/Users/nealiyer/code/nexus-finance/core/ingestion/normalizer.py`
- `/Users/nealiyer/code/nexus-finance/connectors/base.py`
- `/Users/nealiyer/code/nexus-finance/db/schema_sqlite.sql`
- `/Users/nealiyer/code/nexus-finance/tests/test_fixture_loads.py` (style baseline; pytest fixture seam for in-memory SQLite)
- `/Users/nealiyer/code/nexus-finance/tests/fixtures/canonical_ground_truth.json` (44 canonicals)
- `/Users/nealiyer/code/nexus-finance/tests/fixtures/qb_entities.json`
- `/Users/nealiyer/code/nexus-finance/tests/fixtures/ruddr_entities.json`

Current state: branch `autonomous-loop` is at commit `7ffbd99`. You will create branch `feature/deterministic-blocking` from current HEAD before making any changes.

## TASK

Build Pipeline Stages 1 (Deterministic Match) and 2 (Blocking) as eight new files under `core/matching/`, `core/graph/`, and `tests/`. No existing files are modified.

## FILE PATHS

Create EXACTLY these files (and nothing else):

1. `/Users/nealiyer/code/nexus-finance/core/matching/__init__.py` — empty.
2. `/Users/nealiyer/code/nexus-finance/core/matching/types.py` — shared dataclasses.
3. `/Users/nealiyer/code/nexus-finance/core/matching/indices.py` — `TokenIndex`, `NgramIndex`.
4. `/Users/nealiyer/code/nexus-finance/core/matching/deterministic.py` — `deterministic_match()`.
5. `/Users/nealiyer/code/nexus-finance/core/matching/blocking.py` — `generate_candidates()`.
6. `/Users/nealiyer/code/nexus-finance/core/graph/__init__.py` — empty.
7. `/Users/nealiyer/code/nexus-finance/core/graph/entity_store.py` — read-only SQLite helpers (module of functions; not a class).
8. `/Users/nealiyer/code/nexus-finance/tests/test_deterministic.py`
9. `/Users/nealiyer/code/nexus-finance/tests/test_blocking.py`

Do NOT create: a `PhoneticIndex`, a `metaphone`-related file, an `update()` method on indices, an `entity_store` class wrapper, or modifications to existing files.

## CONVENTIONS

- **Python 3.10+**, `from __future__ import annotations` at the top of every new `.py`.
- snake_case functions, PascalCase classes, UPPER_CASE constants (CLAUDE.md).
- All new dataclasses are `@dataclass(frozen=True)`.
- Imports: stdlib first, then third-party (none expected), then first-party (`from core...`, `from connectors...`).
- No `rapidfuzz` import anywhere in `core/matching/deterministic.py`, `core/matching/blocking.py`, `core/matching/indices.py`, `core/matching/types.py`, or `core/graph/entity_store.py`. (`rapidfuzz` belongs to Stage 3.)
- No new pip dependencies. Stdlib + existing `requirements.txt` only.
- Module docstrings briefly state stage and pipeline position; do not paraphrase rules schemas.
- No inline narrative comments explaining WHAT — only WHY when non-obvious (CLAUDE.md style).
- Test files use `pytest` fixture `conn` shaped like `tests/test_fixture_loads.py` (in-memory SQLite seeded from `db/schema_sqlite.sql`).
- All log writes are append-only — this feature writes none.

## SHARED DATACLASSES (exactly these shapes in `core/matching/types.py`)

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

MatchKeyType = Literal["alias_exact", "email", "employee_id"]

@dataclass(frozen=True)
class DeterministicMatch:
    canonical_id: str
    confidence: float
    match_key_type: MatchKeyType

@dataclass(frozen=True)
class CandidateEntity:
    canonical_id: str
    blocking_signals: tuple[str, ...]  # e.g. ("token:cenlar", "token:fsb", "trigram:cen")

@dataclass(frozen=True)
class CandidateSet:
    source_entity_id: str          # echoes NormalizedEntity.source_id
    candidates: tuple[CandidateEntity, ...]
```

## INDICES (in `core/matching/indices.py`)

Two classes. Both are pure in-memory `Dict[str, Set[str]]` wrappers.

```python
class TokenIndex:
    """Whitespace-token → set of canonical_ids."""
    def __init__(self) -> None: ...
    @classmethod
    def build(cls, conn: sqlite3.Connection, tenant_id: Optional[str] = None) -> TokenIndex:
        # Index every canonical_entities.canonical_name AND every entity_aliases.value.
        # Tokenize on whitespace AFTER applying the same normalization Stage 0 produces.
        # Since aliases/canonical_names are already stored normalized (per seeding contract),
        # this is just .split().
        # When tenant_id is None: no WHERE filter (V1 single-tenant default).
        ...
    def lookup(self, tokens: Iterable[str]) -> set[str]:
        # Union of canonical_ids across all tokens.
        ...

class NgramIndex:
    """Character-trigram (sentinel-padded) → set of canonical_ids."""
    N: ClassVar[int] = 3
    def __init__(self) -> None: ...
    @classmethod
    def build(cls, conn: sqlite3.Connection, tenant_id: Optional[str] = None) -> NgramIndex: ...
    def lookup(self, query: str) -> set[str]:
        # Generate trigrams from "^" + query + "$"
        # If query is empty or whitespace, return empty set.
        ...
    @staticmethod
    def trigrams(s: str) -> tuple[str, ...]:
        # Internal helper, also useful for tests.
        # Pads with sentinels: trigrams of "ibm" => ("^ib", "ibm", "bm$")
        ...
```

Implementation detail: store the inner dict as `Dict[str, set[str]]`. Lookups return `set[str]` (NOT `list`). No `update()` method. No `add()` method. No incremental insertion. Build-once, read-many.

## ENTITY STORE (in `core/graph/entity_store.py`)

Module of plain functions. NOT a class. Every function takes `conn: sqlite3.Connection` as first positional argument and `tenant_id: Optional[str] = None` as last.

```python
def lookup_alias_exact(
    conn: sqlite3.Connection,
    normalized_value: str,
    tenant_id: Optional[str] = None,
) -> list[tuple[str, float]]:
    """Return list of (canonical_id, alias_confidence) for exact-string alias match.
    Also matches canonical_entities.canonical_name as if it were an alias with
    confidence 1.0 (seed-alias contract).
    Empty list if no match. Multiple entries on collision.
    """

def lookup_email(
    conn: sqlite3.Connection,
    email: str,
    tenant_id: Optional[str] = None,
) -> list[str]:
    """Return list of canonical_ids whose system_references.external_fields JSON
    contains an "email" key whose value (case-folded) equals the query email
    (case-folded). Empty list if no match. Multiple entries on collision.

    NOTE: V1 schema has no first-class email column. Implementation reads
    system_references.external_fields (JSON TEXT) and filters in Python. This
    is acceptable at V1 scale (<500 canonicals).
    """

def lookup_employee_id(
    conn: sqlite3.Connection,
    employee_id: str,
    tenant_id: Optional[str] = None,
) -> list[str]:
    """Return list of canonical_ids whose system_references.external_fields JSON
    contains an "employee_id" key equal to the query. Empty if no match.
    """

def get_candidates_by_tokens(
    index: TokenIndex,
    tokens: Iterable[str],
) -> set[str]:
    """Thin wrapper around TokenIndex.lookup. Lives here to keep blocking.py
    decoupled from the index class import path. (Optional convenience; may be
    omitted if blocking.py imports the index directly.)
    """

def get_system_refs(
    conn: sqlite3.Connection,
    canonical_id: str,
) -> list[tuple[str, str]]:
    """Return list of (source, external_id) for every system_references row
    on the given canonical_id. Used by Stage 2d intra-system filter.
    """
```

No write functions. No `get_aliases`, no `update`, no class. Tenant filter: when `tenant_id is None`, do not add `WHERE tenant_id = ?`. When set, add the filter — but check `canonical_entities.tenant_id` (the column lives there; `entity_aliases` does not have its own `tenant_id`, so join via `canonical_id`).

## DETERMINISTIC (in `core/matching/deterministic.py`)

```python
def deterministic_match(
    entity: NormalizedEntity,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> Optional[DeterministicMatch]:
    """Stage 1: deterministic match against canonical registry.

    Sub-stages in order:

    1a. Exact alias match on entity.normalized_name.
        - Call lookup_alias_exact(conn, entity.normalized_name, tenant_id).
        - If returns exactly one (canonical_id, alias_conf):
            confidence = min(alias_conf, 0.99); match_key_type = "alias_exact"
            return DeterministicMatch(...)
        - If returns >1: collision, fall through (do not pick one).
        - If returns 0: continue.

    1b. Email match — PERSON ENTITIES ONLY (entity.email_is_person == True).
        - Skip if entity.email is None or entity.entity_category != "person".
        - Call lookup_email(conn, entity.email, tenant_id).
        - For each canonical_id in the result, verify the canonical has
          entity_category == "person" (query canonical_entities). Filter out
          any organizations.
        - If exactly one survives: confidence = 0.99; match_key_type = "email"
        - If >1 survive or 0 survive: continue.

    1c. Employee ID match — PERSON ENTITIES ONLY.
        - Look for entity.raw_record.get("employee_id") (string, non-empty).
        - Call lookup_employee_id(conn, employee_id_value, tenant_id).
        - If exactly one canonical_id: confidence = 1.0; match_key_type = "employee_id"
        - Else: continue.

    Returns None if no sub-stage produces a single-hit match.
    """
```

Do NOT implement: canonical_id echo (cut from V1), tax ID / EIN matching (cut from V1), person-name inversion (already in Stage 0).

## BLOCKING (in `core/matching/blocking.py`)

```python
CANDIDATE_CAP: int = 50

def generate_candidates(
    entity: NormalizedEntity,
    token_index: TokenIndex,
    ngram_index: NgramIndex,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> CandidateSet:
    """Stage 2: blocking — produce a bounded candidate set for fuzzy scoring.

    Steps:

    2a. Tokenize entity.normalized_name on whitespace. If empty, return
        CandidateSet(entity.source_id, ()).

    2b. Token lookup: collect canonical_ids from token_index.lookup(tokens),
        with the set of matching tokens recorded per candidate.

    2c. Trigram lookup: collect canonical_ids from ngram_index.lookup(entity.normalized_name),
        with each matching trigram recorded per candidate.

    2d. Intra-system filter: for each candidate canonical_id, query
        get_system_refs(conn, canonical_id). If the candidate's system_references
        already contains a row matching (source=entity.source, external_id=entity.source_id),
        exclude it. (That candidate is the query's own canonical via deterministic
        resolution; Stage 1 owns that path.) A candidate with refs on BOTH
        QB and RUDDR survives as long as no exact (source, source_id) match.

    2e. Cap: if surviving candidates exceed CANDIDATE_CAP (50), truncate to 50
        in sorted-by-canonical_id order and emit a logger.warning. (No
        signal-hit ranking, no IDF.)

    Build a CandidateEntity for each survivor with blocking_signals like
    ("token:cenlar", "token:fsb", "trigram:^ce", "trigram:cen", ...).
    Order signals deterministically (sorted).

    Return CandidateSet(source_entity_id=entity.source_id, candidates=tuple(...)).
    """
```

Logger: use `logging.getLogger(__name__)`. Do not configure logging globally.

## TEST COMMAND

Exact command that MUST pass at the end:

```
pytest tests/test_deterministic.py tests/test_blocking.py -x --tb=short
```

The full suite must also still pass:

```
pytest tests/ -x --tb=short
```

## ACCEPTANCE CRITERIA

All of the following must hold. Each maps to one or more tests in `tests/test_deterministic.py` or `tests/test_blocking.py`. Tests assert these in-code.

### Files exist and are importable
- [ ] `from core.matching.types import DeterministicMatch, CandidateEntity, CandidateSet, MatchKeyType` works.
- [ ] `from core.matching.indices import TokenIndex, NgramIndex` works.
- [ ] `from core.matching.deterministic import deterministic_match` works.
- [ ] `from core.matching.blocking import generate_candidates, CANDIDATE_CAP` works.
- [ ] `from core.graph.entity_store import lookup_alias_exact, lookup_email, lookup_employee_id, get_system_refs` works.

### Stage 1 (deterministic) behaviors
- [ ] Exact alias hit (single) returns `DeterministicMatch(match_key_type="alias_exact")` with `confidence == min(stored_alias_conf, 0.99)`.
- [ ] Alias collision (same value on two canonicals) returns `None`.
- [ ] Person entity with email matching exactly one PERSON canonical returns `DeterministicMatch(match_key_type="email", confidence=0.99)`.
- [ ] Email matching two canonicals returns `None`.
- [ ] Person query whose email matches only an ORGANIZATION canonical returns `None`.
- [ ] Person entity with `employee_id` matching exactly one canonical returns `DeterministicMatch(match_key_type="employee_id", confidence=1.0)`.
- [ ] Entity with no alias / no email / no employee_id hit returns `None`.
- [ ] When `tenant_id="A"` is passed and the matching alias lives on a canonical with `tenant_id="B"`, returns `None`.

### Stage 2 (blocking) behaviors
- [ ] Whitespace-token hit produces a non-empty `CandidateSet` with `blocking_signals` containing `"token:<token>"` entries (sorted).
- [ ] Trigram hit produces signals containing `"trigram:<gram>"` entries.
- [ ] Short normalized name (`"ibm"`) produces trigrams `("^ib", "ibm", "bm$")` (3 entries) — assert via `NgramIndex.trigrams("ibm")`.
- [ ] Empty normalized name returns `CandidateSet(source_entity_id=<id>, candidates=())` and does NOT raise.
- [ ] Intra-system exclusion: a query against a canonical whose `system_references` contains `(source=query.source, external_id=query.source_id)` excludes that canonical from candidates.
- [ ] Intra-system non-exclusion: a canonical with refs in BOTH `quickbooks` and `ruddr` survives when the query touches only one of them with a different external_id.
- [ ] Candidate cap: synthetic seed of >50 token-colliding canonicals → `len(result.candidates) == 50`.
- [ ] No blocking signal overlap → `CandidateSet.candidates == ()`.

### Cross-cutting / hygiene
- [ ] `rapidfuzz` is NOT imported in any file under `core/matching/` or `core/graph/`. Verify by reading file source as text.
- [ ] All five new dataclasses (`DeterministicMatch`, `CandidateEntity`, `CandidateSet`) are frozen — `dataclasses.fields(DeterministicMatch)` and `DeterministicMatch(...).__setattr__` raises.
- [ ] End-to-end fixture run: load all 44 canonicals into in-memory SQLite (mirror `tests/test_fixture_loads.py`), seed `entity_aliases` from each canonical's source `display_name` (one alias per source per canonical), feed each fixture qb_entity through normalizer → Stage 1 → Stage 2. Assert that every fixture canonical is either deterministic-matched OR produces a non-empty candidate set that includes its true canonical_id under at least one of its QB or RUDDR source-record queries.
- [ ] `pytest tests/test_deterministic.py tests/test_blocking.py -x --tb=short` passes.
- [ ] `pytest tests/ -x --tb=short` passes (no regression in prior 6 features' suites).

## NON-GOALS — DO NOT BUILD

- Pairwise scoring / RapidFuzz calls / Jaro-Winkler — that is Stage 3 (`features/pipeline/pairwise-scoring.md`).
- Threshold / disposition logic / AUTO_APPROVE constants — that is Stage 4.
- LLM fallback — that is Stage 5.
- Any write to `canonical_entities`, `entity_aliases`, `entity_edges`, or `system_references` outside test setup — that is Stage 6.
- Category-pair weight dispatch (`Dict[Tuple[str,str], WeightConfig]`) — that is Stage 3.
- Graph-corroborated adaptive scoring — Stage 3.
- A `PhoneticIndex` or Metaphone encoding (cut from V1 per hardened design).
- `update()` / `add()` methods on indices.
- A `entity_store` class wrapper.
- `get_aliases(canonical_id)` method.
- Stage 1c canonical_id echo.
- Tax ID / EIN matching.
- IDF weighting or signal-hit ranking on candidate cap.
- Person-name inversion (already in normalizer).
- New pip dependencies.
- Edits to any file outside the 9 new files listed in FILE PATHS.
- Modifications to `TEMPLATE.md`, `.claude/hooks/`, or `.claude/settings.json`.

## EXECUTION (ONE STEP AT A TIME)

Execute these steps strictly in order. Do not skip. Do not reorder. Do not bundle.

**Step 1.** From repo root `/Users/nealiyer/code/nexus-finance`, create branch `feature/deterministic-blocking` from current HEAD. Verify with `git status` — you should be on the new branch with a clean tree.

**Step 2.** Read the SITUATION files listed above (rules, brief, hardened design, normalizer, base, schema, fixture-loads test, fixture JSONs). Do not write code yet.

**Step 3.** Create `core/matching/__init__.py` (empty) and `core/graph/__init__.py` (empty).

**Step 4.** Create `core/matching/types.py` with the three frozen dataclasses (`DeterministicMatch`, `CandidateEntity`, `CandidateSet`) and the `MatchKeyType` Literal alias. Match the shapes specified above exactly.

**Step 5.** Create `core/graph/entity_store.py` with the five module-level functions specified. Pay attention to:
- Tenant filter is conditional (`if tenant_id is not None: ... else: ...`).
- `lookup_alias_exact` returns `list[tuple[str, float]]` (canonical_id, confidence). It must also match `canonical_entities.canonical_name` as if it were an alias with confidence 1.0 (the seed-alias contract). The simplest implementation: UNION the two queries.
- `lookup_email` / `lookup_employee_id` read `system_references.external_fields` as JSON and filter in Python (V1 schema has no first-class columns for these).
- Email comparison is case-insensitive (compare `.lower()` on both sides).

**Step 6.** Create `core/matching/indices.py` with `TokenIndex` and `NgramIndex`. Use `classmethod build(...)`, instance method `lookup(...)`, and `staticmethod trigrams(...)` on `NgramIndex`. No `update()`. The build seeds from BOTH `canonical_entities.canonical_name` AND `entity_aliases.value`.

**Step 7.** Create `core/matching/deterministic.py` with `deterministic_match()`. Honor the sub-stage ordering exactly: alias_exact → email (person-only, person-canonical-only) → employee_id (person-only). Return `None` on multi-hit at any sub-stage (the collision must not silently pick one).

**Step 8.** Create `core/matching/blocking.py` with `CANDIDATE_CAP = 50` and `generate_candidates()`. Honor 2a→2b→2c→2d→2e order. Logger via `logging.getLogger(__name__)`.

**Step 9.** Create `tests/test_deterministic.py`. Use a pytest `conn` fixture mirroring `tests/test_fixture_loads.py`. Add an inline helper that seeds a small synthetic registry (5–10 canonicals with controlled alias/email/employee_id setups) — sufficient to assert each acceptance criterion. Also include one larger test that loads the full 44-canonical fixture for smoke coverage.

**Step 10.** Create `tests/test_blocking.py`. Same fixture pattern. Include:
- Unit tests for `TokenIndex.build/lookup` and `NgramIndex.build/lookup/trigrams`.
- Behavioral tests for `generate_candidates` (each acceptance criterion).
- Cap-fire test (synthetic ≥60 canonicals all carrying token "consulting").
- End-to-end Stage 0 → Stage 1 → Stage 2 over the full fixture, asserting every canonical's source-record queries either deterministic-match or yield a non-empty candidate set that includes the true canonical_id.

**Step 11.** Run `pytest tests/test_deterministic.py tests/test_blocking.py -x --tb=short`. Fix any failures. Do not move on until green.

**Step 12.** Run `pytest tests/ -x --tb=short`. Confirm no regression. Fix any new failures (they're regressions caused by your changes — must fix, not waive).

**Step 13.** Run `grep -rn "rapidfuzz" core/matching/ core/graph/ || true`. Confirm zero hits.

**Step 14.** Run `git status` and `git diff --stat` to confirm exactly the 9 new files (and only those) are present.

**Step 15.** STOP. Do not commit. Do not push. Do not modify `FEATURE_QUEUE.md`, `SHIPPED.md`, `RUN_LOG.md`, `PROMPT_LOG.md`, or `CC-LEARNINGS.md` — those are Phase 6's job (the Rocket orchestrator), not the builder's.
