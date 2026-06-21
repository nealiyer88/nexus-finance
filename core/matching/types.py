"""Shared dataclasses for matcher Stages 1–3.

These shapes are imported by `core.matching.deterministic` (Stage 1),
`core.matching.blocking` (Stage 2), and `core.matching.scoring`
(Stage 3). Keeping them in one module avoids each stage redefining
overlapping result types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


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
class SignalBoost:
    signal_id: str
    raw: float
    applied: float


@dataclass(frozen=True)
class SignalBreakdown:
    """Per-signal values used to compute a ScoredMatch.

    RapidFuzz signals are on the 0..100 scale; n-gram Jaccard is on
    0..1. Boolean fields record whether the additive bonuses fired.
    All five weighted signals are always present (even when zero) so
    debug output can compare a scorer pass to its weight profile.
    """

    token_sort_ratio: float
    token_set_ratio: float
    partial_ratio: float
    jaro_winkler: float
    ngram_jaccard: float
    alias_boost_fired: bool
    abbreviation_bonus_fired: bool
    fasttext_cosine: Optional[float] = None
    b_signal_boosts: tuple[SignalBoost, ...] = ()


@dataclass(frozen=True)
class GraphEvidence:
    """Stage 3's graph-corroborated additive bonuses.

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

    - `action` is the band derived from `top_match.score`, with override
      to QUEUE_FOR_REVIEW when `cluster_conflict` is True.
    - `top_match` is None iff `action == NO_MATCH` (no candidate ≥ 0.50).
    - `candidates_ranked` is the deduped-by-canonical_id input tuple,
      sorted descending by (score, ascending canonical_id).
    - `cluster_conflict` is True iff the top-2 distinct canonicals both
      score ≥ SURFACE_THRESHOLD AND are not linked by a SAME_AS edge.
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
