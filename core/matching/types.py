"""Shared dataclasses for matcher Stages 1–3.

These shapes are imported by `core.matching.deterministic` (Stage 1),
`core.matching.blocking` (Stage 2), and `core.matching.scoring`
(Stage 3). Keeping them in one module avoids each stage redefining
overlapping result types.
"""

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
    """

    token_sort_ratio: float
    token_set_ratio: float
    partial_ratio: float
    jaro_winkler: float
    ngram_jaccard: float
    alias_boost_fired: bool
    abbreviation_bonus_fired: bool


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
