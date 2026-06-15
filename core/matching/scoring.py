"""Pipeline Stage 3: Pairwise Scoring.

Scores `(NormalizedEntity, candidate canonical)` pairs surfaced by Stage 2
(Blocking) into a single `ScoredMatch`. Three signal sources combine into
a clamped [0.0, 1.0] score:

    A.  Five string-metric signals (RapidFuzz token_sort / token_set /
        partial / Jaro-Winkler + n-gram Jaccard with sentinel padding
        matching the indexer at `core/matching/indices.py`). All five
        are weighted via `WeightConfig` and contribute to the weighted
        sum.

    B.  Two additive bonuses gated by heuristics:
            - `alias_boost` fires when the entity name scores >85 against
              any candidate alias (max of token_set_ratio and
              Jaro-Winkler, RapidFuzz scale). Single application per
              pair; aliases are deduped against the candidate's own
              canonical_name (already in signal set A).
            - `abbreviation_bonus` fires only for the PSA↔Accounting
              category pair when one side is ≤4 chars and the other side
              has a matching prefix-token or first-letter initialism.

    C.  Two additive graph-corroborated bonuses:
            - shared person neighbors (+0.05 each, capped at 0.10)
            - shared graph neighbors of any category (+0.025 each,
              capped at 0.10)
        Both return 0 evidence on a fresh DB with no `entity_edges`
        rows (V1 default; Stage 6 will populate edges).

Final formula:

    score_raw = weighted_sum(signal_set_A)
              + (alias_boost if alias hit)
              + (abbreviation_bonus if heuristic hit)
              + shared_person_bonus
              + neighborhood_overlap_bonus
    score     = clamp(score_raw, 0.0, 1.0)

This module is the only matcher module that imports `rapidfuzz`. Stages
1, 2, and 0 do not (enforced by
`tests/test_blocking.py::test_no_rapidfuzz_in_matching_modules`).

This module does NOT mutate the graph store, does NOT call LLMs, does
NOT apply thresholds / dispositions (Stage 4), and does NOT consult
Stage 1's deterministic anchors directly.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from connectors.base import NormalizedEntity
from core.graph.entity_store import (
    count_shared_graph_neighbors,
    count_shared_person_neighbors,
    get_aliases,
    get_canonical_name_and_category,
)
from core.matching.types import (
    CandidateSet,
    GraphEvidence,
    ScoredMatch,
    SignalBreakdown,
)
from core.matching.weights import WeightConfig, get_weights


NGRAM_N: int = 3
NGRAM_PAD_LEFT: str = "^"
NGRAM_PAD_RIGHT: str = "$"

ALIAS_BOOST_THRESHOLD: float = 85.0  # RapidFuzz scale
SHORTCODE_MAX_LEN: int = 4

PER_SHARED_PERSON_BONUS: float = 0.05
MAX_SHARED_PERSON_BONUS: float = 0.10
PER_NEIGHBORHOOD_NODE_BONUS: float = 0.025
MAX_NEIGHBORHOOD_BONUS: float = 0.10


_ZERO_BREAKDOWN: SignalBreakdown = SignalBreakdown(
    token_sort_ratio=0.0,
    token_set_ratio=0.0,
    partial_ratio=0.0,
    jaro_winkler=0.0,
    ngram_jaccard=0.0,
    alias_boost_fired=False,
    abbreviation_bonus_fired=False,
)

_ZERO_EVIDENCE: GraphEvidence = GraphEvidence(
    shared_person_count=0,
    shared_person_bonus=0.0,
    neighborhood_overlap_count=0,
    neighborhood_overlap_bonus=0.0,
)


def _trigrams(s: str) -> set[str]:
    if not s:
        return set()
    padded = NGRAM_PAD_LEFT + s + NGRAM_PAD_RIGHT
    n = NGRAM_N
    return {padded[i : i + n] for i in range(len(padded) - n + 1)}


def ngram_jaccard(a: str, b: str) -> float:
    """Character-trigram Jaccard similarity with sentinel padding.

    Returns 0.0 when either input is empty (after strip) OR shorter
    than `NGRAM_N` characters (unpadded). The floor on sub-trigram
    inputs avoids spurious 1.0 scores from degenerate padded sets
    (e.g. `"a"` vs `"a"` both padded to `^a$` share 2/2 trigrams).
    """
    a = a.strip()
    b = b.strip()
    if not a or not b:
        return 0.0
    if len(a) < NGRAM_N or len(b) < NGRAM_N:
        return 0.0
    grams_a = _trigrams(a)
    grams_b = _trigrams(b)
    union = grams_a | grams_b
    if not union:
        return 0.0
    return len(grams_a & grams_b) / len(union)


def _check_psa_abbreviation(
    entity_name: str,
    entity_source: str,
    candidate_name: str,
    candidate_aliases: tuple[str, ...],
    entity_category: str,
    candidate_category: str,
) -> bool:
    """Implements the PSA shortcode heuristic.

    Fires only when the category pair is PSA↔Accounting AND one side
    (the shortcode side) is ≤4 chars normalized AND that side is the
    PSA side (source = `ruddr`) or any of the candidate's aliases is
    ≤4 chars. Match patterns:

        (i)  shortcode is a prefix of any whitespace-token of the
             long side; OR
        (ii) shortcode equals the consonant-skeleton initials of the
             long side (first letter of each token, lowercased).

    The CAN-019 rebrand pair (Stratos Cloud / CloudNine Infrastructure)
    is safe: both sides are >4 chars, so the heuristic does not fire.
    """
    pair = (entity_category, candidate_category)
    if pair not in (("accounting", "psa"), ("psa", "accounting")):
        return False

    candidates: list[tuple[str, str]] = []  # (shortcode, long_side)

    if entity_source == "ruddr" and 0 < len(entity_name) <= SHORTCODE_MAX_LEN:
        candidates.append((entity_name, candidate_name))
    if 0 < len(candidate_name) <= SHORTCODE_MAX_LEN:
        candidates.append((candidate_name, entity_name))
    for alias in candidate_aliases:
        if 0 < len(alias) <= SHORTCODE_MAX_LEN:
            candidates.append((alias, entity_name))

    for shortcode, long_side in candidates:
        sc = shortcode.strip().lower()
        ls = long_side.strip().lower()
        if not sc or not ls:
            continue
        tokens = [t for t in ls.split() if t]
        if not tokens:
            continue
        if any(t.startswith(sc) for t in tokens):
            return True
        initials = "".join(t[0] for t in tokens if t)
        if initials == sc:
            return True
    return False


def _compute_alias_boost_fires(
    entity_name: str,
    candidate_aliases: tuple[str, ...],
) -> bool:
    """Return True iff `max(token_set_ratio, jaro_winkler)` of
    `entity_name` against any alias exceeds `ALIAS_BOOST_THRESHOLD`
    (RapidFuzz 0..100 scale). Single application per pair regardless
    of how many aliases would qualify.
    """
    if not entity_name or not candidate_aliases:
        return False
    for alias in candidate_aliases:
        if not alias:
            continue
        ts = fuzz.token_set_ratio(entity_name, alias)
        jw = JaroWinkler.similarity(entity_name, alias) * 100.0
        if max(ts, jw) > ALIAS_BOOST_THRESHOLD:
            return True
    return False


def _compute_signal_breakdown(
    entity_name: str,
    candidate_name: str,
    candidate_aliases: tuple[str, ...],
    entity_source: str,
    entity_category: str,
    candidate_category: str,
) -> SignalBreakdown:
    """Compute all five weighted string-metric signals plus both
    boolean bonus flags. Pure function — no DB access."""
    token_sort = float(fuzz.token_sort_ratio(entity_name, candidate_name))
    token_set = float(fuzz.token_set_ratio(entity_name, candidate_name))
    partial = float(fuzz.partial_ratio(entity_name, candidate_name))
    jaro = JaroWinkler.similarity(entity_name, candidate_name) * 100.0
    jaccard = ngram_jaccard(entity_name, candidate_name)
    alias_fired = _compute_alias_boost_fires(entity_name, candidate_aliases)
    abbrev_fired = _check_psa_abbreviation(
        entity_name=entity_name,
        entity_source=entity_source,
        candidate_name=candidate_name,
        candidate_aliases=candidate_aliases,
        entity_category=entity_category,
        candidate_category=candidate_category,
    )
    return SignalBreakdown(
        token_sort_ratio=token_sort,
        token_set_ratio=token_set,
        partial_ratio=partial,
        jaro_winkler=jaro,
        ngram_jaccard=jaccard,
        alias_boost_fired=alias_fired,
        abbreviation_bonus_fired=abbrev_fired,
    )


def _compute_graph_evidence(
    conn: sqlite3.Connection,
    source_canonical_id: Optional[str],
    candidate_canonical_id: str,
    tenant_id: Optional[str],
) -> GraphEvidence:
    """Query the graph store for shared-neighbor evidence. Returns
    `_ZERO_EVIDENCE` semantically when `source_canonical_id` is None
    or no edges connect the endpoints."""
    person_count = count_shared_person_neighbors(
        conn, source_canonical_id, candidate_canonical_id, tenant_id
    )
    person_bonus = min(
        MAX_SHARED_PERSON_BONUS, person_count * PER_SHARED_PERSON_BONUS
    )
    overlap_count = count_shared_graph_neighbors(
        conn, source_canonical_id, candidate_canonical_id, tenant_id
    )
    overlap_bonus = min(
        MAX_NEIGHBORHOOD_BONUS, overlap_count * PER_NEIGHBORHOOD_NODE_BONUS
    )
    return GraphEvidence(
        shared_person_count=person_count,
        shared_person_bonus=person_bonus,
        neighborhood_overlap_count=overlap_count,
        neighborhood_overlap_bonus=overlap_bonus,
    )


def _weighted_score(
    weights: WeightConfig, breakdown: SignalBreakdown, evidence: GraphEvidence
) -> float:
    """Combine weighted signals + bonuses + evidence; clamp to [0, 1]."""
    weighted_sum = (
        weights.token_sort_ratio * breakdown.token_sort_ratio / 100.0
        + weights.token_set_ratio * breakdown.token_set_ratio / 100.0
        + weights.partial_ratio * breakdown.partial_ratio / 100.0
        + weights.jaro_winkler * breakdown.jaro_winkler / 100.0
        + weights.ngram_jaccard * breakdown.ngram_jaccard
    )
    raw = (
        weighted_sum
        + (weights.alias_boost if breakdown.alias_boost_fired else 0.0)
        + (weights.abbreviation_bonus if breakdown.abbreviation_bonus_fired else 0.0)
        + evidence.shared_person_bonus
        + evidence.neighborhood_overlap_bonus
    )
    return min(1.0, max(0.0, raw))


def score_pair(
    entity: NormalizedEntity,
    candidate_id: str,
    candidate_name: str,
    candidate_aliases: tuple[str, ...],
    candidate_category: str,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
    source_canonical_id: Optional[str] = None,
) -> ScoredMatch:
    """Score a single (entity, candidate canonical) pair.

    Empty-string guard: if either `entity.normalized_name` or
    `candidate_name` is empty / whitespace-only, returns a `ScoredMatch`
    with score 0.0 and zero-valued breakdown/evidence (still carrying
    the dispatch profile_id and category pair). No exception.

    `source_canonical_id` is the canonical ID assigned to `entity` by
    a prior pipeline pass, if any. At Stage 3 today the entity is
    unresolved (Stage 1 returned None, Stage 2 surfaced candidates);
    passing None disables the graph-corroborated signals cleanly.
    The argument exists so a post-Stage-6 caller can rescore an
    already-resolved entity against drifted weights and pick up the
    graph evidence.
    """
    weights = get_weights(entity.category, candidate_category)
    category_pair = (entity.category, candidate_category)

    entity_name = entity.normalized_name
    if not entity_name.strip() or not candidate_name.strip():
        return ScoredMatch(
            canonical_id=candidate_id,
            score=0.0,
            signal_breakdown=_ZERO_BREAKDOWN,
            graph_evidence=_ZERO_EVIDENCE,
            category_pair=category_pair,
            weight_profile_id=weights.profile_id,
        )

    breakdown = _compute_signal_breakdown(
        entity_name=entity_name,
        candidate_name=candidate_name,
        candidate_aliases=candidate_aliases,
        entity_source=entity.source,
        entity_category=entity.category,
        candidate_category=candidate_category,
    )
    evidence = _compute_graph_evidence(
        conn=conn,
        source_canonical_id=source_canonical_id,
        candidate_canonical_id=candidate_id,
        tenant_id=tenant_id,
    )
    score = _weighted_score(weights, breakdown, evidence)
    return ScoredMatch(
        canonical_id=candidate_id,
        score=score,
        signal_breakdown=breakdown,
        graph_evidence=evidence,
        category_pair=category_pair,
        weight_profile_id=weights.profile_id,
    )


def score_candidate_set(
    entity: NormalizedEntity,
    candidate_set: CandidateSet,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
    source_canonical_id: Optional[str] = None,
) -> tuple[ScoredMatch, ...]:
    """Score every candidate in a `CandidateSet`; sort by descending
    score, breaking ties on ascending canonical_id for deterministic
    ordering. Skips candidates whose canonical_id has no row in
    `canonical_entities` (out of tenant scope or stale)."""
    scored: list[ScoredMatch] = []
    for cand in candidate_set.candidates:
        meta = get_canonical_name_and_category(conn, cand.canonical_id, tenant_id)
        if meta is None:
            continue
        canonical_name, _entity_category, _entity_type = meta
        aliases = tuple(get_aliases(conn, cand.canonical_id, tenant_id))
        # Candidate's category is inferred from its system_references at write
        # time (Stage 6). For V1, infer from the entity's cross-category pair:
        # if the entity is accounting, candidate is psa, and vice-versa.
        # Within-category candidates fall back to the same category (so the
        # dispatch returns DEFAULT_WEIGHTS, the safe path).
        candidate_category = _infer_candidate_category(entity.category)
        scored.append(
            score_pair(
                entity=entity,
                candidate_id=cand.canonical_id,
                candidate_name=canonical_name,
                candidate_aliases=aliases,
                candidate_category=candidate_category,
                conn=conn,
                tenant_id=tenant_id,
                source_canonical_id=source_canonical_id,
            )
        )
    scored.sort(key=lambda m: (-m.score, m.canonical_id))
    return tuple(scored)


def _infer_candidate_category(entity_category: str) -> str:
    """In V1, candidates surfaced by Stage 2 are by construction from the
    *other* source category (Stage 2d filters intra-system pairs). For
    the cross-category pair we know about (accounting / psa), flip;
    otherwise default to the same category and let dispatch fall to
    DEFAULT_WEIGHTS."""
    if entity_category == "accounting":
        return "psa"
    if entity_category == "psa":
        return "accounting"
    return entity_category
