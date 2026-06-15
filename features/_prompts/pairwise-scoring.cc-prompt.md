# Build Prompt: Pairwise Scoring Engine (Pipeline Stage 3)

> Generated from `features/pipeline/pairwise-scoring.md` after Phase 1
> adversary reconciliation. The hardened design at
> `features/_adversaries/pairwise-scoring.md` is binding wherever it
> diverges from the brief.

---

## SITUATION

You are extending the matching pipeline. Stages 0 (normalizer), 1
(deterministic match), and 2 (blocking) have shipped. This feature
adds Stage 3: pairwise scoring of candidate pairs surfaced by Stage 2.

**Required reading before any code (in order):**

1. `.claude/rules/01-nexus-finance-v1.md` — V1 architectural guardrails
   (no XGBoost / fastText / LLM in scoring; rules §1, §5, §11, §13).
2. `features/pipeline/pairwise-scoring.md` — original brief.
3. `features/_adversaries/pairwise-scoring.md` — **HARDENED DESIGN**
   (binding when it diverges from the brief; especially Sections 1.1,
   1.2, 1.10 for removals).
4. `core/matching/types.py` — existing frozen dataclasses
   (`DeterministicMatch`, `CandidateEntity`, `CandidateSet`). You will
   APPEND to this file (do not rewrite existing types).
5. `core/matching/blocking.py` — predecessor stage producing
   `CandidateSet`. Note `CANDIDATE_CAP = 50` and intra-system filter.
6. `core/matching/deterministic.py` — sibling stage for shape parity
   (frozen dataclass return; tenant_id pattern).
7. `core/graph/entity_store.py` — existing read functions
   (`lookup_alias_exact`, `lookup_email`, `lookup_employee_id`,
   `get_system_refs`, `get_entity_category`). You will APPEND four new
   functions following the same pattern (tenant_id optional, module-
   level, no class wrapper, no write path).
8. `core/ingestion/normalizer.py` (first 100 lines) — `NormalizedEntity`
   shape. Person-name inversion happens HERE (lines 184–189), NOT in
   Stage 3.
9. `connectors/base.py` (lines 1–110) — `NormalizedEntity` re-export.
10. `db/schema_sqlite.sql` — confirms `entity_edges`, `entity_aliases`,
    `system_references`, `canonical_entities`. No `transactions` table
    exists (this is why "shared transaction context" is removed from
    scope).
11. `tests/test_blocking.py` line 371 — `test_no_rapidfuzz_in_matching_modules`
    enumerates the no-rapidfuzz files; `scoring.py` is intentionally
    NOT on that list (Stage 3 IS allowed to use RapidFuzz).
12. `tests/fixtures/canonical_ground_truth.json` — 44 canonical
    entities, 19 org pairs + 25 person pairs. Used as test seed.

**What shipped prior (most recent first):**

- 2026-05-23 — `deterministic-blocking` (Stages 1 + 2)
- 2026-05-10 — `ruddr-connector`, `qb-connector`, `connector-base`
- 2026-05-09 — `normalizer`, `fixture-canonical-types`,
  `canonical-schema`, `rules-file-population`

---

## TASK

Implement Pipeline Stage 3 (Pairwise Scoring) with category-pair
weight dispatch, RapidFuzz signals, n-gram Jaccard, PSA abbreviation
heuristic, and graph-corroborated bonuses.

Single deliverable: a `score_pair(entity, candidate_id, candidate_name,
candidate_aliases, candidate_category, conn, tenant_id=None) ->
ScoredMatch` function in `core/matching/scoring.py`, plus a thin
convenience wrapper `score_candidate_set` for deterministic ordering.

---

## FILE PATHS

**Create:**
- `/Users/nealiyer/code/nexus-finance/core/matching/scoring.py` (new)
- `/Users/nealiyer/code/nexus-finance/core/matching/weights.py` (new)
- `/Users/nealiyer/code/nexus-finance/tests/test_scoring.py` (new)

**Modify (APPEND, do not rewrite):**
- `/Users/nealiyer/code/nexus-finance/core/matching/types.py` — append
  `SignalBreakdown`, `GraphEvidence`, `ScoredMatch` frozen dataclasses.
- `/Users/nealiyer/code/nexus-finance/core/graph/entity_store.py` —
  append `get_aliases`, `get_canonical_name_and_category`,
  `count_shared_person_neighbors`, `count_shared_graph_neighbors`.

**Do NOT touch:**
- `.claude/hooks/*`, `.claude/settings.json`
- `features/TEMPLATE.md`
- Any file under `connectors/`, `db/`, existing
  `core/ingestion/normalizer.py`, existing
  `core/matching/{deterministic,blocking,indices,__init__}.py`
- `requirements.txt` (no new deps).

---

## CONVENTIONS

- Python 3.10+. Type hints on every public function. `from __future__
  import annotations` at the top of every new module.
- snake_case functions, PascalCase classes, UPPER_CASE constants.
- Frozen dataclasses for all return shapes (matches
  `DeterministicMatch`, `CandidateSet`).
- Module-level functions in `entity_store.py` (no class wrapper —
  matches existing convention).
- `tenant_id: Optional[str] = None` parameter on every entity_store
  read; SQL gates on `tenant_id IS NULL` semantics via the existing
  `if tenant_id is None` branch pattern.
- Docstring on every public function. Module-level docstring on every
  new file explaining what it owns and what it explicitly does NOT
  do (matches `deterministic.py` / `blocking.py` style).
- Imports: stdlib first, then `connectors.base`, then `core.*`. Use
  `rapidfuzz` ONLY in `scoring.py`. Do NOT import `rapidfuzz` in
  `weights.py`, `types.py`, `entity_store.py`, or anywhere on the
  no-rapidfuzz list.
- Logging via `logging.getLogger(__name__)` if needed (no warnings
  expected from scoring at V1; reserved for future use).

---

## TEST COMMAND

```
pytest tests/ -x --tb=short
```

Must pass with zero failures. Existing tests must remain green
(currently 205 tests passing per the deterministic-blocking ship log).
Adding tests is fine; modifying or skipping existing tests is not.

---

## ACCEPTANCE CRITERIA

From the brief (`features/pipeline/pairwise-scoring.md` Success
Criteria, applied with the hardened-design amendments):

- [ ] `core/matching/scoring.py` exists with `score_pair()` function.
- [ ] `core/matching/weights.py` exists with `WeightConfig` and
      `get_weights()`.
- [ ] Signal breakdown returned for every scored pair (all individual
      weighted signal values visible as a `SignalBreakdown` frozen
      dataclass).
- [ ] Category-pair dispatch selects different weights for PSA↔Accounting
      vs default (table: two cross-pair entries + `_default` fallback).
- [ ] Graph-corroborated scoring queries `entity_store` for shared
      person entities. (Transactions signal REMOVED — see hardened
      design §1.2.)
- [ ] "CEN" (RUDDR) vs "Cenlar, LLC" (QB) — abbreviation heuristic
      fires, score > 0.70.
- [ ] "Chen, Michael" (QB) vs "Michael Chen" (RUDDR) — both normalized
      to "michael chen" — scores ≥ 0.95 via the normal string-metric
      path (NO 0.95 direct override — Stage 0 owns inversion).
- [ ] "BrightPath Machine Learning Corp" (QB) vs "Luminos AI" (RUDDR)
      — rebrand pattern scores < 0.50.
- [ ] All 44 ground truth pairs produce scores > 0.50.
- [ ] `pytest tests/test_scoring.py` passes.

From the hardened design's additional test list (§6 of
`features/_adversaries/pairwise-scoring.md`) — all 20 must be present
in `tests/test_scoring.py`:

1. `test_weights_sum_to_one`
2. `test_score_clamped_to_unit_interval`
3. `test_empty_normalized_name_returns_zero_score`
4. `test_empty_candidate_name_returns_zero_score`
5. `test_psa_abbreviation_heuristic_does_not_fire_on_long_names`
6. `test_psa_abbreviation_heuristic_fires_on_prefix_match`
7. `test_psa_abbreviation_heuristic_fires_on_initialism_match`
8. `test_alias_boost_single_application`
9. `test_shared_person_bonus_capped`
10. `test_neighborhood_overlap_bonus_capped`
11. `test_graph_evidence_zero_on_empty_edge_table`
12. `test_dispatch_returns_default_for_unconfigured_pair`
13. `test_dispatch_returns_psa_accounting_for_cross_pair`
14. `test_score_candidate_set_orders_by_descending_score`
15. `test_score_candidate_set_breaks_ties_by_canonical_id`
16. `test_no_xgboost_no_fasttext_no_llm_in_scoring`
17. `test_signal_breakdown_carries_every_weighted_signal`
18. `test_44_ground_truth_pairs_score_above_no_match`
19. `test_10_synthesized_non_match_pairs_score_below_no_match`
20. `test_person_inversion_pair_scores_at_least_0_95_via_string_metrics`

---

## NON-GOALS

From brief Out of Scope + hardened design removals — do NOT build any
of these:

- XGBoost classifier (V1 uses fixed dispatch — rules §11).
- fastText cosine similarity (V1 uses n-gram Jaccard — rules §11).
- LLM parallel assessment (Stage 5 territory — rules §11).
- Threshold application / AUTO_APPROVE / SURFACE / NO_MATCH branching
  (Stage 4, separate feature).
- Cluster conflict detection (Stage 4).
- Graph writes / edge insertion (Stage 6).
- **Shared transaction context signal** (removed per hardened design
  §1.2 — no transactions table in V1 schema).
- **Person-name inversion direct-score** (removed per hardened design
  §1.1 — Stage 0 owns it).
- **Email +0.10 soft signal** (removed per hardened design §1.10 —
  Stage 1 already resolves email matches).
- Pipeline orchestrator (the function that chains Stages 0→3) — Stage 3
  exposes `score_pair` and `score_candidate_set` and that's it.
- Approval feedback loop / weight tuning (V2 — rules §11).
- New connector scaffolding (V1 connector set is QB + RUDDR — rules
  §13).
- Updating `requirements.txt`, `db/schema_sqlite.sql`, or
  `connectors/base.py`.

---

## EXECUTION (ONE STEP AT A TIME)

Do not batch steps. Run pytest after every major step that adds tests.

### Step 0 — Branch

```
git checkout -b feature/pairwise-scoring
```

Verify `git status` is clean (the open `rocket.sh` modification stays
in working tree but is not committed in this branch's scope — leave it
unstaged).

### Step 1 — Append types to `core/matching/types.py`

Add three frozen dataclasses (DO NOT modify existing ones):

```python
@dataclass(frozen=True)
class SignalBreakdown:
    token_sort_ratio: float        # 0..100 (RapidFuzz scale)
    token_set_ratio: float         # 0..100
    partial_ratio: float           # 0..100
    jaro_winkler: float            # 0..100
    ngram_jaccard: float           # 0..1
    alias_boost_fired: bool
    abbreviation_bonus_fired: bool


@dataclass(frozen=True)
class GraphEvidence:
    shared_person_count: int
    shared_person_bonus: float        # capped at 0.10
    neighborhood_overlap_count: int
    neighborhood_overlap_bonus: float # capped at 0.10


@dataclass(frozen=True)
class ScoredMatch:
    canonical_id: str
    score: float                          # clamped [0.0, 1.0]
    signal_breakdown: SignalBreakdown
    graph_evidence: GraphEvidence
    category_pair: tuple[str, str]
    weight_profile_id: str
```

### Step 2 — Write `core/matching/weights.py`

Public surface:

- `@dataclass(frozen=True) class WeightConfig` with these 9 fields,
  defaults shown:
  - `token_sort_ratio: float`
  - `token_set_ratio: float`
  - `partial_ratio: float`
  - `jaro_winkler: float`
  - `ngram_jaccard: float`
  - `alias_boost: float`
  - `abbreviation_bonus: float`
  - `profile_id: str`
- `DEFAULT_WEIGHTS = WeightConfig(0.25, 0.25, 0.15, 0.10, 0.10, 0.15,
  0.0, "default_v1")`
- `PSA_ACCOUNTING_WEIGHTS = WeightConfig(0.25, 0.25, 0.15, 0.10, 0.10,
  0.15, 0.20, "psa_accounting_v1")`
- `_DISPATCH: Dict[Tuple[str, str], WeightConfig]` populated as:
  - `("accounting", "psa"): PSA_ACCOUNTING_WEIGHTS`
  - `("psa", "accounting"): PSA_ACCOUNTING_WEIGHTS`
- `def get_weights(source_category: str, target_category: str) ->
  WeightConfig:` returns dispatch hit or `DEFAULT_WEIGHTS`.

Module docstring explains that this module is the V1 substitute for
XGBoost (rules §11) and that weights are deliberately interpretable.

Invariant (asserted by test): `DEFAULT_WEIGHTS` and
`PSA_ACCOUNTING_WEIGHTS`'s `token_sort_ratio + token_set_ratio +
partial_ratio + jaro_winkler + ngram_jaccard + alias_boost` sum to
`1.0` to within 1e-9. Bonuses (`abbreviation_bonus`) and graph
evidence are additive on top and excluded from the sum-to-one check.

### Step 3 — Append four entity_store functions to
`core/graph/entity_store.py`

All four follow the same `tenant_id: Optional[str] = None` pattern
already in the file. Append at the bottom; do not modify existing
functions.

```python
def get_aliases(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> list[str]:
    """Return every `entity_aliases.value` for this canonical, excluding
    the canonical's own `canonical_name` (the caller already has it).
    Tenant-scoped when `tenant_id` is set."""

def get_canonical_name_and_category(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> Optional[tuple[str, str, str]]:
    """Return `(canonical_name, entity_category, entity_type)` or None
    if the canonical does not exist (or fails tenant scope)."""

def count_shared_person_neighbors(
    conn: sqlite3.Connection,
    source_canonical_id: Optional[str],
    candidate_canonical_id: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Count `entity_edges` rows where both `source_canonical_id` and
    `candidate_canonical_id` connect to the same `person` canonical
    via any relationship. Returns 0 if `source_canonical_id` is None
    (Stage 3 query side is unresolved). Both endpoints checked
    bidirectionally (source_node OR target_node)."""

def count_shared_graph_neighbors(
    conn: sqlite3.Connection,
    source_canonical_id: Optional[str],
    candidate_canonical_id: str,
    tenant_id: Optional[str] = None,
) -> int:
    """Count distinct canonical_ids that connect (bidirectionally) to
    BOTH `source_canonical_id` and `candidate_canonical_id` via
    `entity_edges`. Returns 0 if `source_canonical_id` is None."""
```

Tenant scoping pattern: when `tenant_id` is not None, JOIN
`canonical_entities AS c ON c.canonical_id = ?` and filter
`c.tenant_id = ?` for each canonical-id-bearing column.

### Step 4 — Write `core/matching/scoring.py`

Module docstring (mandatory):

- States this module is Pipeline Stage 3.
- States that it owns the only `rapidfuzz` import in the matcher.
- States that it does NOT mutate the graph, does NOT call LLMs, does
  NOT consult Stage 1's anchors directly (it operates on candidates
  Stage 2 produced).
- States the score formula:
  `score = clamp(weighted_sum(signals) + alias_boost + abbreviation_bonus + graph_evidence_bonuses, 0.0, 1.0)`.

Public surface:

- `NGRAM_N = 3`
- `NGRAM_PAD_LEFT = "^"`, `NGRAM_PAD_RIGHT = "$"`
  (match `core/matching/indices.NgramIndex` constants)
- `ALIAS_BOOST_THRESHOLD = 85.0` (RapidFuzz scale)
- `SHORTCODE_MAX_LEN = 4`
- `PER_SHARED_PERSON_BONUS = 0.05`
- `MAX_SHARED_PERSON_BONUS = 0.10`
- `PER_NEIGHBORHOOD_NODE_BONUS = 0.025`
- `MAX_NEIGHBORHOOD_BONUS = 0.10`
- `def ngram_jaccard(a: str, b: str) -> float:`
  Returns 0.0 if either input is empty after strip OR if either
  input length (unpadded) is less than `NGRAM_N`. Otherwise returns
  `|A∩B| / |A∪B|` where A, B are sets of sentinel-padded trigrams.
- `def _check_psa_abbreviation(...) -> bool:`
  Implements the algorithm in hardened design §1.12. Pure function.
- `def _compute_alias_boost(...) -> bool:`
  Returns True iff `max(token_set_ratio, jaro_winkler)` of the entity
  name against any candidate alias exceeds `ALIAS_BOOST_THRESHOLD`.
- `def _compute_signal_breakdown(...) -> SignalBreakdown:`
  Calls RapidFuzz four times + `ngram_jaccard` + alias/abbreviation
  fires.
- `def _compute_graph_evidence(...) -> GraphEvidence:`
  Queries `count_shared_person_neighbors` and
  `count_shared_graph_neighbors`; applies per-node bonus * count
  caps; returns `GraphEvidence`.
- `def score_pair(entity, candidate_id, candidate_name,
  candidate_aliases, candidate_category, conn, tenant_id=None) ->
  ScoredMatch:`
  Empty-string guard at the top: if `not entity.normalized_name.strip()`
  or `not candidate_name.strip()`, return `ScoredMatch` with score
  0.0 and all-zero / all-false breakdowns and evidence (still carries
  `weight_profile_id` from dispatch and `category_pair`).
  Otherwise: dispatch weights via `get_weights(entity.category,
  candidate_category)`, compute breakdown, compute evidence,
  compute clamped score.
- `def score_candidate_set(entity, candidate_set, conn, tenant_id=None)
  -> tuple[ScoredMatch, ...]:`
  For each `CandidateEntity` in `candidate_set.candidates`, fetch
  candidate name/aliases/category via the new `entity_store` reads;
  call `score_pair`; sort results by `(-score, canonical_id)`; return
  tuple.

Implementation notes:

- `source_canonical_id` for `score_pair` is **not known** (Stage 3
  receives an unresolved entity from Stage 2). Pass `None` to the
  shared-neighbor count functions so they return 0 cleanly. The
  graph signal becomes meaningful only AFTER Stage 6 begins writing
  edges where the incoming entity has been assigned a canonical_id —
  V2 wiring.
- RapidFuzz import: `from rapidfuzz import fuzz`. Use
  `fuzz.token_sort_ratio`, `fuzz.token_set_ratio`,
  `fuzz.partial_ratio`, `fuzz.WRatio`? — **NO. Use
  `fuzz.token_sort_ratio`, `fuzz.token_set_ratio`,
  `fuzz.partial_ratio` and `from rapidfuzz.distance import
  JaroWinkler` then `JaroWinkler.similarity(a, b) * 100`.** Do not
  use `WRatio` (it composes signals internally and breaks the
  breakdown requirement).
- The weighted-sum formula divides each RapidFuzz output by 100 so
  it lives on the same [0, 1] scale as `ngram_jaccard` before
  multiplying by the weight. Bonuses are already on [0, 1].
- `tenant_id` is threaded into every `entity_store` call inside
  `score_pair` / `score_candidate_set`.

### Step 5 — Run pytest to confirm baseline still green

```
pytest tests/ -x --tb=short
```

All 205 prior tests must still pass with `scoring.py` and `weights.py`
in place (no tests yet in `test_scoring.py`).

### Step 6 — Write `tests/test_scoring.py`

Use the same fixture pattern as `tests/test_blocking.py`:
- `conn` fixture creating `:memory:` SQLite from
  `db/schema_sqlite.sql`
- helpers to insert canonical / alias / system_ref / edge rows
- `REPO_ROOT`, `FIXTURE_GT`, `FIXTURE_QB`, `FIXTURE_RUDDR` constants

Implement **all 20 hardened-design tests + the 9 brief success criteria
tests** (some overlap). One pytest function per item. Names exactly as
listed in ACCEPTANCE CRITERIA above. The mandatory ones from the
hardened design plus the original brief criteria:

Additional test cases (matching brief success criteria):

- `test_dispatch_selects_psa_accounting_weights` — assert
  `get_weights('accounting', 'psa').profile_id == 'psa_accounting_v1'`
- `test_cen_vs_cenlar_scores_above_surface` — abbreviation heuristic
  fires on "CEN" / "Cenlar FSB" pair; score > 0.70.
- `test_rebrand_pair_scores_below_no_match` — "BrightPath Machine
  Learning Corp" / "Luminos AI"; score < 0.50.

For the `test_44_ground_truth_pairs_score_above_no_match` test:
- Load `tests/fixtures/canonical_ground_truth.json`.
- For each canonical entity, construct entity = QB side (normalized
  via the normalizer), candidate = RUDDR side (normalized via the
  normalizer). Call `score_pair`. Assert returned score > 0.50.
- If a single pair fails, the assertion message must include the
  canonical_id, both display names, and the computed score so the
  failure is debuggable.

For the `test_10_synthesized_non_match_pairs_score_below_no_match`
test:
- Seed `random.Random(42)`.
- Build the cross-product (QB entity X, RUDDR entity Y) where X.canonical_id != Y.canonical_id (per ground truth).
- Random.sample 10 pairs.
- Assert all score < 0.50.

For tests that need to verify cap behavior, manually insert canonical
entities and edge rows directly into the test conn.

### Step 7 — Run pytest

```
pytest tests/ -x --tb=short
```

Expected: 205 prior + ~25 new = ~230 passing, 0 failing. If any test
fails, fix the production code (NOT the test) unless the test itself
is wrong.

### Step 8 — Confirm hygiene: no forbidden imports

The `test_no_xgboost_no_fasttext_no_llm_in_scoring` test you wrote
will check this. The existing `test_no_rapidfuzz_in_matching_modules`
in `tests/test_blocking.py` will continue to pass because
`scoring.py` is NOT in its target list.

### Step 9 — Commit

```
git add core/matching/scoring.py core/matching/weights.py \
        core/matching/types.py core/graph/entity_store.py \
        tests/test_scoring.py \
        features/_adversaries/pairwise-scoring.md \
        features/_prompts/pairwise-scoring.cc-prompt.md
git commit -m "Stage 3 Pairwise Scoring [Rocket build]"
```

Do NOT push; the Rocket Phase 6 ship step handles push + log writes.

---

End of build prompt. The reviewer agents in Phase 4 will validate
against the brief's success criteria + the hardened design's added
tests + general code quality.
