"""Shared dataclasses for matcher Stages 1–3.

These shapes are imported by `core.matching.deterministic` (Stage 1),
`core.matching.blocking` (Stage 2), and `core.matching.scoring`
(Stage 3). Keeping them in one module avoids each stage redefining
overlapping result types.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

from core.matching.indices import EmbeddingIndex, NgramIndex, TokenIndex

if TYPE_CHECKING:
    from core.matching.llm_fallback import LLMClient
    from core.matching.scoring import BoostEntry


MatchKeyType = Literal["alias_exact", "email", "employee_id"]


@dataclass(frozen=True)
class DeterministicMatch:
    canonical_id: str
    confidence: float
    match_key_type: MatchKeyType


@dataclass(frozen=True)
class CandidateEntity:
    canonical_id: str
    blocking_signals: tuple[str, ...]


@dataclass(frozen=True)
class CandidateSet:
    source_entity_id: str
    candidates: tuple[CandidateEntity, ...]


# ---------------------------------------------------------------------------
# Stage 3 — Pairwise Scoring result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalBreakdown:
    """Per-signal values used to compute a ScoredMatch.

    RapidFuzz signals are on the 0..100 scale; n-gram Jaccard is on
    0..1. Boolean fields record whether the additive bonuses fired.
    All five weighted signals are always present (even when zero) so
    debug output can compare a scorer pass to its weight profile.
    `fasttext_cosine` is 0.0 when the model file is absent.

    `fasttext_available` distinguishes "cosine is genuinely 0.0" from
    "no embedding could be computed" (model absent / OOV-empty input).
    Stage 3 renormalizes the weight budget over available signals, so
    this flag decides whether `fasttext_cosine`'s weight participates
    in the sum-to-1.0 budget for the pair.
    """

    token_sort_ratio: float
    token_set_ratio: float
    partial_ratio: float
    jaro_winkler: float
    ngram_jaccard: float
    alias_boost_fired: bool
    abbreviation_bonus_fired: bool
    fasttext_cosine: float = 0.0
    fasttext_available: bool = False
    b_boosts: tuple[BoostEntry, ...] = ()


@dataclass(frozen=True)
class GraphEvidence:
    """Shared-neighbor evidence observed for the pair — a REPORT, not a
    score contribution. Since the 8a Signal Set B retrofit, the applied
    graph boosts live in `SignalBreakdown.b_boosts` (band-gated,
    +0.20-capped); the `*_bonus` fields here are the legacy-formula
    values derived from the same counts and may legitimately be nonzero
    on pairs whose score received no boost (base outside the B band).

    On a fresh DB with no `entity_edges` rows (V1 default; Stage 6
    owns writes), all four fields are 0 / 0.0. Tests seed edges
    manually to exercise the bonus paths.
    """

    shared_person_count: int
    shared_person_bonus: float
    neighborhood_overlap_count: int
    neighborhood_overlap_bonus: float


@dataclass(frozen=True)
class ScoredMatch:
    """One scored (entity, candidate) pair.

    `score` is clamped to [0.0, 1.0]. `weight_profile_id` carries the
    `WeightConfig.profile_id` that produced the score; useful when a
    weight-tuning change shifts a ground-truth pair out of band.
    """

    canonical_id: str
    score: float
    signal_breakdown: SignalBreakdown
    graph_evidence: GraphEvidence
    category_pair: tuple[str, str]
    weight_profile_id: str


# ---------------------------------------------------------------------------
# Stage 4 — Threshold / Cluster Conflict & Stage 5 — LLM Fallback shapes
# ---------------------------------------------------------------------------


Action = Literal["AUTO_APPROVE", "QUEUE_FOR_REVIEW", "LLM_FALLBACK", "NO_MATCH"]


@dataclass(frozen=True)
class RedactedPrompt:
    """Output of `redact_org` / `redact_person`.

    `text` is the literal string handed to the LLM. `forbidden_tokens` is
    the set of lowercased substrings that MUST NOT appear in `text` (or in
    the LLM's reasoning output) — Stage 5 runs `leak_check` against this
    set both on send and on receive.
    """

    category_pair: tuple[str, str]
    text: str
    forbidden_tokens: frozenset[str]


@dataclass(frozen=True)
class LLMAssessment:
    """Stage 5 output. Always carried by a `Disposition.llm_assessment`
    field; never merged into `ScoredMatch.score`.

    `llm_confidence` is the model's own [0,1] confidence in the proposed
    match. It is informational only — the disposition action is always
    QUEUE_FOR_REVIEW when an LLMAssessment is present.
    """

    call_id: str
    match: bool
    llm_confidence: float
    reasoning: str
    signals_examined: tuple[str, ...]
    prompt_sha256: str


@dataclass(frozen=True)
class Disposition:
    """Stage 4 result. In-memory only — Stage 4 does not write to SQLite.

    - `action` is the band derived from `top_match.score`, with two
      overrides: downgrade to QUEUE_FOR_REVIEW when `cluster_conflict`
      is True, and upgrade to QUEUE_FOR_REVIEW when
      `abbreviation_rescue` is True (SC-5 amended 2026-07-05).
    - `top_match` is None iff `action == NO_MATCH` (no candidate ≥ 0.50).
    - `candidates_ranked` is the deduped-by-canonical_id input tuple,
      sorted descending by (score, ascending canonical_id).
    - `cluster_conflict` is True iff the top-2 distinct canonicals both
      score ≥ SURFACE_THRESHOLD AND are not linked by a SAME_AS edge.
    - `abbreviation_rescue` is True iff the action was upgraded from
      LLM_FALLBACK because the top match's PSA↔Accounting abbreviation
      heuristic fired — recorded so the review queue (feature 11) can
      show WHY a sub-SURFACE item is queued without re-deriving bands.
    - `llm_assessment` is populated only after Stage 5 runs.
    - `tenant_id` is propagated from the orchestrator for downstream
      tenant-scoped writes (e.g., the llm_training_data row).
    """

    source_entity_id: str
    action: "Action"
    top_match: Optional[ScoredMatch]
    candidates_ranked: tuple[ScoredMatch, ...]
    cluster_conflict: bool
    llm_assessment: Optional[LLMAssessment]
    tenant_id: Optional[str]
    abbreviation_rescue: bool = False


# ---------------------------------------------------------------------------
# Feature 12 — Matcher orchestrator shapes
# ---------------------------------------------------------------------------


MatchType = Literal["deterministic", "scored", "llm", "new", "none"]


@dataclass
class MatchContext:
    """Mutable run context threaded through `core.matching.engine.match()`.

    Carries exactly what the shipped Stage 1-5 signatures demand: the
    open connection, the tenant scope, the two blocking indices always
    required by Stage 2, the optional fastText ANN index (Stage 2c —
    absent-model-safe), and an optional injected Stage 5 `LLMClient`
    (tests pass a fake; production leaves this `None` and Stage 5 falls
    back to its own default factory).

    NOT frozen: the orchestrator's index-rebuild policy (module-level
    staleness bit in `core.graph.resolution`) replaces `token_index` /
    `ngram_index` / `embedding_index` in place on this same instance
    rather than handing back a new `MatchContext`.
    """

    conn: sqlite3.Connection
    token_index: TokenIndex
    ngram_index: NgramIndex
    tenant_id: Optional[str] = None
    embedding_index: Optional[EmbeddingIndex] = None
    llm_client: Optional["LLMClient"] = None


@dataclass(frozen=True)
class MatchResult:
    """Outcome of one `match()` call. Every field has a shipped producer —
    see `core.matching.engine` for the per-path construction. There is
    NO `audit_entry` field: no audit table or writer exists anywhere in
    the tree (feature 10a, BLOCKED, owns that surface).

    `match_type` distinguishes the code path taken, independent of the
    `action` that decided the write:
      - "deterministic" — Stage 1 exact-anchor hit (alias/email/employee_id).
      - "new"            — Stage 2 blocking returned zero candidates; a
                            canonical was created without ever reaching
                            Stage 3/4 scoring.
      - "scored"         — Stage 3/4 ran and banded the outcome directly
                            (AUTO_APPROVE or QUEUE_FOR_REVIEW) without a
                            Stage 5 LLM call.
      - "llm"            — Stage 4 routed to LLM_FALLBACK; Stage 5 ran
                            (or was caught unavailable/budget-exhausted)
                            and the disposition was converted to
                            QUEUE_FOR_REVIEW.
      - "none"           — Stage 4 scored candidates but none cleared
                            even the LLM_FALLBACK band (NO_MATCH); a new
                            canonical was created from the rejected set.
    """

    source_entity_id: str
    canonical_id: Optional[str]
    confidence: float
    match_type: MatchType
    action: "Action"
    signal_breakdown: Optional[SignalBreakdown]
    disposition: Optional[Disposition]
    reasoning_trace: str
