"""Shared dataclasses for matcher Stages 1–3.

These shapes are imported by `core.matching.deterministic` (Stage 1),
`core.matching.blocking` (Stage 2), and (in a later feature) Stage 3
pairwise scoring. Keeping them in one module avoids each stage redefining
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
