"""Matcher orchestrator (feature 12): the `match()` entry point.

Wires Stages 1-6 into a single call per incoming entity, per spec
Section 9. Sequential only — no async, no parallel, no queue (rules
§13, NOT-SCOPE §11).

    Stage 1 (deterministic_match) -> if hit: Stage 6 confirm, return.
    Stage 2 (generate_candidates) -> if empty: Stage 6 new entity, return.
    Stage 3 (score_candidate_set) -> Stage 4 (apply_thresholds)
    if action == LLM_FALLBACK: Stage 5 (llm_assess), catching the two
        documented failure modes and converting to QUEUE_FOR_REVIEW.
    Stage 6: dispatch to the writer appropriate to the final action.

Index rebuild policy (DECIDED, not a builder choice — see
`core.graph.resolution.mark_indices_stale`): checked once per incoming
entity, immediately before Stage 2, for every entity that reaches this
point (a Stage 1 hit never touches blocking and so never triggers a
rebuild it doesn't need).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any, Optional

from core.graph.resolution import (
    create_new_entity,
    mark_indices_stale,
    reset_indices_stale_flag,
    resolve_match,
)
from core.ingestion.normalizer import NormalizedEntity
from core.matching.blocking import generate_candidates
from core.matching.deterministic import deterministic_match
from core.matching.disposition import apply_thresholds
from core.matching.indices import EmbeddingIndex, NgramIndex, TokenIndex
from core.matching.llm_fallback import (
    LLMBudgetExceededError,
    LLMNotConfiguredError,
    llm_assess,
)
from core.matching.pending_store import enqueue_pending
from core.matching.scoring import score_candidate_set
from core.matching.types import Action, Disposition, MatchContext, MatchResult, MatchType


# The single fixed system-actor identity for every write this orchestrator
# makes without a human in the loop (auto-approvals and system-created new
# entities). Every caller imports this constant — never an inline literal.
AUTO_APPROVAL_ACTOR: str = "system:matcher-orchestrator"

# Organization sub-type inferred from the connector's raw record `type`
# field. V1 connector set only (QB + RUDDR); default to "client" for an
# unrecognized organization type rather than raising, since Stage 6 must
# still be able to mint a canonical id.
_ORG_TYPE_MAP: dict[str, str] = {
    "customer": "client",
    "client": "client",
    "vendor": "vendor",
}


def _infer_canonical_entity_type(entity: NormalizedEntity) -> str:
    """Canonical `entity_type` (id-prefix axis) for a NEW canonical,
    derived from the connector-source's own `type` field — never from
    `entity.category` (accounting/psa), which is a different axis."""
    if entity.entity_category == "person":
        return "person"
    raw_type = str(entity.raw_record.get("type", "")).lower()
    return _ORG_TYPE_MAP.get(raw_type, "client")


def _rebuild_indices_if_stale(ctx: MatchContext) -> None:
    """Stage 6's writers set a module-level dirty bit on every graph
    mutation; there is no incremental index update path, so a set bit
    means the three blocking indices must be rebuilt from the connection
    before the next Stage 2 call sees the updated graph."""
    if mark_indices_stale():
        ctx.token_index = TokenIndex.build(ctx.conn, ctx.tenant_id)
        ctx.ngram_index = NgramIndex.build(ctx.conn, ctx.tenant_id)
        ctx.embedding_index = EmbeddingIndex.build(ctx.conn, ctx.tenant_id)
        reset_indices_stale_flag()


def _write_confirmed_match(
    conn: sqlite3.Connection,
    disposition: Disposition,
    entity: NormalizedEntity,
    canonical_id: str,
    alias_confidence: float,
    tenant_id: Optional[str],
    reasoning_trace: str,
) -> str:
    """Stage 6 AUTO_APPROVE write. There is no second canonical node on
    the orchestrator's own side of the match (the incoming entity is a
    raw connector record, not yet a graph node) — the confirmation is
    the alias write; the edge is a self-referential SAME_AS on
    `canonical_id`, whose `approval_count` then tracks repeat
    corroboration of the same canonical across ingestion runs."""
    return resolve_match(
        conn,
        disposition,
        entity,
        canonical_id=canonical_id,
        alias_confidence=alias_confidence,
        source_node=canonical_id,
        target_node=canonical_id,
        relationship="SAME_AS",
        source_category=entity.category,
        target_category=entity.category,
        weight=alias_confidence,
        approved_by=AUTO_APPROVAL_ACTOR,
        reasoning_trace=reasoning_trace,
        tenant_id=tenant_id,
    )


def _write_new_entity(
    conn: sqlite3.Connection,
    disposition: Disposition,
    entity: NormalizedEntity,
    tenant_id: Optional[str],
    reasoning_trace: str,
) -> str:
    """Stage 6 NO_MATCH write: mint a brand-new canonical for `entity`."""
    entity_type = _infer_canonical_entity_type(entity)
    system_ref = {
        "source": entity.source,
        "category": entity.category,
        "external_id": entity.source_id,
        "external_fields": entity.raw_record,
    }
    return create_new_entity(
        conn,
        disposition,
        entity,
        canonical_name=entity.normalized_name,
        entity_type=entity_type,
        entity_category=entity.entity_category,
        confidence=1.0,
        system_refs=(system_ref,),
        approved_by=AUTO_APPROVAL_ACTOR,
        reasoning_trace=reasoning_trace,
        tenant_id=tenant_id,
    )


def _build_confirmed_proposal(
    entity: NormalizedEntity,
    canonical_id: str,
    alias_confidence: float,
    reasoning_trace: str,
) -> dict[str, Any]:
    """The JSON-serializable keyword-argument mapping the orchestrator
    would have handed to `resolve_match` (minus `conn`/`disposition`/
    `entity`/`tenant_id`, which the enqueue call and a later approval
    both derive independently, and minus `approved_by`, which has no
    value yet — that identity belongs to the human who eventually
    approves the pending row, a feature 11 concern)."""
    return {
        "canonical_id": canonical_id,
        "alias_confidence": alias_confidence,
        "source_node": canonical_id,
        "target_node": canonical_id,
        "relationship": "SAME_AS",
        "source_category": entity.category,
        "target_category": entity.category,
        "weight": alias_confidence,
        "reasoning_trace": reasoning_trace,
    }


def _write_queued(
    conn: sqlite3.Connection,
    disposition: Disposition,
    entity: NormalizedEntity,
    tenant_id: Optional[str],
    reasoning_trace: str,
) -> Optional[str]:
    """Stage 6 QUEUE_FOR_REVIEW write: durably persist the decision via
    feature 10b's enqueue call. No action check here — `enqueue_pending`
    self-gates on `disposition.action`, exactly as the training-pair
    store does.

    Transaction boundary is the orchestrator's, not the store's — 10b's
    `enqueue_pending` never commits or rolls back, so this call commits
    on success and rolls back on failure, mirroring `resolve_match`/
    `create_new_entity` in `core/graph/resolution.py`.
    """
    top = disposition.top_match
    if top is None:
        # Disposition's own contract guarantees top_match is non-None
        # whenever action != NO_MATCH. Returning None here instead would
        # silently drop a queued entity: the run's queued_for_review
        # bucket would still count it, but no pending_decisions row would
        # exist, breaking the persisted-rows == queued-count invariant
        # with no failure. Fail loudly, matching the AUTO_APPROVE branch's
        # `assert top is not None` and the LLM_FALLBACK dispatch guard.
        raise RuntimeError(
            "Stage 6 QUEUE_FOR_REVIEW dispatch received a disposition with "
            "top_match=None; Disposition guarantees top_match is None only "
            "when action == 'NO_MATCH'"
        )
    try:
        proposal = _build_confirmed_proposal(entity, top.canonical_id, top.score, reasoning_trace)
        pending_id = enqueue_pending(conn, disposition, entity, proposal, tenant_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return pending_id


def match(incoming: NormalizedEntity, ctx: MatchContext) -> MatchResult:
    """Run the full Stage 1-6 pipeline for one incoming entity."""
    # ---- Stage 1: Deterministic Match --------------------------------
    hit = deterministic_match(incoming, ctx.conn, ctx.tenant_id)
    if hit is not None:
        reasoning_trace = (
            f"Stage 1 deterministic match via {hit.match_key_type} "
            f"(confidence={hit.confidence:.4f})"
        )
        synthetic_disposition = Disposition(
            source_entity_id=incoming.source_id,
            action="AUTO_APPROVE",
            top_match=None,
            candidates_ranked=(),
            cluster_conflict=False,
            llm_assessment=None,
            tenant_id=ctx.tenant_id,
        )
        canonical_id = _write_confirmed_match(
            ctx.conn,
            synthetic_disposition,
            incoming,
            hit.canonical_id,
            hit.confidence,
            ctx.tenant_id,
            reasoning_trace,
        )
        return MatchResult(
            source_entity_id=incoming.source_id,
            canonical_id=canonical_id,
            confidence=hit.confidence,
            match_type="deterministic",
            action="AUTO_APPROVE",
            signal_breakdown=None,
            disposition=None,
            reasoning_trace=reasoning_trace,
        )

    # ---- Index rebuild policy, then Stage 2: Blocking -----------------
    _rebuild_indices_if_stale(ctx)

    candidate_set = generate_candidates(
        incoming,
        ctx.token_index,
        ctx.ngram_index,
        ctx.conn,
        ctx.tenant_id,
        ctx.embedding_index,
    )
    if not candidate_set.candidates:
        reasoning_trace = (
            "Stage 2 blocking returned no candidates; creating new canonical entity"
        )
        synthetic_disposition = Disposition(
            source_entity_id=incoming.source_id,
            action="NO_MATCH",
            top_match=None,
            candidates_ranked=(),
            cluster_conflict=False,
            llm_assessment=None,
            tenant_id=ctx.tenant_id,
        )
        canonical_id = _write_new_entity(
            ctx.conn, synthetic_disposition, incoming, ctx.tenant_id, reasoning_trace
        )
        return MatchResult(
            source_entity_id=incoming.source_id,
            canonical_id=canonical_id,
            confidence=0.0,
            match_type="new",
            action="NO_MATCH",
            signal_breakdown=None,
            disposition=None,
            reasoning_trace=reasoning_trace,
        )

    # ---- Stage 3: Pairwise Scoring, Stage 4: Threshold -----------------
    scored = score_candidate_set(incoming, candidate_set, ctx.conn, ctx.tenant_id)
    disposition = apply_thresholds(incoming.source_id, scored, ctx.conn, ctx.tenant_id)

    # ---- Stage 5: LLM Fallback (only when Stage 4 routed there) -------
    via_llm = False
    reasoning_trace: str
    if disposition.action == "LLM_FALLBACK":
        via_llm = True
        try:
            disposition = llm_assess(
                disposition, incoming, ctx.conn, ctx.tenant_id, client=ctx.llm_client
            )
            assessment = disposition.llm_assessment
            reasoning_trace = (
                f"Stage 5 LLM assessment: match={assessment.match} "
                f"llm_confidence={assessment.llm_confidence:.4f}"
                if assessment is not None
                else "Stage 5 LLM assessment complete"
            )
        except (LLMNotConfiguredError, LLMBudgetExceededError) as exc:
            # Stage 5 unavailable/exhausted: record as queued for review
            # with no assessment; the batch must never abort.
            disposition = replace(disposition, action="QUEUE_FOR_REVIEW")
            reasoning_trace = (
                f"Stage 5 LLM fallback unavailable ({exc.__class__.__name__}); "
                "queued for review with no assessment"
            )
    else:
        top = disposition.top_match
        if top is not None:
            reasoning_trace = (
                f"Stage 3/4 scored match: canonical_id={top.canonical_id} "
                f"score={top.score:.4f} action={disposition.action}"
            )
        else:
            reasoning_trace = f"Stage 4 disposition: action={disposition.action}"

    # ---- Stage 6: Resolution / Graph Update ----------------------------
    action: Action = disposition.action
    canonical_id: Optional[str] = None
    if action == "AUTO_APPROVE":
        top = disposition.top_match
        assert top is not None  # Disposition's own contract
        canonical_id = _write_confirmed_match(
            ctx.conn, disposition, incoming, top.canonical_id, top.score, ctx.tenant_id, reasoning_trace
        )
    elif action == "QUEUE_FOR_REVIEW":
        _write_queued(ctx.conn, disposition, incoming, ctx.tenant_id, reasoning_trace)
    elif action == "NO_MATCH":
        canonical_id = _write_new_entity(
            ctx.conn, disposition, incoming, ctx.tenant_id, reasoning_trace
        )
    elif action == "LLM_FALLBACK":
        # Unreachable on any live path: Stage 5 always converts
        # LLM_FALLBACK to QUEUE_FOR_REVIEW, on both its success and its
        # two documented failure modes. A defensive guard, not a real
        # fourth outcome.
        raise RuntimeError(
            "Stage 6 dispatch received action='LLM_FALLBACK'; Stage 5 must "
            "resolve this to QUEUE_FOR_REVIEW before dispatch"
        )
    else:
        raise ValueError(f"unhandled Disposition.action {action!r}")

    match_type: MatchType = "llm" if via_llm else ("none" if action == "NO_MATCH" else "scored")
    top = disposition.top_match
    confidence = top.score if top is not None else 0.0
    signal_breakdown = top.signal_breakdown if top is not None else None

    return MatchResult(
        source_entity_id=incoming.source_id,
        canonical_id=canonical_id,
        confidence=confidence,
        match_type=match_type,
        action=action,
        signal_breakdown=signal_breakdown,
        disposition=disposition,
        reasoning_trace=reasoning_trace,
    )
