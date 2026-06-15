# Hardened Design: threshold-llm-fallback (Stages 4 + 5)

Reconciliation of the three adversary reports for
`features/pipeline/threshold-llm-fallback.md`. Drives Phase 2 prompt-gen
and Phase 3 build. Authoritative when it disagrees with the original brief.

Source documents:
- features/_adversaries/threshold-llm-fallback.design-advocate.md
- features/_adversaries/threshold-llm-fallback.skeptic.md
- features/_adversaries/threshold-llm-fallback.engineer.md

---

## 1. Disagreement Decisions

### D1. Bind training-data write to a "Disposition transaction"?
- Advocate: YES (refinement #2).
- Engineer: NO — Stage 4 doesn't persist a Disposition in V1.
- **Decision: ENGINEER WINS.** Stage 4 returns an in-memory `Disposition`; nothing is written to SQLite by `apply_thresholds`. The training-data row is appended inside `llm_assess()` on its own transaction. The advocate's "bind to disposition txn" assumes a write target that doesn't exist in V1 (graph writes are feature 10 / Stage 6).

### D2. Cluster conflict — graph traversal or in-run candidate set?
- Advocate: use `_neighbors` / `count_shared_graph_neighbors`.
- Engineer + Skeptic: in-run candidate-set check; graph is empty in V1.
- **Decision: ENGINEER + SKEPTIC WIN.** `_neighbors` returns edge neighbors, not cluster membership — wrong tool. In V1, "cluster conflict" means: for a single source entity, the top-2 distinct candidate canonical_ids both score `>= SURFACE` and are not already linked by a `SAME_AS` edge. Add a thin `are_clustered(conn, cid_a, cid_b, tenant_id) -> bool` helper to `entity_store.py` that queries `entity_edges` for a `SAME_AS` relationship in either direction; it will return `False` for every V1 fixture call (no edges seeded) — that is correct behavior, not a bug.

### D3. Redaction depth (NER/Unicode normalization vs templated allow-list)?
- Skeptic: recursive scrub, NFKC normalization, homoglyph detection.
- Engineer: default-deny templated builder; redactor never sees `raw_record`.
- Advocate: `RedactedPrompt` dataclass with `leak_check_tokens` + runtime second-pass guard.
- **Decision: ENGINEER + ADVOCATE WIN over SKEPTIC.** Default-deny architecture: redaction functions take **only** an explicit allow-list of fields (entity_type, category, token-overlap counts, structural patterns, score). They never receive `raw_record` or the full `NormalizedEntity`. Layered on top: a runtime leak-check that compares the produced prompt string against a `forbidden_tokens` set (the source/candidate canonical_name, every alias from `entity_aliases`, every `external_id`, plus emails / employee_ids extracted from `system_references.external_fields`). NFKC normalization and homoglyph detection are V2+; documented as a known limitation.

### D4. Cost runaway / per-tenant daily cap / circuit breakers?
- Skeptic: BLOCKING.
- Engineer: PHANTOM at V1 (~14 calls per fixture run ≈ fractions of a cent).
- **Decision: ENGINEER WINS.** No per-tenant cap, no daily budget, no circuit breaker. Single belt-and-suspenders constant `MAX_LLM_CALLS_PER_RUN = 50`; exceeding raises `LLMBudgetExceededError`. Re-evaluate when paying customers + production traffic land.

### D5. Concurrency / idempotency unique index?
- Skeptic: HIGH (`(tenant_id, entity_signature, candidate_canonical_id, prompt_sha256)` unique).
- Engineer: PHANTOM at V1 (sequential SQLite, fixture pipeline).
- **Decision: ENGINEER WINS.** No idempotency dedupe. Each `llm_assess` call writes a new training row; duplicates from re-runs are accepted. `call_id` (uuid4 hex) is for audit linkage only, not dedupe.

### D6. Threshold band inclusivity (Skeptic BLOCKER).
- **Decision:** Single rule, no ambiguity:
  - `score >= 0.90` → AUTO_APPROVE
  - `0.70 <= score < 0.90` → QUEUE_FOR_REVIEW
  - `0.50 <= score < 0.70` → LLM_FALLBACK
  - `score < 0.50` → NO_MATCH
  - Empty candidate set OR `top_match.score < 0.50` → NO_MATCH

### D7. `tenant_id` on every persisted row.
- **Decision:** REQUIRED. Every row in `llm_training_data` has a non-nullable `tenant_id` column (V1 fixtures may pass `None` → DB stores `NULL`, accepted, but the field exists). `Disposition` is in-memory only and carries an optional `tenant_id` field for symmetry.

### D8. Org-vs-person redaction dispatch.
- **Decision:** Dispatch is on the **canonical entity_category**, not on `NormalizedEntity.entity_type` directly. Rule: look up the candidate's `entity_category` from `canonical_entities` (the existing `get_canonical_name_and_category` already returns it as `category`). If `entity_category == "person"`, OR the source `NormalizedEntity.entity_type == "person"`, use `redact_person`. Otherwise use `redact_org`. The strictest applies — if either side is a person, person-grade redaction kicks in.

### D9. Empty / None candidate set.
- **Decision:** `Disposition(action=NO_MATCH, top_match=None, candidates_ranked=(), cluster_conflict=False, llm_assessment=None)`.

### D10. Tie-break for equal scores in cluster conflict demotion.
- **Decision:** When `scored[0].score == scored[1].score` and both `>= SURFACE`, both are flagged with `cluster_conflict=True` and both candidates remain in `candidates_ranked` (sorted ascending by `canonical_id` as the deterministic secondary key, matching Stage 3's existing ordering convention). The `action` becomes `QUEUE_FOR_REVIEW` regardless of band — the conflict overrides AUTO_APPROVE.

### D11. Class-code initials leak in org redaction.
- Skeptic: BLOCKING.
- Engineer: allow-list approach — pass only structural regex pattern, not raw value.
- **Decision: ENGINEER WINS.** Org redaction never emits the raw class code or project code value. Instead, the redactor extracts the **shape only** (e.g., `[A-Z]+\.[A-Z]+\.[A-Z]+` becomes the literal string `"X.Y.Z"` in the prompt; `CEN-GENAI-SOW3` becomes `"AAA-BBB-NNN"`). The original strings never reach the LLM. The leak-check verifies this.

### D12. LLM `reasoning` output re-leaking PII.
- Skeptic: HIGH — outbound scrub required.
- Engineer: silent.
- **Decision: SKEPTIC WINS.** The same `forbidden_tokens` leak-check runs against the LLM's `reasoning` field before it is stored to `llm_training_data` or returned in `LLMAssessment`. If a forbidden token is detected, the reasoning is replaced with `"[redacted: PII detected in LLM output]"` and a `structlog.warning` is emitted. The decision still routes to `QUEUE_FOR_REVIEW`; the scrubbed reasoning is what reviewers see.

### D13. LLM JSON output: tool-call schema vs string parsing.
- **Decision: tool-call schema.** Use Anthropic's tool-use API with a single tool `submit_assessment(match: bool, confidence: float, reasoning: str, signals: list[str])`. Avoids JSON-in-markdown brittleness. Reject any response without a tool_use block.

### D14. GDPR retention vs append-only audit rule (Skeptic BLOCKING).
- **Decision:** Rules §10 append-only applies to the **audit log**, which is a separate concept (not built in V1). `llm_training_data` is a distinct table; documented as append-only **within a tenant's lifetime**, with `DELETE FROM llm_training_data WHERE tenant_id = ?` permitted at customer offboarding. This carve-out is documented in the table-creation migration as a SQL comment. No edit to rules §10 required.

### D15. Three sibling modules vs folding redaction into llm_fallback.
- **Decision:** Three separate files (`disposition.py`, `llm_fallback.py`, `redaction.py`). Mirrors `deterministic.py` / `blocking.py` / `scoring.py` / `weights.py` already shipped. `redaction.py` must be importable without touching the LLM client (the leak-check is a pure function).

### D16. `prompt_sha256` + `call_id` for audit.
- **Decision:** Both fields present on every `LLMAssessment` and every `llm_training_data` row. `call_id` is `uuid4().hex`; `prompt_sha256` is `hashlib.sha256(redacted_prompt.encode()).hexdigest()`. Neither is used for idempotency dedupe in V1.

---

## 2. Scope Adjustments

### Added (beyond brief)
1. **`LLMClient` Protocol with injected factory** in `llm_fallback.py`. Default factory constructs an `_AnthropicAdapter` wrapping `anthropic.Anthropic(api_key=...)`. Tests pass a `FakeLLMClient` returning a canned dict; **no test ever constructs `anthropic.Anthropic()`**.
2. **`LLMNotConfiguredError`** raised by the default factory when `ANTHROPIC_API_KEY` is absent. Surfaces "missing key" instead of the SDK's `AuthenticationError` at first call.
3. **`LLMBudgetExceededError`** raised when `MAX_LLM_CALLS_PER_RUN = 50` is hit.
4. **`db/migrations/002_llm_training_data.sql`** creating the table:
   ```sql
   CREATE TABLE IF NOT EXISTS llm_training_data (
       call_id           TEXT PRIMARY KEY,
       tenant_id         TEXT,             -- NULL OK at V1 single-tenant
       category_pair     TEXT NOT NULL,    -- e.g. "psa:accounting"
       redacted_prompt   TEXT NOT NULL,
       prompt_sha256     TEXT NOT NULL,
       llm_response_json TEXT NOT NULL,    -- match / confidence / reasoning / signals
       created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
   );
   CREATE INDEX IF NOT EXISTS llm_training_data_tenant ON llm_training_data (tenant_id);
   -- Append-only within tenant lifetime. DELETE permitted on tenant offboarding.
   ```
5. **`RedactedPrompt` frozen dataclass** in `redaction.py`:
   ```python
   @dataclass(frozen=True)
   class RedactedPrompt:
       category_pair: tuple[str, str]
       text: str
       forbidden_tokens: frozenset[str]  # what the leak-check guards against
   ```
   `llm_assess` runs `leak_check(prompt.text, prompt.forbidden_tokens)` before send; raises `RedactionLeakError` if any forbidden token is a substring of the prompt.
6. **`are_clustered(conn, cid_a, cid_b, tenant_id) -> bool`** helper added to `core/graph/entity_store.py`. Returns True iff a `SAME_AS` edge exists between the two canonical_ids in either direction. Always False in V1 (no SAME_AS writes yet); shipped now so Stage 6 can populate without revisiting Stage 4.
7. **`load_dotenv()` wiring** in `llm_fallback.py` module body, gated on `os.environ.get("ANTHROPIC_API_KEY") is None` (idempotent; harmless if already loaded by the harness or tests).

### Removed (scope cut from brief)
- Daily / per-tenant cost cap. (Phantom at V1 — math: <100 calls per fixture run.)
- Idempotency unique index on `llm_training_data`. (Phantom at V1 sequential SQLite.)
- Recursive `NormalizedEntity.attributes` scrubbing — REPLACED by default-deny: the redactor never receives the full entity, only allow-listed fields.
- NFKC / homoglyph / RTL-mark handling. (V2+, documented limitation.)
- Approval-queue storage table. (Brief out-of-scopes the UI; storage is feature 10/11.)
- Reaching into `core/matching/scoring.py` to harmonize SignalBreakdown shapes. (Skeptic scope-creep flag.)
- Generic `MockClaudeClient` class — tests use a 30-line `FakeLLMClient` defined in the test module.
- New `core/matching/thresholds.py` constants module. Thresholds live as module constants in `disposition.py` (the only consumer in V1; rules §5 is the spec.)
- Structured logging of the redacted prompt content itself. Log only `call_id`, `prompt_sha256`, `category_pair`, response success/failure. Never log prompt text.
- Cluster conflict via `_neighbors` graph traversal. Replaced with in-run candidate-set logic + `are_clustered` helper.

---

## 3. Implementation Constraints

- **`anthropic==0.39.0`** already pinned in `requirements.txt` (line 12). **`python-dotenv==1.0.1`** already pinned (line 24). **No new deps.**
- **Tool-use API** required for structured output. Build the prompt as a single Claude message + a `submit_assessment` tool with JSON schema.
- **Default factory must NOT construct the SDK client at import time.** Construction happens inside `_default_llm_client_factory()` on first `llm_assess` call.
- **`redact_org` and `redact_person` signatures** take only primitives + small allow-lists; never `NormalizedEntity` or `raw_record`:
  ```python
  def redact_org(
      source_category: str, candidate_category: str,
      source_entity_type: str, candidate_entity_type: str,
      class_code_shape: Optional[str],          # e.g. "X.Y.Z" or None
      project_code_shape: Optional[str],        # e.g. "AAA-BBB-NNN" or None
      token_overlap_count: int, token_total: int,
      score: float,
      forbidden_tokens: frozenset[str],
  ) -> RedactedPrompt: ...

  def redact_person(
      source_category: str, candidate_category: str,
      source_role: Optional[str], candidate_role: Optional[str],
      name_inversion_detected: bool,
      token_overlap_count: int, token_total: int,
      score: float,
      forbidden_tokens: frozenset[str],
  ) -> RedactedPrompt: ...
  ```
  Caller (in `llm_fallback.py`) is responsible for extracting the structural shapes from the raw values before invoking the redactor. This locks the security boundary: the redactor literally cannot leak fields it never receives.
- **Tenant scoping:** `apply_thresholds` accepts `tenant_id: Optional[str] = None` and threads it through to `are_clustered` and to the training row. Matches the existing pattern in `entity_store.py`.
- **`Disposition` and `LLMAssessment` are `@dataclass(frozen=True)`** to match `ScoredMatch` / `CandidateSet` style in `core/matching/types.py`.
- **`llm_confidence` is a separate field** from `ScoredMatch.score` — never merged. A future caller cannot mistake the LLM's verdict for the canonical confidence.
- **No edit to `core/matching/scoring.py`, `core/matching/weights.py`, `core/matching/blocking.py`, `core/matching/deterministic.py`, or `connectors/*`.** Stage 4 reads `ScoredMatch` as-is.
- **`apply_thresholds()` signature:**
  ```python
  def apply_thresholds(
      source_entity_id: str,
      candidates: tuple[ScoredMatch, ...],
      conn: sqlite3.Connection,
      tenant_id: Optional[str] = None,
  ) -> Disposition: ...
  ```
  Caller is expected to pass the candidates sorted descending by score; the function dedupes by `canonical_id` (keeping max score) before applying logic, to absorb Stage-1-rediscovery cases the engineer flagged.

---

## 4. Real Risks (must address)

1. **Free-form attribute leak.** Mitigated by default-deny redactor signature: the redactor cannot leak fields it never receives. The leak-check is the runtime second-pass guard.
2. **Class-code initials leak.** Mitigated by shape-only emission (`X.Y.Z`, not `Commercial.NI.Sands`).
3. **LLM `reasoning` output PII re-leak.** Mitigated by outbound leak-check on `reasoning` before persistence + return.
4. **`tenant_id` cross-tenancy in training data.** Mitigated by mandatory column + (future) `DELETE WHERE tenant_id = ?` carve-out for GDPR.
5. **Threshold boundary ambiguity at exactly 0.50 / 0.70 / 0.90.** Mitigated by single-rule definition (D6) + parametrized boundary tests.
6. **Cluster conflict tie-break nondeterminism.** Mitigated by ascending-canonical_id secondary sort + override-to-QUEUE_FOR_REVIEW rule (D10).
7. **`anthropic.Anthropic()` constructed in tests.** Mitigated by `LLMClient` Protocol + `FakeLLMClient` in test modules.
8. **Missing `ANTHROPIC_API_KEY` in CI:** mitigated by `LLMNotConfiguredError` raised by the default factory.

## 5. Phantom Risks (dismissed)

- Distributed locking / concurrency on training-data writes (V1 sequential).
- Cost runaway beyond `MAX_LLM_CALLS_PER_RUN = 50` belt-and-suspenders.
- Prompt versioning infrastructure (`git log` on `redaction.py` is the version history).
- NER libraries (`presidio`, `scrubadub`) — 100MB+ deps for a templated string builder.
- Unicode homoglyph attack at V1 fixture scale (clean ASCII fixtures; V2+ when real customer data lands).
- Refactoring `SignalBreakdown` into `Disposition` (skeptic-flagged scope creep).

---

## 6. Test Cases the Skeptic / Engineer Identified that the Brief Missed

Add to `tests/test_disposition.py`:
- Parametrized boundary tests at score == 0.50, 0.70, 0.90 (exact equality, including 0.7000001).
- Empty candidate tuple → NO_MATCH disposition, top_match is None.
- Two candidates with identical scores at 0.95 → cluster_conflict=True, action=QUEUE_FOR_REVIEW.
- Single candidate at 0.95 with no contention → AUTO_APPROVE.
- Top-1 at 0.95, top-2 at 0.50 → AUTO_APPROVE (no conflict; second is below SURFACE).
- Top-1 at 0.85, top-2 at 0.80 → both in QUEUE_FOR_REVIEW band, cluster_conflict=True.
- Duplicate `canonical_id` across input ScoredMatches → dedup by canonical_id with max score.
- `are_clustered` returns True on a seeded `SAME_AS` edge → top-2 are clustered → cluster_conflict=False, AUTO_APPROVE allowed on top.
- `tenant_id` filter: seeded SAME_AS edge under tenant A is ignored when query runs under tenant B.

Add to `tests/test_llm_fallback.py`:
- Action=LLM_FALLBACK input dispatches `llm_assess`; AUTO_APPROVE input does not.
- `FakeLLMClient` returns `{match: True, confidence: 0.99}` → final disposition action is **QUEUE_FOR_REVIEW**, NOT AUTO_APPROVE, with `llm_assessment` populated and `llm_confidence == 0.99`.
- Training row written for every `llm_assess` call: 1 row, correct tenant_id, correct category_pair, sha256 matches prompt.
- `ANTHROPIC_API_KEY` missing → default factory raises `LLMNotConfiguredError` (test via `monkeypatch.delenv`).
- `MAX_LLM_CALLS_PER_RUN` exceeded → raises `LLMBudgetExceededError`.
- LLM response with no tool_use block → raises `LLMResponseError`.
- LLM `reasoning` containing a forbidden token → reasoning replaced with `"[redacted: PII detected in LLM output]"`, structlog warning emitted, training row stores the redacted reasoning.
- Tool-call schema: assert `submit_assessment` tool spec carries the four required fields with correct types.

Add to `tests/test_llm_fallback.py` (redaction half) — or split into `tests/test_redaction.py` if preferred:
- Person redaction: forbidden_tokens includes source name, all source aliases, all candidate aliases, every email and employee_id from system_references. Assert `leak_check(prompt.text, forbidden_tokens)` passes on a clean prompt and fails when name is injected.
- Org redaction: raw class code `"Commercial.NI.Sands"` does NOT appear in prompt — only `"X.Y.Z"`. Same for project code `"CEN-GENAI-SOW3"` → `"AAA-BBB-NNN"`.
- Org redaction with no class code / no project code → fields omitted from prompt template.
- Loop over all fixture person entities (from canonical_ground_truth.json): assert no person's `canonical_name` substring appears in the redacted prompt produced for any pairing.
- Dispatch rule: source NormalizedEntity with entity_type='person' paired against a candidate with entity_category='organization' → person redaction applies (the strictest wins).

---

## 7. Verification Plan

- `pytest tests/test_disposition.py tests/test_llm_fallback.py -x --tb=short` green.
- Full repo `pytest tests/ -x --tb=short` green (no regression from 234).
- No real Claude API calls in CI: `grep -rn "anthropic.Anthropic()" tests/` returns zero hits.
- `grep -rn "ANTHROPIC_API_KEY" tests/` returns only `monkeypatch.delenv` and `monkeypatch.setenv` references.
- `python -c "from core.matching.disposition import apply_thresholds; from core.matching.llm_fallback import llm_assess; from core.matching.redaction import redact_org, redact_person"` succeeds.
- Tier-3 usage check: run apply_thresholds across the 89-entity fixture; count `LLM_FALLBACK` actions / total source entities; assert `< 0.15`.

---

## 8. Position Statement

Ship with the 16 disagreement decisions above resolved, the seven additions, the eight removals, the eight constraints, and the new test cases. The brief's V1 thesis (Tier-3 only, never auto-approve, mandatory redaction, training-data from Day 1) is preserved exactly. The hardening is concentrated in the security boundary (default-deny redactor signature + leak-check on prompt and reasoning) and in killing ambiguity (threshold inclusivity, tie-break, dispatch rule, empty candidate handling). Reject any reach for cost-tracking, idempotency dedupe, NER-based redaction, or graph-traversal cluster detection — all phantom or wrong-tool at V1 scale.
