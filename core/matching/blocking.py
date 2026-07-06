"""Pipeline Stage 2: Blocking.

Reduces the comparison space for Stage 3 (Pairwise Scoring) from O(n^2) to
O(n*k) by surfacing a bounded candidate set per query entity. Three steps:

    2a. Tokenize the query's normalized_name.
    2b. Collect candidates from the token inverted index.
    2c. Collect candidates from the trigram inverted index.
    2d. Intra-system filter: exclude any candidate whose system_references
        already include (source = query.source, external_id = query.source_id);
        that candidate is the query's own canonical via deterministic match
        and Stage 1 owns it. Candidates with refs across multiple sources
        survive as long as no exact (source, source_id) collision.
    2e. Cap at CANDIDATE_CAP (50). On overflow, hard-truncate keeping
        token/trigram-corroborated candidates before embed-only ones
        (the embedding channel returns top_k hits regardless of cosine,
        so embed-only noise must never evict a lexical match), in
        canonical_id sort order within each group, and emit a
        `logger.warning`. No ranking, no IDF beyond that.

Empty input (no tokens, no trigrams) returns an empty CandidateSet rather
than raising — guards against future normalizer regressions producing
degenerate names.

Does NOT call rapidfuzz, does NOT consult Stage 1's anchors, does NOT
mutate the graph store.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from connectors.base import NormalizedEntity
from core.graph.entity_store import get_system_refs
from core.matching.indices import EmbeddingIndex, NgramIndex, TokenIndex
from core.matching.types import CandidateEntity, CandidateSet


CANDIDATE_CAP: int = 50

_logger = logging.getLogger(__name__)


def generate_candidates(
    entity: NormalizedEntity,
    token_index: TokenIndex,
    ngram_index: NgramIndex,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
    embedding_index: Optional[EmbeddingIndex] = None,
) -> CandidateSet:
    del tenant_id

    tokens = [t for t in entity.normalized_name.split() if t]
    if not tokens and not entity.normalized_name.strip():
        return CandidateSet(source_entity_id=entity.source_id, candidates=())

    token_hits = token_index.candidates_per_token(tokens)
    ngram_hits = ngram_index.candidates_per_gram(entity.normalized_name)

    signals_by_candidate: dict[str, set[str]] = {}
    for tok, cids in token_hits.items():
        for cid in cids:
            signals_by_candidate.setdefault(cid, set()).add(f"token:{tok}")
    for gram, cids in ngram_hits.items():
        for cid in cids:
            signals_by_candidate.setdefault(cid, set()).add(f"trigram:{gram}")

    # Stage 2c: fastText flat-cosine ANN (skipped when embedding_index is
    # None). Non-positive cosines are noise — query() returns top_k hits
    # unconditionally, so an explicit floor keeps orthogonal names out.
    if embedding_index is not None:
        embed_hits = embedding_index.query(entity.normalized_name, top_k=CANDIDATE_CAP)
        for rank, (cid, score) in enumerate(embed_hits, start=1):
            if score <= 0.0:
                continue
            signals_by_candidate.setdefault(cid, set()).add(f"embed:{rank}")

    if not signals_by_candidate:
        return CandidateSet(source_entity_id=entity.source_id, candidates=())

    survivors: dict[str, set[str]] = {}
    for cid, signals in signals_by_candidate.items():
        refs = get_system_refs(conn, cid)
        if (entity.source, entity.source_id) in refs:
            continue
        survivors[cid] = signals

    if not survivors:
        return CandidateSet(source_entity_id=entity.source_id, candidates=())

    # Overflow truncation: lexically-corroborated candidates (any
    # token:/trigram: signal) outrank embed-only ones — the embedding
    # channel emits top_k hits even at near-zero cosine, and pre-8a
    # behavior (token+trigram surfacing) must survive model install.
    # canonical_id order within each group keeps output deterministic.
    sorted_cids = sorted(
        survivors.keys(),
        key=lambda cid: (
            all(s.startswith("embed:") for s in survivors[cid]),
            cid,
        ),
    )
    if len(sorted_cids) > CANDIDATE_CAP:
        _logger.warning(
            "candidate cap exceeded: %d candidates for source_id=%s; truncating to %d",
            len(sorted_cids),
            entity.source_id,
            CANDIDATE_CAP,
        )
        sorted_cids = sorted_cids[:CANDIDATE_CAP]

    candidates = tuple(
        CandidateEntity(
            canonical_id=cid,
            blocking_signals=tuple(sorted(survivors[cid])),
        )
        for cid in sorted_cids
    )

    return CandidateSet(source_entity_id=entity.source_id, candidates=candidates)
