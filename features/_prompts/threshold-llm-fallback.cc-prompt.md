# Build Prompt: Threshold + Disposition + LLM Fallback (Stages 4 + 5)

> Generated from `features/pipeline/threshold-llm-fallback.md` after Phase 1
> adversary reconciliation. The hardened design at
> `features/_adversaries/threshold-llm-fallback.md` is binding wherever it
> diverges from the brief.

---

## SITUATION

You are extending the matching pipeline. Stages 0 (normalizer),
1 (deterministic), 2 (blocking), and 3 (pairwise scoring) have shipped.
This feature adds **Stage 4 (Threshold + Cluster Conflict Detection)**
and **Stage 5 (LLM Fallback with mandatory PII redaction)**.

**Required reading before any code (in this order):**

1. `.claude/rules/01-nexus-finance-v1.md` — V1 guardrails (rules §1,
   §5 thresholds, §10 data security/redaction, §11 NOT-SCOPE, §13).
2. `features/pipeline/threshold-llm-fallback.md` — original brief.
3. `features/_adversaries/threshold-llm-fallback.md` — **HARDENED
   DESIGN, binding** wherever it diverges from the brief (16
   disagreement decisions in Section 1; scope add/cut in Section 2;
   constraints in Section 3; test cases in Section 6).
4. `core/matching/types.py` — existing frozen dataclasses (`ScoredMatch`,
   `SignalBreakdown`, `GraphEvidence`, `CandidateSet`). You will
   **APPEND** new types (`Action`, `Disposition`, `LLMAssessment`,
   `RedactedPrompt`) — do not rewrite or rename existing types.
5. `core/matching/scoring.py` (read in full) — produces `ScoredMatch`,
   which is the Stage 4 input. Do not modify this file.
6. `core/matching/weights.py` — sibling module for naming/style parity.
   Do not modify.
7. `core/matching/blocking.py` and `core/matching/deterministic.py`
   — sibling modules for style parity (frozen dataclasses, tenant_id
   pattern, module-level functions).
8. `core/graph/entity_store.py` (read in full) — existing read helpers
   (`lookup_alias_exact`, `lookup_email`, `lookup_employee_id`,
   `get_aliases`, `get_canonical_name_and_category`, `get_system_refs`
   if present, `count_shared_person_neighbors`,
   `count_shared_graph_neighbors`). You will **APPEND** one new
   function: `are_clustered(conn, cid_a, cid_b, tenant_id=None) -> bool`.
   Do not modify existing functions.
9. `connectors/base.py` (lines 1–215) — `NormalizedEntity` shape
   (`canonical_id`, `entity_type`, `tenant_id`, attributes dict,
   etc.). `NormalizedEntity.entity_type` is the source-side
   entity-type discriminator.
10. `db/schema_sqlite.sql` — confirms `canonical_entities.entity_category`
    (`organization` | `person`), `entity_edges` (will hold future
    `SAME_AS` rows), `entity_aliases`, `system_references`.
11. `db/migrations/001_canonical_schema.sql` — style template for the
    new migration you will write (`002_llm_training_data.sql`).
12. `tests/test_scoring.py` — style template for the new test modules
    (in-memory SQLite fixture, frozen-dataclass assertions).
13. `tests/fixtures/canonical_ground_truth.json` — 44 canonical
    entities (19 org pairs + 25 person pairs). Reference for the
    "<15% Tier 3" check and the person-redaction leak test.
14. `requirements.txt` lines 11–13, 23–24 — `anthropic==0.39.0`,
    `python-dotenv==1.0.1`, `structlog==24.4.0` are pinned. No new deps.

**What shipped prior (most recent first):**

- `pairwise-scoring` (c53d0da, 2026-06-14) — Stage 3; produces
  `ScoredMatch`.
- `deterministic-blocking` (ff5db1c, 2026-05-23) — Stages 1 + 2.
- `ruddr-connector`, `qb-connector`, `connector-base` — Stage 0 inputs.

---

## TASK

Implement Pipeline Stages 4 (Threshold + Cluster Conflict Detection)
and 5 (LLM Fallback with redaction) on a new feature branch
`feature/threshold-llm-fallback`. Three new modules under
`core/matching/`, one append to `core/matching/types.py`, one append
to `core/graph/entity_store.py`, one new SQLite migration, three new
test modules. Net: turn a `tuple[ScoredMatch, ...]` into a
`Disposition` and, where the disposition is `LLM_FALLBACK`, populate
`Disposition.llm_assessment` via a redacted Claude call — without
leaking PII and without ever auto-approving the LLM verdict.

---

## FILE PATHS

### Create
- `core/matching/disposition.py` — `apply_thresholds()`, threshold
  constants, dedup, cluster-conflict logic. ~150 lines.
- `core/matching/llm_fallback.py` — `llm_assess()`, `LLMClient`
  Protocol, `_default_llm_client_factory()`, `_AnthropicAdapter`,
  three exceptions (`LLMNotConfiguredError`, `LLMBudgetExceededError`,
  `LLMResponseError`), training-row writer, outbound leak-check.
  ~250 lines.
- `core/matching/redaction.py` — `redact_org()`, `redact_person()`,
  `leak_check()`, `_shape_class_code()`, `_shape_project_code()`.
  Pure functions; no SQLite, no SDK, no env reads. ~150 lines.
- `core/matching/__init__.py` — append exports (`apply_thresholds`,
  `llm_assess`, `Disposition`, `LLMAssessment`, etc.) IF the existing
  `__init__.py` exports prior stages' public API. If it is empty,
  leave it empty (match prevailing convention).
- `db/migrations/002_llm_training_data.sql` — table + index + the
  GDPR carve-out comment (verbatim block in hardened design §2 item 4).
- `tests/test_disposition.py` — parametrized boundary tests + cluster
  conflict + tenant scoping + empty/dup-candidate edge cases.
  ~25 tests.
- `tests/test_llm_fallback.py` — dispatch / never-auto-approve /
  training capture / factory error / budget / tool-call schema /
  reasoning scrub. ~15 tests. Uses an in-test `FakeLLMClient`.
- `tests/test_redaction.py` — org shape-only emission + person
  zero-name + fixture sweep + leak_check positive/negative cases.
  ~12 tests.

### Append to (do not rewrite)
- `core/matching/types.py` — append `Action` Literal,
  `Disposition`, `LLMAssessment`, `RedactedPrompt` frozen dataclasses.
  Add the Stage 4/5 section header comment block matching the
  existing "Stage 3" comment block on line 37–39.
- `core/graph/entity_store.py` — append `are_clustered()` at end of
  file, following the existing module-function pattern (tenant_id
  optional, docstring, parameterized SQL).

### Do NOT touch
- `core/matching/scoring.py`, `core/matching/weights.py`,
  `core/matching/blocking.py`, `core/matching/deterministic.py`,
  `core/matching/indices.py`.
- `connectors/*`, `core/ingestion/normalizer.py`.
- `db/schema_sqlite.sql`, `db/schema.sql`,
  `db/migrations/001_canonical_schema.sql`.
- Any existing test file.
- `.claude/rules/`, `CLAUDE.md`, `TEMPLATE.md`, `FEATURE_QUEUE.md`,
  `features/SHIPPED.md`, `features/RUN_LOG.md` — Phase 6 owns those.

---

## CONVENTIONS

- Python 3.10+. `from __future__ import annotations` at the top of
  every new `.py` file.
- snake_case functions, PascalCase classes, UPPER_CASE constants.
- Frozen dataclasses for all return types
  (`@dataclass(frozen=True)`). Match `ScoredMatch` style.
- Module-level functions (no class wrappers — match `entity_store.py`
  and `deterministic.py`).
- `tenant_id: Optional[str] = None` on every function that issues a
  DB query (match `entity_store.py` pattern).
- Imports ordered: stdlib → third-party → first-party. Match prior
  modules.
- Docstrings: triple-double-quoted, first line ≤ 80 chars, blank line,
  then detail. Match `scoring.py` module docstring style.
- No raw SQL in `disposition.py` or `llm_fallback.py` — go through
  `entity_store.py`. The training-row insert IS allowed to live in
  `llm_fallback.py` (it's a write path and entity_store.py is read-only
  per its own docstring) — write it as a small inline helper inside
  `llm_fallback.py`.
- structlog for logs (`structlog.get_logger(__name__)`). Match
  prior modules. **Never log the redacted prompt text or LLM
  reasoning content.** Log only `call_id`, `prompt_sha256`,
  `category_pair`, outcome (`"ok"` / `"redacted_leak"` / `"no_tool_use"`).
- No `print()`. No `# type: ignore` without a comment explaining why.
- No new third-party deps. `anthropic`, `python-dotenv`, `structlog`
  are already pinned.
- Do not modify `TEMPLATE.md`, `.claude/hooks/`, or
  `.claude/settings.json`.

---

## TEST COMMAND

```bash
pytest tests/test_disposition.py tests/test_llm_fallback.py tests/test_redaction.py -x --tb=short
```

Followed by full-suite regression check:

```bash
pytest tests/ -x --tb=short
```

Both must pass with **zero new failures** and zero regressions from
the 234 currently-green tests.

CI safety check (must produce zero hits):

```bash
grep -rn "anthropic.Anthropic()" tests/
```

---

## ACCEPTANCE CRITERIA

Structural (mechanical, verifiable by static check):

- [ ] `python -c "from core.matching.disposition import apply_thresholds, Disposition, AUTO_APPROVE_THRESHOLD, SURFACE_THRESHOLD, LLM_FALLBACK_THRESHOLD"` succeeds.
- [ ] `python -c "from core.matching.llm_fallback import llm_assess, LLMClient, LLMNotConfiguredError, LLMBudgetExceededError, LLMResponseError, MAX_LLM_CALLS_PER_RUN"` succeeds.
- [ ] `python -c "from core.matching.redaction import redact_org, redact_person, leak_check, RedactedPrompt"` succeeds.
- [ ] `python -c "from core.matching.types import Action, Disposition, LLMAssessment, RedactedPrompt"` succeeds.
- [ ] `python -c "from core.graph.entity_store import are_clustered"` succeeds.
- [ ] `core/matching/disposition.py` declares constants `AUTO_APPROVE_THRESHOLD = 0.90`, `SURFACE_THRESHOLD = 0.70`, `LLM_FALLBACK_THRESHOLD = 0.50` at module level. No other thresholds module is created.
- [ ] `db/migrations/002_llm_training_data.sql` exists and creates the table verbatim per hardened design §2 item 4 (PK = `call_id`, nullable `tenant_id`, `prompt_sha256` column, GDPR comment present).

Behavioral (verified by tests in this PR):

- [ ] Threshold dispatch is `score >= 0.90 → AUTO_APPROVE`, `0.70 <= score < 0.90 → QUEUE_FOR_REVIEW`, `0.50 <= score < 0.70 → LLM_FALLBACK`, `score < 0.50 → NO_MATCH`. Exact-boundary tests at 0.50, 0.70, 0.90 cover the `>=` rule.
- [ ] Empty candidate set OR top score `< 0.50` → `Disposition(action=NO_MATCH, top_match=None, candidates_ranked=())`.
- [ ] Cluster conflict: top-2 distinct canonical_ids both `>= SURFACE_THRESHOLD` AND `are_clustered()` returns False → `cluster_conflict=True`, `action=QUEUE_FOR_REVIEW` (override AUTO_APPROVE if applicable). Equal scores at top → still QUEUE_FOR_REVIEW.
- [ ] Single candidate at `>= 0.90` → AUTO_APPROVE, `cluster_conflict=False`.
- [ ] Duplicate `canonical_id` across input ScoredMatches is deduped with max score before logic.
- [ ] `apply_thresholds()` does **not** call the LLM. Only routes by setting `action=LLM_FALLBACK`. A separate orchestration step (the caller) invokes `llm_assess()` when `action == LLM_FALLBACK`.
- [ ] `llm_assess()` returns a `Disposition` with `action=QUEUE_FOR_REVIEW` (NEVER `AUTO_APPROVE`) when given a `LLM_FALLBACK` disposition input, regardless of LLM's `match` / `confidence`.
- [ ] `llm_assess()` is invoked **only** when input `Disposition.action == LLM_FALLBACK`. Calling it with any other action raises `ValueError`.
- [ ] `llm_assess()` writes exactly one row to `llm_training_data` per call, with all six columns populated (call_id, tenant_id nullable OK, category_pair, redacted_prompt, prompt_sha256, llm_response_json).
- [ ] Default factory raises `LLMNotConfiguredError` when `ANTHROPIC_API_KEY` is absent (test via `monkeypatch.delenv`).
- [ ] When `MAX_LLM_CALLS_PER_RUN` is reached within a single `LLMAssessor` instance (or via a module-level counter — your choice; document in the code), the next call raises `LLMBudgetExceededError`.
- [ ] LLM response with no `tool_use` block → `LLMResponseError`.
- [ ] LLM `reasoning` containing a forbidden token → reasoning replaced with the literal string `"[redacted: PII detected in LLM output]"`, structlog warning emitted with `outcome="redacted_leak"`, training row stores the redacted reasoning (not the leaked one).
- [ ] Redaction default-deny: `redact_org()` and `redact_person()` signatures take only primitives + small allow-lists. Neither accepts `NormalizedEntity` or `raw_record` or `attributes`. Static check: their type signatures contain no `NormalizedEntity` or `dict[str, Any]` parameters.
- [ ] Org redaction emits shape-only for class codes (`Commercial.NI.Sands` → `X.Y.Z`) and project codes (`CEN-GENAI-SOW3` → `AAA-BBB-NNN`). Test asserts the raw strings do not appear in the prompt.
- [ ] Person redaction: for every fixture person entity (25 person rows in `canonical_ground_truth.json`), the redacted prompt produced for any pairing contains zero substrings drawn from `{canonical_name, all aliases, all emails, all employee_ids}`. Tested via a parametrized fixture-sweep.
- [ ] Dispatch rule: source `entity_type == "person"` OR candidate `entity_category == "person"` → `redact_person` (strictest wins).
- [ ] Tier-3 usage: write a single test that builds `Disposition` for every fixture candidate set (mock or trivially seeded ScoredMatch tuples derived from the fixture pairs) and asserts `count(LLM_FALLBACK) / count(entities) < 0.15`. Use `pytest.skip` ONLY if `count(entities) < 7` (statistical floor).
- [ ] `pytest tests/test_disposition.py tests/test_llm_fallback.py tests/test_redaction.py -x --tb=short` passes.
- [ ] `pytest tests/ -x --tb=short` passes (full repo, no regression).
- [ ] `grep -rn "anthropic.Anthropic()" tests/` produces zero hits.

---

## NON-GOALS (DO NOT BUILD)

From the brief and the hardened design — **do not add any of the
following.** Each is either out of scope by the brief or a phantom
risk explicitly dismissed in the hardened design.

- ❌ Approval queue UI (feature 11).
- ❌ Graph writes on approval / Stage 6 logic (feature 10). Do not
  write to `entity_edges` from this code. The `are_clustered` helper
  reads only.
- ❌ Self-hosted LLM (rules §11). Claude API only via injected client.
- ❌ LLM parallel assessment on all candidates (rules §11). V1 is
  fallback only, gated by `action == LLM_FALLBACK`.
- ❌ Per-tenant daily LLM cost cap. (Belt-and-suspenders only:
  `MAX_LLM_CALLS_PER_RUN = 50` per the hardened design.)
- ❌ Idempotency unique index on `llm_training_data`. Duplicates from
  re-runs are accepted.
- ❌ Concurrency / distributed locking on the training-data table.
- ❌ NER libraries (`presidio`, `scrubadub`, spaCy). Pure templated
  builder + leak-check is the V1 design.
- ❌ NFKC / homoglyph / RTL-mark normalization. Documented V2+
  limitation.
- ❌ A new `core/matching/thresholds.py` constants module. Thresholds
  live in `disposition.py` only.
- ❌ A new `core/matching/confidence.py` module. Inline constants only.
- ❌ Reaching into `core/matching/scoring.py` to harmonize
  `SignalBreakdown` with `Disposition` or to rename fields.
- ❌ A generic `MockClaudeClient` class scaffold under
  `tests/fixtures/`. Use a 30-line in-test `FakeLLMClient`.
- ❌ Structured logging of the redacted prompt body or the LLM
  reasoning body.
- ❌ Refactoring or "cleanup" of any existing module that doesn't need
  to change.
- ❌ Constructing `anthropic.Anthropic()` anywhere a test can reach it.
- ❌ Loading `.env` at import time of `disposition.py` or
  `redaction.py`. `llm_fallback.py` is the only module allowed to call
  `dotenv.load_dotenv()`, and only inside its module body gated on the
  env var being absent.

---

## EXECUTION (ONE STEP AT A TIME)

Do these in order. After each step, run the partial test command in
the brackets to verify. **Do not skip ahead.**

### Step 1 — Branch + scaffolding
- Create branch `feature/threshold-llm-fallback` from current HEAD
  (which is `feature/pairwise-scoring` at commit 51820a2).
- Create empty files (placeholders): `core/matching/disposition.py`,
  `core/matching/llm_fallback.py`, `core/matching/redaction.py`,
  `db/migrations/002_llm_training_data.sql`,
  `tests/test_disposition.py`, `tests/test_llm_fallback.py`,
  `tests/test_redaction.py`.
- Verify `git status` shows the new files. **No tests yet — nothing
  to run.**

### Step 2 — Append `Action`, `Disposition`, `LLMAssessment`, `RedactedPrompt` to `core/matching/types.py`
- Append AT END OF FILE (do not reorder existing dataclasses).
- Add a module-level section comment matching the Stage 3 comment
  block style:
  ```
  # ---------------------------------------------------------------------------
  # Stage 4 — Threshold / Cluster Conflict & Stage 5 — LLM Fallback shapes
  # ---------------------------------------------------------------------------
  ```
- Define exactly:
  - `Action = Literal["AUTO_APPROVE", "QUEUE_FOR_REVIEW", "LLM_FALLBACK", "NO_MATCH"]`
  - `RedactedPrompt(category_pair, text, forbidden_tokens)` — `forbidden_tokens` is `frozenset[str]`.
  - `LLMAssessment(call_id, match, llm_confidence, reasoning, signals_examined, prompt_sha256)` — `signals_examined` is `tuple[str, ...]`.
  - `Disposition(source_entity_id, action, top_match, candidates_ranked, cluster_conflict, llm_assessment, tenant_id)` — `top_match: Optional[ScoredMatch]`, `candidates_ranked: tuple[ScoredMatch, ...]`, `cluster_conflict: bool`, `llm_assessment: Optional[LLMAssessment]`, `tenant_id: Optional[str]`.
- All frozen.
- **[Run:** `python -c "from core.matching.types import Action, Disposition, LLMAssessment, RedactedPrompt; print('ok')"`**]**

### Step 3 — Append `are_clustered()` to `core/graph/entity_store.py`
- Append at end of file, matching the existing module-function style
  (tenant_id optional, docstring explaining V1 behavior: "returns
  True iff a `SAME_AS` edge exists between the two canonical_ids in
  either direction. V1 has no Stage 6 writes yet, so this returns
  False for every production call — shipped now so Stage 6 can
  populate without revisiting Stage 4.").
- SQL: query `entity_edges` for `relationship = 'SAME_AS'` AND
  `(source_node = ? AND target_node = ?) OR (source_node = ? AND
  target_node = ?)`, with optional `tenant_id` filter via JOIN to
  `canonical_entities`.
- Return `bool`.
- **[Run:** `python -c "from core.graph.entity_store import are_clustered; print('ok')"`**]**

### Step 4 — Write `db/migrations/002_llm_training_data.sql`
- Verbatim per hardened design §2 item 4: table + index +
  the comment "Append-only within tenant lifetime. DELETE permitted
  on tenant offboarding."
- Match the style of `001_canonical_schema.sql` (CREATE IF NOT
  EXISTS guards, two-space indent inside columns, no trailing
  whitespace).

### Step 5 — Implement `core/matching/redaction.py`
- Module docstring explaining the **default-deny security
  boundary**: redactors take only allow-listed primitives, never the
  raw `NormalizedEntity` or its `raw_record`. Future maintainers
  cannot leak via this surface because the surface refuses to receive
  the leak source.
- Define `_NAME_INVERSION_LANG = "name-inversion-detected"` and
  similar small constant strings if needed for stable prompt phrasing.
- `_shape_class_code(value: Optional[str]) -> Optional[str]` — collapses
  `"Commercial.NI.Sands"` → `"X.Y.Z"`, preserving the dot count and
  segment-count signal, but returning a literal placeholder string.
  `None` → `None`.
- `_shape_project_code(value: Optional[str]) -> Optional[str]` —
  collapses `"CEN-GENAI-SOW3"` → `"AAA-BBB-NNN"`. Letters → `A`,
  digits → `N`, dashes preserved. `None` → `None`.
- `redact_org(...)` — signature exactly as in hardened design §3.
  Returns `RedactedPrompt(category_pair=(source_category,
  candidate_category), text=..., forbidden_tokens=...)`. The text is
  a single English sentence ≤ 400 chars, mentioning category, entity
  types, the SHAPED class/project codes (not raw), the token-overlap
  count and total, and the score formatted to 2 decimals.
- `redact_person(...)` — signature exactly as in hardened design §3.
  Returns `RedactedPrompt` with text that mentions roles, name
  inversion bool, token overlap, score. Names, emails, employee_ids
  never appear because they are never received.
- `leak_check(text: str, forbidden_tokens: frozenset[str]) -> Optional[str]`
  — returns the first leaked token found (case-insensitive substring
  match on a normalized lowercase prompt), or `None` if clean.
- Pure functions: no SQLite, no env reads, no SDK imports.
- **[Run:** `pytest tests/test_redaction.py -x --tb=short` (you will
  write tests next — for now just import-check via
  `python -c "from core.matching.redaction import *; print('ok')"`**]**

### Step 6 — Write `tests/test_redaction.py`
- ~12 tests covering hardened design §6 redaction bullets.
- Use the canonical_ground_truth.json fixture for the person sweep.
- Parametrize the boundary cases for `_shape_class_code` and
  `_shape_project_code`: empty string, `None`, single segment,
  mixed digits/letters.
- Assert `leak_check` returns the first forbidden token on a leak
  and `None` on a clean prompt.
- Assert the static signatures: use `inspect.signature(redact_org)`
  and assert no parameter is named `entity` or `attributes` or
  `raw_record` and no parameter annotation contains `NormalizedEntity`
  or `dict`.
- **[Run:** `pytest tests/test_redaction.py -x --tb=short`**]**

### Step 7 — Implement `core/matching/disposition.py`
- Module docstring mirroring Stage 3 style: explains the threshold
  bands (with the `>=` rule explicit), the cluster-conflict semantics
  (in-run, plus `are_clustered` check), tenant scoping, and the
  no-DB-write contract.
- Constants: `AUTO_APPROVE_THRESHOLD = 0.90`, `SURFACE_THRESHOLD = 0.70`,
  `LLM_FALLBACK_THRESHOLD = 0.50`.
- Internal `_dedup_by_canonical(candidates: tuple[ScoredMatch, ...]) -> tuple[ScoredMatch, ...]`
  helper: keeps max score per `canonical_id`, stable sort descending by
  (score, ascending canonical_id) for deterministic tie-break.
- `apply_thresholds(source_entity_id, candidates, conn, tenant_id=None) -> Disposition`:
  1. Dedup candidates by canonical_id.
  2. If empty OR top score < `LLM_FALLBACK_THRESHOLD` → `NO_MATCH`.
  3. Else determine `base_action` from the top score's band.
  4. Compute `cluster_conflict`: top-2 distinct, both `>= SURFACE_THRESHOLD`,
     AND `not are_clustered(conn, top_1.canonical_id, top_2.canonical_id, tenant_id)`.
  5. If `cluster_conflict` and `base_action == AUTO_APPROVE` →
     downgrade `action = QUEUE_FOR_REVIEW`. (Conflict override.)
  6. Return `Disposition(... llm_assessment=None ...)`.
- **No LLM call here.** Stage 4 only routes.
- **[Run:** `pytest tests/test_disposition.py -x --tb=short` after Step 8.**]**

### Step 8 — Write `tests/test_disposition.py`
- ~25 tests covering hardened design §6 disposition bullets:
  exact-boundary 0.50 / 0.70 / 0.90; 0.7000001 lands in QUEUE_FOR_REVIEW;
  empty candidates → NO_MATCH; tie at top → QUEUE_FOR_REVIEW with
  conflict; single candidate at 0.95 → AUTO_APPROVE; top-1 at 0.95 +
  top-2 at 0.50 → AUTO_APPROVE (no conflict); top-1 at 0.85 + top-2 at
  0.80 → QUEUE_FOR_REVIEW + conflict=True; duplicate canonical_id in
  input → deduped max score; seeded SAME_AS edge → `are_clustered=True`
  → no conflict → AUTO_APPROVE allowed on top; tenant scoping:
  edge seeded for tenant A is ignored when query runs under tenant B.
- Use an in-memory SQLite with `db/schema_sqlite.sql` loaded (match
  `tests/test_scoring.py` fixture style).
- Construct `ScoredMatch` instances directly via the frozen
  dataclass; do NOT call Stage 3 to produce them (keeps Stage 4 unit
  tests decoupled).
- **[Run:** `pytest tests/test_disposition.py -x --tb=short`**]**

### Step 9 — Implement `core/matching/llm_fallback.py`
- Module docstring explains: only invoked when input
  `Disposition.action == LLM_FALLBACK`; never auto-approves; tool-call
  schema; mandatory leak-check on prompt AND outbound response;
  training row written per call; `MAX_LLM_CALLS_PER_RUN` belt-and-
  suspenders.
- Module body:
  ```python
  if os.environ.get("ANTHROPIC_API_KEY") is None:
      from dotenv import load_dotenv
      load_dotenv()
  ```
  (Once. Idempotent.)
- Constants: `MAX_LLM_CALLS_PER_RUN = 50`. `_call_counter = 0` module-
  level; reset only by a dedicated `reset_call_budget()` helper for
  tests.
- Tool spec: a single tool `submit_assessment` with input_schema
  matching `{match: bool, confidence: float in [0,1], reasoning: str,
  signals: list[str]}`. Use the anthropic SDK's `tools=[...]` and
  `tool_choice={"type": "tool", "name": "submit_assessment"}`.
- Exceptions:
  - `LLMNotConfiguredError(RuntimeError)`
  - `LLMBudgetExceededError(RuntimeError)`
  - `LLMResponseError(RuntimeError)` — also raised on
    out-of-range confidence (not in `[0.0, 1.0]`), wrong types,
    missing fields.
- `class LLMClient(Protocol):` with single method
  `assess(self, system_prompt: str, user_prompt: str, tool_spec: dict) -> dict`.
  Returns the parsed tool input dict.
- `class _AnthropicAdapter:` implements `LLMClient` against
  `anthropic.Anthropic` using model `claude-sonnet-4-6` (latest
  Sonnet per the Claude model cheat-sheet for V1 fallback). Max
  tokens = 512.
- `_default_llm_client_factory() -> LLMClient` — checks env var,
  raises `LLMNotConfiguredError` if absent, returns `_AnthropicAdapter`.
- Internal `_extract_org_redaction_inputs(entity, candidate_cid, conn,
  tenant_id)` and `_extract_person_redaction_inputs(...)` — these
  do the **default-allow → default-deny conversion**: they read the
  full `NormalizedEntity` and the candidate's `canonical_entities`/
  `system_references` rows, extract ONLY the allow-listed primitives
  (category, entity_type, role if person, structural code shapes if
  org, token overlap, score), and pass them to the redactor. The full
  entity / raw_record / attributes are NEVER passed through.
- `_build_forbidden_tokens(entity, candidate_cid, conn, tenant_id) -> frozenset[str]`
  — walks `entity.canonical_id?` (the source's normalized_name),
  `entity.aliases` if present, `entity.attributes` for email/
  employee_id-like keys (use a small fixed key allow-list:
  `{"email", "Email", "PrimaryEmail", "employee_id", "EmployeeId",
  "EmployeeNumber"}`), then queries `get_aliases(conn, candidate_cid,
  tenant_id)` and `get_canonical_name_and_category` for the
  candidate side; returns the frozenset (lowercased, non-empty,
  length ≥ 3 to avoid trivial substring noise).
- `_write_training_row(conn, call_id, tenant_id, category_pair,
  redacted_prompt, prompt_sha256, llm_response_json)` — one INSERT
  to `llm_training_data`. Own try/except → wrap exceptions in
  `LLMResponseError("training write failed")` only if needed; let
  programming errors propagate. Do NOT swallow `sqlite3.IntegrityError`.
- `llm_assess(disposition: Disposition, entity: NormalizedEntity,
  conn: sqlite3.Connection, tenant_id: Optional[str] = None,
  client: Optional[LLMClient] = None) -> Disposition`:
  1. If `disposition.action != "LLM_FALLBACK"`, raise `ValueError`.
  2. Bump `_call_counter`; if it exceeds `MAX_LLM_CALLS_PER_RUN`,
     raise `LLMBudgetExceededError`.
  3. Resolve client = `client or _default_llm_client_factory()`.
  4. Determine candidate canonical_id from `disposition.top_match`.
  5. Look up candidate's `entity_category` via
     `get_canonical_name_and_category(conn, candidate_cid, tenant_id)`.
  6. Dispatch: if `entity.entity_type == "person"` OR
     `candidate_category == "person"` → use person path; else org.
  7. Build forbidden_tokens via `_build_forbidden_tokens`.
  8. Call the appropriate redaction function.
  9. Run `leak_check(redacted_prompt.text, redacted_prompt.forbidden_tokens)`.
     If non-None → raise `LLMResponseError(f"prompt leak detected: {hit}")`.
     (Programming error — should never fire if redactor is correct.)
  10. `call_id = uuid.uuid4().hex`. `prompt_sha256 = hashlib.sha256(
      redacted_prompt.text.encode()).hexdigest()`.
  11. Call `client.assess(...)`. Validate the returned dict: keys
      `match`, `confidence`, `reasoning`, `signals`; types `bool`,
      `float` in [0,1], `str`, `list[str]`. Wrong shape → `LLMResponseError`.
  12. Outbound leak-check: `leak_check(response["reasoning"],
      redacted_prompt.forbidden_tokens)`. If non-None: replace
      `reasoning` with `"[redacted: PII detected in LLM output]"`,
      log `outcome="redacted_leak"`.
  13. Write training row with `llm_response_json = json.dumps(
      {"match": ..., "confidence": ..., "reasoning": <possibly-scrubbed>,
      "signals": ...})`.
  14. Build `LLMAssessment(...)`.
  15. Return a new `Disposition` (use `dataclasses.replace`) with
      `action="QUEUE_FOR_REVIEW"` (NEVER `AUTO_APPROVE`) and
      `llm_assessment=<the assessment>`.
- `reset_call_budget()` — for tests.
- **[Run:** `pytest tests/test_llm_fallback.py -x --tb=short` after Step 10.**]**

### Step 10 — Write `tests/test_llm_fallback.py`
- ~15 tests covering hardened design §6 llm_fallback bullets.
- Define a 30-line `FakeLLMClient` in the test module: takes a canned
  return dict in `__init__`, records calls.
- Tests:
  - `LLMNotConfiguredError` via `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)`.
  - `llm_assess` raises `ValueError` if called with non-LLM_FALLBACK disposition.
  - `llm_assess` returns QUEUE_FOR_REVIEW with FakeLLMClient returning `{match: True, confidence: 0.99, ...}` — never AUTO_APPROVE.
  - `llm_confidence == 0.99` in the returned `LLMAssessment`, **NOT** merged into `top_match.score`.
  - Training row count goes up by 1 per call; columns populated; sha256 matches.
  - Tenant scoping: `tenant_id="t_a"` writes to a row whose
    `tenant_id` column is `"t_a"`.
  - Budget: call `MAX_LLM_CALLS_PER_RUN` times; the next call raises.
  - `LLMResponseError` on FakeLLMClient returning a dict missing
    `confidence`; out-of-range `confidence=1.5`; non-bool `match`;
    non-string `reasoning`.
  - Reasoning scrub: FakeLLMClient returns reasoning containing a
    forbidden token (e.g., the source's canonical_name) → final
    `LLMAssessment.reasoning == "[redacted: PII detected in LLM output]"`,
    training row stores the redacted reasoning.
  - Tool-call schema: assert `_AnthropicAdapter` constructs the
    `submit_assessment` tool spec with the four required input_schema
    fields. Inspect the adapter's internal builder without making a
    real API call.
  - Tier-3 usage: build candidate tuples seeded from the fixture's
    44 pairs; run `apply_thresholds` for each; assert the fraction
    of LLM_FALLBACK dispositions is `< 0.15`.
- All tests use `reset_call_budget()` in a fixture so they don't bleed
  the counter across tests.
- **[Run:** `pytest tests/test_llm_fallback.py -x --tb=short`**]**

### Step 11 — Wire `tests/test_disposition.py` and `tests/test_llm_fallback.py` to the SQLite migration
- Tests must load both `db/schema_sqlite.sql` AND
  `db/migrations/002_llm_training_data.sql` into the in-memory
  connection so the training-row writes succeed. Add a small helper
  in the test module (or a pytest fixture) that does this; do NOT
  add it to `conftest.py` (no conftest currently — keep the fixture
  local).

### Step 12 — Full-suite regression
- **[Run:** `pytest tests/ -x --tb=short`**]**
- Must be green. Any pre-existing test failing now is a regression
  and must be fixed.

### Step 13 — Static safety check
- **[Run:** `grep -rn "anthropic.Anthropic()" tests/`**]** Must produce
  zero hits.
- **[Run:** `grep -rn "load_dotenv" core/matching/disposition.py core/matching/redaction.py`**]** Must produce zero hits (only `llm_fallback.py` may load dotenv).

### Step 14 — Commit
- Commit message: `"Stage 4 + 5 (Threshold + LLM Fallback) [Rocket build]"`.
- Do NOT push (Phase 6 owns the push).
- Do NOT update SHIPPED.md, RUN_LOG.md, PROMPT_LOG.md, CC-LEARNINGS.md,
  or FEATURE_QUEUE.md (Phase 6 owns those).

---

## ON SCOPE EXPANSION

If during the build you discover a real missing requirement that
forces a scope change (e.g., a Stage 3 contract gap), **stop, write
the gap to the conversation, and ask for direction.** Do not silently
expand. Do not add a "while we're here" refactor. Do not introduce
a new constants module. Do not add a `tests/fixtures/llm/` directory.

If the hardened design and the brief disagree on a point not
explicitly resolved here, the **hardened design wins** — that's
the entire purpose of Phase 1.
