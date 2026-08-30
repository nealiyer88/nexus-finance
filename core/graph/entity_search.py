"""Fuzzy name search over canonical entities (feature 14).

Separated from `core/graph/entity_store.py` because that module is
banned from importing RapidFuzz (`tests/test_blocking.py::
test_no_rapidfuzz_in_matching_modules`). This module takes the raw
`(canonical_id, canonical_name, alias_values)` rows `entity_store.
list_entities_for_search` returns and scores them against a query
string using RapidFuzz's `token_set_ratio` — the same metric
`core/matching/scoring.py` uses for entity-name comparison.
"""

from __future__ import annotations

from rapidfuzz import fuzz

# Below this token_set_ratio score, a candidate is not considered a
# match. Chosen to tolerate a misspelling (transposed/dropped letters)
# while still rejecting a query that shares no tokens with a row.
SEARCH_SCORE_THRESHOLD: float = 60.0


def search_canonical_ids(
    candidates: list[tuple[str, str, list[str]]],
    query: str,
    score_threshold: float = SEARCH_SCORE_THRESHOLD,
) -> list[str]:
    """Return `canonical_id`s from `candidates` whose `canonical_name` or
    any alias value scores `>= score_threshold` against `query` via
    `fuzz.token_set_ratio`, ranked best-score-first (ties broken by
    ascending `canonical_id` for determinism).

    An empty/whitespace-only `query` returns every candidate's id,
    unranked (identity — "no search term" is not "no results").
    """
    stripped = query.strip()
    if not stripped:
        return [canonical_id for canonical_id, _name, _aliases in candidates]

    scored: list[tuple[float, str]] = []
    for canonical_id, name, aliases in candidates:
        best = float(fuzz.token_set_ratio(stripped, name)) if name else 0.0
        for alias in aliases:
            if not alias:
                continue
            best = max(best, float(fuzz.token_set_ratio(stripped, alias)))
        if best >= score_threshold:
            scored.append((best, canonical_id))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [canonical_id for _score, canonical_id in scored]
