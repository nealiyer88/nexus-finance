# Adversary-Engineer: threshold-llm-fallback

## Algorithm Review

**Threshold dispatch.** Four bands keyed off `ScoredMatch.score`
(clamped [0,1]). No algorithm choice. Boundaries: use `>=` for upper
inclusivity. Test the exact triples (0.50, 0.70, 0.90) — Stage 3
returns floats and 0.7000001 must land in the same bucket as 0.70.

**Cluster conflict detection.** Not a graph traversal. The conflict is
purely on the candidate set for ONE source entity. Given the
`tuple[ScoredMatch, ...]` already sorted descending, the conflict is:
`scored[0].score >= SURFACE` AND `scored[1].score >= SURFACE` AND the
two canonical_ids are NOT linked by a `SAME_AS` edge. V1 has no
SAME_AS edges yet, so the check reduces to "are these distinct
canonical_ids" — trivially true. The existing `_neighbors()` helper
is the wrong tool (returns edge neighbors, not cluster membership).
Add `are_clustered(conn, cid_a, cid_b, tenant_id)` returning False in
V1 by construction.

**Redaction.** Templated string building with category-pattern
preservation. For org, retain class-code regex (`X.Y.Z`) and
project-code regex (`[A-Z]+-[A-Z]+-\d+`) from raw_record; strip
everything else. For person, never pass the name — synthesize
`"person role=engineer category=psa token_overlap=2/2 score=0.64"`.
Reach for an NER library here would be wrong at 89 fixture entities.

## Performance Sanity Check

Stage 4 over <500 canonicals: each source entity has ≤50 candidates
(Stage 2 hard cap). Threshold application is O(k) per entity, conflict
detection is one comparison of the top two scores plus one SQL lookup
(`SAME_AS` edge existence) — at <500 entities, the edge table has at
most a few hundred rows and the index on `entity_edges_source` makes
this ~microseconds. Total Stage 4 walltime at V1: 500 × (50 + 1 SQL) ≈
25K ops, well under 100ms.

Stage 5 LLM cost: <15% of 89 fixture entities ≈ 14 calls. At Claude
Sonnet pricing roughly $0.003/1K input tokens, redacted prompts ~300
tokens, this is fractions of a cent per full fixture run. The
brief's "cost runaway" is mathematically impossible at fixture scale —
you'd need 10K+ calls to hit a dollar. No cost-tracking infra needed
in V1; a single structlog `info` line per call is enough.

## Dependency Risk

**`anthropic==0.39.0` — already pinned in `requirements.txt`** (line 12).
No new dep needed. Pure-Python wheel on PyPI; no compiled ext, no Apple
Silicon compilation risk. Active maintenance (Anthropic team owns it).
Default httpx transport is already a transitive dep (line 25).

**`python-dotenv==1.0.1` — already pinned** (line 24). The brief's
"`.env.example` has ANTHROPIC_API_KEY" claim is verified (I checked).
But: no code currently loads `.env` anywhere in the repo. The
`llm_fallback.py` module must own that wiring — call
`dotenv.load_dotenv()` exactly once at the module level if and only if
`ANTHROPIC_API_KEY` is not already in `os.environ`. Otherwise tests
that set the env var via monkeypatch will be silently overridden.

**No new deps required.** Reject any reach for `presidio` /
`scrubadub` / NER libraries for redaction — they would be a 100MB+
install for a templated string builder at 14 calls per run.

## Silent Failure Modes

- **`ANTHROPIC_API_KEY` missing in CI:** `anthropic.Anthropic()`
  constructs lazily but raises on first call. If a test forgets to
  inject the fake client, the failure surfaces as
  `AuthenticationError` mid-test, not as "you forgot to inject."
  Mitigation: the default factory should raise a *specific*
  `LLMNotConfiguredError` when key is absent, not propagate the SDK's
  error.

- **Redaction substring leak via aliases:** brief says strip
  `entity.name` for persons. But an entity's `aliases` table values
  (e.g., the QB `DisplayName` mirror) can contain the same name. The
  leak-check must walk `get_aliases(conn, canonical_id, tenant_id)`
  and all `system_references.external_fields` JSON, not just
  `normalized_name`.

- **`raw_record` JSON contains the email/name in nested fields** the
  redactor didn't know to scrub. Defense: the redactor should NEVER
  see `raw_record` — pass only the explicit, allow-listed fields
  (`category`, `entity_type`, normalized token-overlap count, score,
  signal_breakdown). Default-deny, not default-allow.

- **Cluster conflict double-counts when Stage 3 returns the same
  canonical twice** (deterministic Stage 1 hit plus blocking
  rediscovery). Conflict detection must dedupe by `canonical_id`
  before applying the "top two distinct" rule.

- **LLM returns `{match: true, confidence: 0.99}` and a caller
  mistakenly persists this as the entity's confidence on a future
  re-resolution.** Mitigation: emit `Disposition.action =
  QUEUE_FOR_REVIEW` regardless of LLM confidence, and store
  `llm_confidence` in a *separate* field from `score`. The brief's
  success criteria already require this; just make the dataclass
  shape enforce it (separate field, no shared accessor).

- **Training-data table on the wrong tenant_id.** The brief omits
  `tenant_id` from the training-row schema. Every write must include
  it; otherwise V2+ fine-tuning data crosses tenant boundaries.

- **`json.loads(reply.content)` on a Claude response that wrapped JSON
  in markdown fences (`” ```json\n{...}\n``` ”`).** The Anthropic SDK
  does NOT do structured-output coercion. Either request a tool-call
  with a JSON schema (cleanest) or strip code fences before parsing.

## Critique of Adversary-Design

The design advocate's refinement #1 (`RedactedPrompt` dataclass with a
`leak_check_tokens` field and runtime second-pass check) is sound and
should ship. But refinement #2 (persist training row in the same
SQLite transaction as the Disposition write) over-specifies V1.
Stage 4 does NOT write a Disposition to the DB in V1 — the brief
explicitly defers graph writes to Stage 6 (feature 10). The
disposition is an in-memory result returned to the orchestrator.
Lighter form: append the training row to a single
`llm_training_data` table on its own transaction inside
`llm_assess()`. One append-only table, four columns
(`call_id, tenant_id, redacted_prompt, llm_response_json,
created_at`), no FK to a disposition row that doesn't exist yet.

Also: the design advocate accepts "three sibling modules" without
pushback. At V1, `redaction.py` is ~80 lines of templated string
building. Folding `redact_org` and `redact_person` into
`llm_fallback.py` is acceptable IF the leak-check is its own pure
function importable in isolation. But the advocate's argument
(testability isolation) is fine — keep three files.

## Critique of Adversary-Skeptic

The skeptic doc was not yet written at the time of this analysis. The
phantom risks I would pre-emptively dismiss if they appear:

- **Concurrency / distributed locking on training-data table.** Phantom
  at V1. SQLite default isolation handles ~14 sequential append calls
  per fixture run trivially. Re-evaluate when >1000 concurrent LLM
  calls per second, which is ~6 orders of magnitude away.

- **Cost runaway / circuit breakers / rate-limit budgets.** Phantom at
  fixture scale. Math above shows fractions of a cent per run. A
  simple `MAX_LLM_CALLS_PER_RUN = 50` constant guard is sufficient.

- **Cluster conflict frequency.** On a fresh V1 DB with no prior
  resolutions and no SAME_AS edges, *any* multi-candidate above
  SURFACE triggers the conflict path by construction. This is fine
  — the path resolves to "demote lower-confidence, queue both."
  Skeptic worry that "conflicts never fire" or "always fire" both
  miss the point: the path's job is to surface contradictions to
  humans, and at fixture scale it's exercised by 2-3 ambiguous
  pairs (the Cenlar / CEN-GP shortcode cluster is a candidate).

- **Prompt versioning infrastructure.** Phantom. Store the prompt
  string verbatim in the training-data row; "versioning" is `git log`
  on the redaction module.

## Concrete Contract

```python
# core/matching/disposition.py
from dataclasses import dataclass
from typing import Literal, Optional
from core.matching.types import ScoredMatch

Action = Literal["AUTO_APPROVE", "QUEUE_FOR_REVIEW", "LLM_FALLBACK", "NO_MATCH"]

@dataclass(frozen=True)
class Disposition:
    source_entity_id: str
    action: Action
    top_match: Optional[ScoredMatch]           # None if NO_MATCH
    candidates_ranked: tuple[ScoredMatch, ...] # all above NO_MATCH, deduped by canonical_id
    cluster_conflict: bool                     # True iff top-2 distinct canonicals both ≥ SURFACE
    llm_assessment: Optional["LLMAssessment"]  # populated only after Stage 5 runs

@dataclass(frozen=True)
class LLMAssessment:
    call_id: str                # uuid4 hex, links to training-data row
    match: bool
    llm_confidence: float       # NOT merged into ScoredMatch.score
    reasoning: str
    signals_examined: tuple[str, ...]
    prompt_sha256: str          # hash of the redacted prompt for audit

# core/matching/llm_fallback.py
from typing import Protocol

class LLMClient(Protocol):
    def assess(self, redacted_prompt: str) -> dict: ...

def _default_llm_client_factory() -> LLMClient:
    import os, anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMNotConfiguredError("ANTHROPIC_API_KEY not set")
    # adapter wraps anthropic.Anthropic() to satisfy LLMClient
    return _AnthropicAdapter(anthropic.Anthropic(api_key=key))

def llm_assess(
    entity: NormalizedEntity,
    top_match: ScoredMatch,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
    client: Optional[LLMClient] = None,
) -> LLMAssessment: ...
```

Test injection: pass a `FakeLLMClient` that returns a canned dict
(`{"match": True, "confidence": 0.99, "reasoning": "...", "signals":
[]}`). Real `anthropic.Anthropic()` is never constructed in tests.

**Cluster conflict fixture:** three canonicals seeded in an in-memory
SQLite — CLIENT_A ("Cenlar FSB"), CLIENT_B ("Cenlar Federal"),
CLIENT_C ("Cenlar Holdings") — no SAME_AS edges between them. Feed
a NormalizedEntity "Cenlar" through Stage 3, assert the candidate set
has all three above SURFACE, assert `Disposition.cluster_conflict ==
True`, assert top match stays AUTO_APPROVE-eligible only if scored[1]
is below SURFACE (which it won't be), else demoted.

## Position Statement

Ship with three changes: (1) cluster-conflict logic operates purely
on the candidate set, not graph traversal — add a single
`are_clustered()` helper that returns False in V1 by construction;
(2) training-data persistence is a single append-only
`llm_training_data` table written inside `llm_assess()`, NOT bound
to a Disposition transaction that doesn't exist; (3) `LLMClient`
Protocol injection with a default factory — no test ever constructs
`anthropic.Anthropic()`. The brief is otherwise correctly scoped for
V1. Reject any addition of cost-tracking, prompt-versioning,
concurrency-control, or NER-based redaction infrastructure — all
phantom at <100 fixture-entity scale.
