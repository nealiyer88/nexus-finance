"""Pipeline Stage 3: Pairwise Scoring.

Scores `(NormalizedEntity, candidate canonical)` pairs surfaced by Stage 2
(Blocking) into a single `ScoredMatch`. Signal sources:

    A.  Five string-metric signals + fasttext cosine (Signal Set C),
        all weighted via `WeightConfig`.

    B.  Additive bonuses gated by heuristics:
            - `alias_boost` fires when the entity name scores >85 against
              any candidate alias. Single application per pair.
            - `abbreviation_bonus` fires only for the PSA↔Accounting
              category pair when one side is ≤4 chars with a matching
              prefix-token or first-letter initialism.

    C.  Signal Set B (B1, B2, B3, B4, B5, B6) — graph-corroborated adaptive
        boosts, capped at +0.20 total. Only fire in the ambiguous band
        0.70 ≤ base_score < 0.90.

Final formula:

    budget     = sum of tier-1 weights available for the pair
                 (five string metrics + alias_boost, plus fasttext_cosine
                 only when an embedding was computed for BOTH names)
    base_score = weighted_sum(available A signals) / budget
                 + abbreviation_bonus
    score      = clamp(base_score + sum(b_boost.applied), 0.0, 1.0)

Dynamic renormalization: the tier-1 weights are configured to sum to
1.0 WITHOUT `fasttext_cosine` (`test_weights_sum_to_one`). When the
embedding model is absent (or either name is OOV-empty), the fastText
slot is excluded from both numerator and budget, so the no-model path
divides by exactly 1.0 and scores byte-identically to the pre-8a
stack. When embeddings are available, the budget grows by the
fastText weight and the whole tier-1 sum renormalizes to a 1.0
ceiling — an absent signal never consumes weight, and a present one
never inflates the budget past 1.0 (AC-5).

This module is the only matcher module that imports `rapidfuzz`. Stages
1, 2, and 0 do not (enforced by
`tests/test_blocking.py::test_no_rapidfuzz_in_matching_modules`).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, replace
from typing import Literal, Optional

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from connectors.base import NormalizedEntity
from core.graph.entity_store import (
    count_amount_cooccurrence_periods,
    count_shared_graph_neighbors,
    count_shared_person_neighbors,
    get_aliases,
    get_canonical_name_and_category,
    get_created_at,
    get_external_field,
)
from core.matching import embeddings as _embeddings
from core.matching.types import (
    CandidateSet,
    GraphEvidence,
    ScoredMatch,
    SignalBreakdown,
)
from core.matching.weights import WeightConfig, get_weights


@dataclass(frozen=True)
class BoostEntry:
    """One fired Signal Set B boost with raw and cap-applied values."""

    signal_id: Literal["B1", "B2", "B3", "B4", "B5", "B6"]
    raw: float
    applied: float


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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


_FREEMAIL_DOMAINS: frozenset[str] = frozenset(
    {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}
)

_FRAGMENT_SPLIT_RE = re.compile(r"[-/_\s]+")

MAX_B_BOOST: float = 0.20

# Signal Set B only fires in the ambiguous band (v4 §9). Shared by the
# _compute_b_boosts guard and score_pair's decision to fetch candidate
# external_fields at all.
B_BOOST_BAND_LOW: float = 0.70
B_BOOST_BAND_HIGH: float = 0.90

# Key-casing variants per source system (QB emits PascalCase 'Class';
# mirrors llm_fallback's _CLASS_CODE_KEYS / _PROJECT_CODE_KEYS).
_B2_STRING_KEYS: tuple[str, ...] = (
    "class", "Class", "class_code", "ClassCode", "memo", "Memo",
)
_B2_LIST_KEYS: tuple[str, ...] = ("project_codes", "projectCodes")
_B4_EMAIL_KEYS: tuple[str, ...] = ("email", "Email", "PrimaryEmail", "primary_email")


def _extract_project_code_fragments(external_fields: dict) -> set[str]:
    """Return uppercase fragments (≥2 chars) from class/memo/project-code
    fields in `external_fields` (all key-casing variants). Splits on
    -, /, _, and space."""
    out: set[str] = set()
    for key in _B2_STRING_KEYS:
        val = external_fields.get(key)
        if not isinstance(val, str):
            continue
        for frag in _FRAGMENT_SPLIT_RE.split(val):
            frag = frag.upper()
            if len(frag) >= 2:
                out.add(frag)
    for key in _B2_LIST_KEYS:
        codes = external_fields.get(key)
        if not isinstance(codes, list):
            continue
        for item in codes:
            if not isinstance(item, str):
                continue
            for frag in _FRAGMENT_SPLIT_RE.split(item):
                frag = frag.upper()
                if len(frag) >= 2:
                    out.add(frag)
    return out


def _first_email(external_fields: dict) -> Optional[str]:
    """First non-empty email value under any known key casing."""
    for key in _B4_EMAIL_KEYS:
        val = external_fields.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def _get_candidate_external_fields(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str],
) -> dict:
    """Merge all external_fields JSON blobs for `canonical_id` into one dict."""
    if tenant_id is None:
        rows = conn.execute(
            "SELECT external_fields FROM system_references WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.external_fields
              FROM system_references AS s
              JOIN canonical_entities AS c ON c.canonical_id = s.canonical_id
             WHERE s.canonical_id = ?
               AND c.tenant_id = ?
            """,
            (canonical_id, tenant_id),
        ).fetchall()
    combined: dict = {}
    for (fields_json,) in rows:
        if not fields_json:
            continue
        try:
            d = json.loads(fields_json)
            if isinstance(d, dict):
                combined.update(d)
        except (TypeError, ValueError):
            pass
    return combined


def _compute_b_boosts(
    conn: sqlite3.Connection,
    source_id: Optional[str],
    candidate_id: str,
    source_category: str,
    candidate_category: str,
    source_external_fields: dict,
    candidate_external_fields: dict,
    base_score: float,
    tenant_id: Optional[str] = None,
    person_count: Optional[int] = None,
    neighbor_count: Optional[int] = None,
    amount_cooccurrence_periods: Optional[int] = None,
) -> tuple[BoostEntry, ...]:
    """Compute Signal Set B (B1, B2, B3, B4, B5, B6) adaptive boosts.

    Returns () when base_score is outside [0.70, 0.90) — boosts only
    apply in the ambiguous band where they are load-bearing evidence.
    Total applied boost is capped at MAX_B_BOOST (+0.20); distributed
    proportionally when the sum of raw boosts would exceed the cap.

    `person_count` / `neighbor_count` accept shared-neighbor counts the
    caller already queried (score_pair queries them once for
    GraphEvidence per AC-23); None falls back to querying here.

    `amount_cooccurrence_periods` accepts the B3 distinct-period count
    the caller already queried (score_pair queries it once, only when
    `in_b_band`); None means "skip B3" so existing direct-call tests
    keep working unchanged. A count of 0 also contributes nothing.
    """
    if base_score < B_BOOST_BAND_LOW or base_score >= B_BOOST_BAND_HIGH:
        return ()

    raws: list[tuple[Literal["B1", "B2", "B3", "B4", "B5", "B6"], float]] = []

    # B1 — shared person neighbors (+0.05/person, cap 0.10)
    n_persons = (
        person_count
        if person_count is not None
        else count_shared_person_neighbors(conn, source_id, candidate_id, tenant_id)
    )
    b1_raw = min(n_persons * 0.05, 0.10)
    if b1_raw > 0:
        raws.append(("B1", b1_raw))

    # B2 — overlapping project-code fragments (+0.08 for 1 fragment, +0.12 for ≥2)
    src_frags = _extract_project_code_fragments(source_external_fields)
    cand_frags = _extract_project_code_fragments(candidate_external_fields)
    overlap = src_frags & cand_frags
    if overlap:
        b2_raw = 0.12 if len(overlap) >= 2 else 0.08
        raws.append(("B2", b2_raw))

    # B3 — amount co-occurrence within tolerance, same period
    # (+0.10 for 1 distinct co-occurring period, +0.15 for ≥2).
    if amount_cooccurrence_periods is not None and amount_cooccurrence_periods > 0:
        b3_raw = 0.15 if amount_cooccurrence_periods >= 2 else 0.10
        raws.append(("B3", b3_raw))

    # B4 — matching email domain (+0.05 freemail, +0.08 corporate).
    # Source side: read the unresolved entity's own record first (the
    # normal Stage 3 path has source_id=None, so a DB-only read would
    # leave B4 permanently dark); resolved-entity DB read is the
    # fallback. Candidate side: the merged external_fields dict is
    # already in hand — a get_external_field query would rescan the
    # same system_references rows.
    src_email = _first_email(source_external_fields)
    if src_email is None and source_id:
        src_email = get_external_field(conn, source_id, "email", tenant_id)
    cand_email = _first_email(candidate_external_fields)
    if src_email and cand_email:
        src_domain = src_email.split("@")[-1].lower() if "@" in src_email else ""
        cand_domain = cand_email.split("@")[-1].lower() if "@" in cand_email else ""
        if src_domain and cand_domain and src_domain == cand_domain:
            b4_raw = 0.05 if src_domain in _FREEMAIL_DOMAINS else 0.08
            raws.append(("B4", b4_raw))

    # B5 — temporal co-occurrence (created_at delta ≤ 30 days)
    ts_src = get_created_at(conn, source_id, tenant_id) if source_id else None
    ts_cand = get_created_at(conn, candidate_id, tenant_id)
    if ts_src is not None and ts_cand is not None:
        # abs() BEFORE .days: timedelta.days floors toward -inf, so
        # (a - b).days and (b - a).days differ by one for sub-day
        # remainders — the boost must be symmetric in argument order.
        delta_days = abs(ts_src - ts_cand).days
        if delta_days <= 30:
            b5_raw = 0.05 if delta_days < 15 else 0.03
            raws.append(("B5", b5_raw))

    # B6 — shared graph neighbors of any category (+0.025/node, cap 0.10)
    n_neighbors = (
        neighbor_count
        if neighbor_count is not None
        else count_shared_graph_neighbors(conn, source_id, candidate_id, tenant_id)
    )
    b6_raw = min(n_neighbors * 0.025, 0.10)
    if b6_raw > 0:
        raws.append(("B6", b6_raw))

    if not raws:
        return ()

    total_raw = sum(r for _, r in raws)
    scale = min(1.0, MAX_B_BOOST / total_raw) if total_raw > MAX_B_BOOST else 1.0
    return tuple(
        BoostEntry(signal_id=sig, raw=r, applied=r * scale)
        for sig, r in raws
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


ABBREV_MIN_PREFIX_LEN: int = 3
ABBREV_MIN_CONCAT_PART_LEN: int = 2


def _is_concat_of_token_prefixes(token: str, long_tokens: list[str]) -> bool:
    """True when `token` is a concatenation of prefixes (each
    ≥ ABBREV_MIN_CONCAT_PART_LEN chars) of 2+ CONSECUTIVE tokens of
    `long_tokens` — the 'pacrim' = 'pac|rim' over 'pacific rim' pattern.
    The whole token must be consumed.

    Memoized on (position, token index, min(parts, 2)) — states are
    polynomial, so adversarially repetitive source-system names
    ('aa aa aa …') cannot trigger exponential path exploration."""

    def consume(pos: int, idx: int, parts: int, memo: dict) -> bool:
        if pos == len(token):
            return parts >= 2
        if idx >= len(long_tokens):
            return False
        key = (pos, idx, min(parts, 2))
        cached = memo.get(key)
        if cached is not None:
            return cached
        tok = long_tokens[idx]
        hi = min(len(token) - pos, len(tok))
        result = False
        for take in range(ABBREV_MIN_CONCAT_PART_LEN, hi + 1):
            if tok[:take] == token[pos : pos + take] and consume(
                pos + take, idx + 1, parts + 1, memo
            ):
                result = True
                break
        memo[key] = result
        return result

    return any(
        consume(0, start, 0, {}) for start in range(len(long_tokens) - 1)
    )


def _tokens_abbreviate(short_name: str, long_name: str) -> bool:
    """Token-level abbreviation test: the side with strictly FEWER
    whitespace-tokens abbreviates the other iff EVERY short-side token
    either (i) appears verbatim in the long side, (ii) is a
    ≥3-char proper prefix of some long-side token ('tech' →
    'technologies', 'cap' → 'capital'), or (iii) is a concatenation of
    prefixes of consecutive long-side tokens ('pacrim' → 'pacific
    rim') — AND at least one token matched via (ii)/(iii). All-verbatim
    subsets ('cenlar' ⊂ 'cenlar fsb') are truncations, not
    abbreviations: token_set_ratio already scores them 100, and firing
    the bonus there would rank a truncated candidate above an exact
    match. Equal token counts never fire — plain string metrics own
    that regime."""
    ta = [t for t in short_name.split() if t]
    tb = [t for t in long_name.split() if t]
    if not ta or not tb or len(ta) == len(tb):
        return False
    short, long_ = (ta, tb) if len(ta) < len(tb) else (tb, ta)
    any_abbreviated = False
    for t in short:
        if t in long_:
            continue
        if len(t) >= ABBREV_MIN_PREFIX_LEN and any(
            lt.startswith(t) and len(lt) > len(t) for lt in long_
        ):
            any_abbreviated = True
            continue
        if _is_concat_of_token_prefixes(t, long_):
            any_abbreviated = True
            continue
        return False
    return any_abbreviated


def _check_psa_abbreviation(
    entity_name: str,
    candidate_name: str,
    candidate_aliases: tuple[str, ...],
    entity_category: str,
    candidate_category: str,
) -> bool:
    """Implements the PSA shortcode heuristic.

    Fires only when the category pair is PSA↔Accounting AND the
    shortcode side (the ≤4-char side) is the PSA side. In V1 the only
    PSA connector is RUDDR, so the category-pair invariant uniquely
    identifies which side is PSA — gating on category alone is
    sufficient and consistent across the entity, candidate, and alias
    branches. Match patterns:

        (i)  shortcode is a prefix of any whitespace-token of the
             long side; OR
        (ii) shortcode equals the consonant-skeleton initials of the
             long side (first letter of each token, lowercased).

    Token-level extension (SC-5 amended): independent of the ≤4-char
    shortcode branch, multi-word abbreviations fire when every token of
    the fewer-token side abbreviates the other side (verbatim token,
    ≥3-char prefix, or concatenated consecutive prefixes) — catches
    'pacrim tech' ↔ 'pacific rim technologies international' and
    'meridian cap' ↔ 'meridian capital group'. This branch is
    side-agnostic within the PSA↔Accounting pair: QB abbreviates RUDDR
    names as often as the reverse in the ground-truth fixture.
    Measured on the real pre-trained fastText model (2026-07-05),
    embedding cosine cannot separate these pairs (pacrim↔pacific
    cosine ≈ 0.0), so this deterministic heuristic is the abbreviation
    signal, with Stage 4 routing heuristic-fired mid-band pairs to the
    review queue.

    The CAN-019 rebrand pair (Stratos Cloud / CloudNine Infrastructure)
    is safe: the shortcode branch needs a ≤4-char side, and the
    token-level branch needs unequal token counts plus full coverage —
    'stratos' matches nothing in 'cloudnine infrastructure'.
    """
    pair = (entity_category, candidate_category)
    if pair not in (("accounting", "psa"), ("psa", "accounting")):
        return False

    if _tokens_abbreviate(entity_name.strip().lower(), candidate_name.strip().lower()):
        return True

    candidates: list[tuple[str, str]] = []  # (shortcode, long_side)

    # Entity-side shortcode: only when the entity is the PSA side.
    if entity_category == "psa" and 0 < len(entity_name) <= SHORTCODE_MAX_LEN:
        candidates.append((entity_name, candidate_name))
    # Candidate-side shortcode: only when the candidate is the PSA side.
    if candidate_category == "psa" and 0 < len(candidate_name) <= SHORTCODE_MAX_LEN:
        candidates.append((candidate_name, entity_name))
    # Alias-side shortcode: only when the candidate (alias owner) is PSA.
    if candidate_category == "psa":
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
    candidate_name: str,
    candidate_aliases: tuple[str, ...],
) -> bool:
    """Return True iff `max(token_set_ratio, jaro_winkler)` of
    `entity_name` against any alias exceeds `ALIAS_BOOST_THRESHOLD`
    (RapidFuzz 0..100 scale). Single application per pair regardless
    of how many aliases would qualify.

    Defensive guard: any alias whose value equals `candidate_name`
    (case-insensitive) is skipped — `get_aliases` already filters
    `value == canonical_name` at the SQL layer, but the boost path
    must not double-count the canonical's own name even when callers
    bypass that filter.
    """
    if not entity_name or not candidate_aliases:
        return False
    cand_lower = candidate_name.strip().lower()
    for alias in candidate_aliases:
        if not alias:
            continue
        if alias.strip().lower() == cand_lower:
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
    entity_category: str,
    candidate_category: str,
) -> SignalBreakdown:
    """Compute all string-metric signals, fasttext cosine, and bonus flags.
    Pure function — no DB access."""
    token_sort = float(fuzz.token_sort_ratio(entity_name, candidate_name))
    token_set = float(fuzz.token_set_ratio(entity_name, candidate_name))
    partial = float(fuzz.partial_ratio(entity_name, candidate_name))
    jaro = JaroWinkler.similarity(entity_name, candidate_name) * 100.0
    jaccard = ngram_jaccard(entity_name, candidate_name)
    alias_fired = _compute_alias_boost_fires(
        entity_name, candidate_name, candidate_aliases
    )
    abbrev_fired = _check_psa_abbreviation(
        entity_name=entity_name,
        candidate_name=candidate_name,
        candidate_aliases=candidate_aliases,
        entity_category=entity_category,
        candidate_category=candidate_category,
    )
    vec_entity = _embeddings.embed(entity_name)
    vec_candidate = _embeddings.embed(candidate_name)
    ft_available = vec_entity is not None and vec_candidate is not None
    ft_cosine = _embeddings.cosine(vec_entity, vec_candidate)
    return SignalBreakdown(
        token_sort_ratio=token_sort,
        token_set_ratio=token_set,
        partial_ratio=partial,
        jaro_winkler=jaro,
        ngram_jaccard=jaccard,
        alias_boost_fired=alias_fired,
        abbreviation_bonus_fired=abbrev_fired,
        fasttext_cosine=ft_cosine,
        fasttext_available=ft_available,
    )


def _compute_graph_evidence(
    person_count: int,
    overlap_count: int,
) -> GraphEvidence:
    """Derive shared-neighbor evidence from counts the caller already
    queried (AC-23: one query per count per pair — the same counts feed
    Signal Set B). Semantically `_ZERO_EVIDENCE` when both counts are 0."""
    person_bonus = min(
        MAX_SHARED_PERSON_BONUS, person_count * PER_SHARED_PERSON_BONUS
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


def _base_weighted_score(
    weights: WeightConfig, breakdown: SignalBreakdown
) -> float:
    """Weighted tier-1 sum renormalized over available signals, plus the
    abbreviation bonus; not clamped.

    The tier-1 budget is the five string weights + alias_boost
    (configured to sum to exactly 1.0 — `test_weights_sum_to_one`),
    plus `fasttext_cosine` ONLY when `breakdown.fasttext_available`.
    Dividing by the budget keeps the tier-1 ceiling at 1.0 whether or
    not the embedding model is present (AC-5); the no-model path
    divides by exactly 1.0 and reproduces the pre-8a score bit-for-bit.
    `abbreviation_bonus` stays outside the budget (upside, not budget).
    """
    weighted_sum = (
        weights.token_sort_ratio * breakdown.token_sort_ratio / 100.0
        + weights.token_set_ratio * breakdown.token_set_ratio / 100.0
        + weights.partial_ratio * breakdown.partial_ratio / 100.0
        + weights.jaro_winkler * breakdown.jaro_winkler / 100.0
        + weights.ngram_jaccard * breakdown.ngram_jaccard
    )
    budget = (
        weights.token_sort_ratio
        + weights.token_set_ratio
        + weights.partial_ratio
        + weights.jaro_winkler
        + weights.ngram_jaccard
        + weights.alias_boost
    )
    if breakdown.fasttext_available:
        weighted_sum += weights.fasttext_cosine * breakdown.fasttext_cosine
        budget += weights.fasttext_cosine
    weighted_sum += weights.alias_boost if breakdown.alias_boost_fired else 0.0
    return weighted_sum / budget + (
        weights.abbreviation_bonus if breakdown.abbreviation_bonus_fired else 0.0
    )


def _weighted_score(
    base_score: float,
    b_boosts: tuple[BoostEntry, ...],
) -> float:
    """Combine the precomputed base score + Signal Set B applied boosts;
    clamp to [0, 1]. Takes the base rather than recomputing it so the
    band-gating value and the scored value can never diverge."""
    raw = base_score + sum(e.applied for e in b_boosts)
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
    with score 0.0 and zero-valued breakdown/evidence. No exception.

    `source_canonical_id` is the canonical ID of `entity` from a prior
    pipeline pass. None disables graph-corroborated signals (B1, B5, B6)
    cleanly — normal path at Stage 3 where the entity is still unresolved.
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
        entity_category=entity.category,
        candidate_category=candidate_category,
    )

    base_score = _base_weighted_score(weights, breakdown)

    # Shared-neighbor counts queried ONCE per pair (AC-23): the same
    # counts drive Signal Set B (B1, B6) and GraphEvidence.
    person_count = count_shared_person_neighbors(
        conn, source_canonical_id, candidate_id, tenant_id
    )
    neighbor_count = count_shared_graph_neighbors(
        conn, source_canonical_id, candidate_id, tenant_id
    )

    # external_fields only feed B2, which only fires in the ambiguous
    # band — skip the per-pair fetch + JSON parse everywhere else.
    in_b_band = B_BOOST_BAND_LOW <= base_score < B_BOOST_BAND_HIGH
    candidate_ext = (
        _get_candidate_external_fields(conn, candidate_id, tenant_id)
        if in_b_band
        else {}
    )
    # B3 join keys are load-bearing: the source side is unresolved on
    # the normal Stage 3 path, so key on (entity.source, entity.source_id)
    # — never on source_canonical_id. Queried at most once per pair,
    # only when in_b_band (mirrors the candidate_ext gating above).
    amount_cooccurrence_periods = (
        count_amount_cooccurrence_periods(
            conn, entity.source, entity.source_id, candidate_id, tenant_id
        )
        if in_b_band
        else None
    )
    b_boosts = _compute_b_boosts(
        conn=conn,
        source_id=source_canonical_id,
        candidate_id=candidate_id,
        source_category=entity.category,
        candidate_category=candidate_category,
        source_external_fields=entity.raw_record,
        candidate_external_fields=candidate_ext,
        base_score=base_score,
        tenant_id=tenant_id,
        person_count=person_count,
        neighbor_count=neighbor_count,
        amount_cooccurrence_periods=amount_cooccurrence_periods,
    )

    score = _weighted_score(base_score, b_boosts)

    evidence = _compute_graph_evidence(person_count, neighbor_count)

    final_breakdown = replace(breakdown, b_boosts=b_boosts)

    return ScoredMatch(
        canonical_id=candidate_id,
        score=score,
        signal_breakdown=final_breakdown,
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
