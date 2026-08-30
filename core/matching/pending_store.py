"""Stage 4 -> approval-queue handoff: durable `pending_decisions` rows.

`Disposition` (`core/matching/types.py`) is in-memory only — Stage 4 does
not write to SQLite. When `action == "QUEUE_FOR_REVIEW"`, this module is
what makes that decision survive a process restart: `enqueue_pending`
writes one row carrying a serialized snapshot of the `Disposition`, the
`NormalizedEntity` that produced it, and the pipeline-derived Stage 6
write arguments (`proposal`) — enough for `rehydrate` to reconstruct all
three without re-running Stages 2-3 against a graph that may have since
changed. `db/migrations/004_pending_decisions_sqlite.sql` is the DDL this
module reads and writes.

Module-level functions only, no class wrapper, matching the convention in
`core/graph/entity_store.py`: `conn: sqlite3.Connection` first,
`tenant_id: Optional[str] = None` last. Like `core/graph/entity_store.py`'s
write functions and `core/matching/training_data.py`, this module NEVER
commits or rolls back the connection it is given — the caller owns the
transaction boundary, so an approval can enqueue-and-resolve atomically
alongside `core/graph/resolution.py`'s writes.

Privacy — deliberate divergence from `core/matching/training_data.py`:
`entity_json` stores the raw, unredacted `NormalizedEntity`, `raw_record`
included. No `redact_org` / `redact_person` call, no `leak_check` gate.
The approval queue's entire product purpose is to show a human the real
names; a redacted pending row is a useless pending row. The countervailing
guard is containment, not redaction: this table is never a training
source — this module never writes to the LLM training-capture table
owned by `core/matching/training_data.py`, and that module never reads
this one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from core.graph.entity_store import _assert_tenant_scope
from core.ingestion.normalizer import NormalizedEntity
from core.matching.scoring import BoostEntry
from core.matching.types import (
    Disposition,
    GraphEvidence,
    LLMAssessment,
    ScoredMatch,
    SignalBreakdown,
)

# The one Action value this module ever enqueues. Cited against the
# `Action` Literal alias in `core/matching/types.py` — every other member
# (AUTO_APPROVE, LLM_FALLBACK, NO_MATCH) is rejected by `enqueue_pending`.
_ENQUEUABLE_ACTION: str = "QUEUE_FOR_REVIEW"

_COLUMNS: tuple[str, ...] = (
    "pending_id",
    "tenant_id",
    "decision_key",
    "status",
    "source_entity_id",
    "action",
    "top_canonical_id",
    "top_score",
    "category_pair",
    "cluster_conflict",
    "abbreviation_rescue",
    "llm_call_id",
    "entity_json",
    "disposition_json",
    "proposal_json",
    "created_at",
    "resolved_at",
    "resolved_by",
    "outcome_canonical_id",
)
_SELECT_COLUMNS_SQL: str = ", ".join(_COLUMNS)

# Feature 11 addition: the store's SQLite file path. `nexus.db` at the
# repository root is the V1 default (already `.gitignore`d via `*.db`);
# `NEXUS_STORE_PATH` overrides it, read via `os.environ.get` in the same
# precedence style as `api.middleware.tenant`'s `NEXUS_TENANT_ID`. No
# second variable, no settings object, no CLI argument.
DEFAULT_STORE_PATH: str = str(Path(__file__).resolve().parents[2] / "nexus.db")


@contextlib.contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Module-level connection provider for this store.

    Resolves the SQLite file path at call time (never at import time)
    from `NEXUS_STORE_PATH`, falling back to `DEFAULT_STORE_PATH`. Opens
    the connection, yields it, and closes it on exit — including on the
    exception path. This is the only way callers (the approvals API
    router, the approval-queue dashboard page) obtain a connection to
    this store; neither caller constructs a `sqlite3.Connection` of its
    own.
    """
    path = os.environ.get("NEXUS_STORE_PATH", DEFAULT_STORE_PATH)
    conn = sqlite3.connect(path)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class PendingDecision:
    """One `pending_decisions` row. `entity_json` / `disposition_json` /
    `proposal_json` are exposed already-parsed (via `json.loads`), not as
    raw text — `rehydrate` turns them into the live `Disposition` /
    `NormalizedEntity` / proposal-dict shapes."""

    pending_id: str
    tenant_id: Optional[str]
    decision_key: str
    status: str
    source_entity_id: str
    action: str
    top_canonical_id: Optional[str]
    top_score: Optional[float]
    category_pair: str
    cluster_conflict: bool
    abbreviation_rescue: bool
    llm_call_id: Optional[str]
    entity_json: dict[str, Any]
    disposition_json: dict[str, Any]
    proposal_json: dict[str, Any]
    created_at: str
    resolved_at: Optional[str]
    resolved_by: Optional[str]
    outcome_canonical_id: Optional[str]


# ---------------------------------------------------------------------------
# Serialization — dataclass <-> JSON-safe dict
#
# json.dumps/loads collapses every tuple to a list; `_from_dict` helpers
# restore the tuple fields the frozen dataclasses need for `==` to hold.
# `NormalizedEntity.rules_applied` is the one exception: it is a `list` on
# the live object and must be restored as a `list`, not a `tuple`.
# ---------------------------------------------------------------------------


def _signal_breakdown_to_dict(sb: SignalBreakdown) -> dict[str, Any]:
    return {
        "token_sort_ratio": sb.token_sort_ratio,
        "token_set_ratio": sb.token_set_ratio,
        "partial_ratio": sb.partial_ratio,
        "jaro_winkler": sb.jaro_winkler,
        "ngram_jaccard": sb.ngram_jaccard,
        "alias_boost_fired": sb.alias_boost_fired,
        "abbreviation_bonus_fired": sb.abbreviation_bonus_fired,
        "fasttext_cosine": sb.fasttext_cosine,
        "fasttext_available": sb.fasttext_available,
        "b_boosts": [
            {"signal_id": b.signal_id, "raw": b.raw, "applied": b.applied}
            for b in sb.b_boosts
        ],
    }


def _signal_breakdown_from_dict(d: dict[str, Any]) -> SignalBreakdown:
    return SignalBreakdown(
        token_sort_ratio=d["token_sort_ratio"],
        token_set_ratio=d["token_set_ratio"],
        partial_ratio=d["partial_ratio"],
        jaro_winkler=d["jaro_winkler"],
        ngram_jaccard=d["ngram_jaccard"],
        alias_boost_fired=d["alias_boost_fired"],
        abbreviation_bonus_fired=d["abbreviation_bonus_fired"],
        fasttext_cosine=d.get("fasttext_cosine", 0.0),
        fasttext_available=d.get("fasttext_available", False),
        b_boosts=tuple(
            BoostEntry(signal_id=b["signal_id"], raw=b["raw"], applied=b["applied"])
            for b in d.get("b_boosts", [])
        ),
    )


def _graph_evidence_to_dict(ge: GraphEvidence) -> dict[str, Any]:
    return {
        "shared_person_count": ge.shared_person_count,
        "shared_person_bonus": ge.shared_person_bonus,
        "neighborhood_overlap_count": ge.neighborhood_overlap_count,
        "neighborhood_overlap_bonus": ge.neighborhood_overlap_bonus,
    }


def _graph_evidence_from_dict(d: dict[str, Any]) -> GraphEvidence:
    return GraphEvidence(
        shared_person_count=d["shared_person_count"],
        shared_person_bonus=d["shared_person_bonus"],
        neighborhood_overlap_count=d["neighborhood_overlap_count"],
        neighborhood_overlap_bonus=d["neighborhood_overlap_bonus"],
    )


def _scored_match_to_dict(m: ScoredMatch) -> dict[str, Any]:
    return {
        "canonical_id": m.canonical_id,
        "score": m.score,
        "signal_breakdown": _signal_breakdown_to_dict(m.signal_breakdown),
        "graph_evidence": _graph_evidence_to_dict(m.graph_evidence),
        "category_pair": list(m.category_pair),
        "weight_profile_id": m.weight_profile_id,
    }


def _scored_match_from_dict(d: dict[str, Any]) -> ScoredMatch:
    return ScoredMatch(
        canonical_id=d["canonical_id"],
        score=d["score"],
        signal_breakdown=_signal_breakdown_from_dict(d["signal_breakdown"]),
        graph_evidence=_graph_evidence_from_dict(d["graph_evidence"]),
        category_pair=tuple(d["category_pair"]),
        weight_profile_id=d["weight_profile_id"],
    )


def _llm_assessment_to_dict(a: LLMAssessment) -> dict[str, Any]:
    return {
        "call_id": a.call_id,
        "match": a.match,
        "llm_confidence": a.llm_confidence,
        "reasoning": a.reasoning,
        "signals_examined": list(a.signals_examined),
        "prompt_sha256": a.prompt_sha256,
    }


def _llm_assessment_from_dict(d: dict[str, Any]) -> LLMAssessment:
    return LLMAssessment(
        call_id=d["call_id"],
        match=d["match"],
        llm_confidence=d["llm_confidence"],
        reasoning=d["reasoning"],
        signals_examined=tuple(d["signals_examined"]),
        prompt_sha256=d["prompt_sha256"],
    )


def _disposition_to_dict(disposition: Disposition) -> dict[str, Any]:
    return {
        "source_entity_id": disposition.source_entity_id,
        "action": disposition.action,
        "top_match": (
            _scored_match_to_dict(disposition.top_match)
            if disposition.top_match is not None
            else None
        ),
        "candidates_ranked": [
            _scored_match_to_dict(m) for m in disposition.candidates_ranked
        ],
        "cluster_conflict": disposition.cluster_conflict,
        "llm_assessment": (
            _llm_assessment_to_dict(disposition.llm_assessment)
            if disposition.llm_assessment is not None
            else None
        ),
        "tenant_id": disposition.tenant_id,
        "abbreviation_rescue": disposition.abbreviation_rescue,
    }


def _disposition_from_dict(d: dict[str, Any]) -> Disposition:
    return Disposition(
        source_entity_id=d["source_entity_id"],
        action=d["action"],
        top_match=(
            _scored_match_from_dict(d["top_match"])
            if d.get("top_match") is not None
            else None
        ),
        candidates_ranked=tuple(
            _scored_match_from_dict(m) for m in d.get("candidates_ranked", [])
        ),
        cluster_conflict=d["cluster_conflict"],
        llm_assessment=(
            _llm_assessment_from_dict(d["llm_assessment"])
            if d.get("llm_assessment") is not None
            else None
        ),
        tenant_id=d.get("tenant_id"),
        abbreviation_rescue=d.get("abbreviation_rescue", False),
    )


def _entity_to_dict(entity: NormalizedEntity) -> dict[str, Any]:
    return {
        "raw_name": entity.raw_name,
        "normalized_name": entity.normalized_name,
        "entity_category": entity.entity_category,
        "source": entity.source,
        "category": entity.category,
        "source_id": entity.source_id,
        "email": entity.email,
        "email_is_person": entity.email_is_person,
        "raw_record": entity.raw_record,
        "rules_applied": list(entity.rules_applied),
    }


def _entity_from_dict(d: dict[str, Any]) -> NormalizedEntity:
    return NormalizedEntity(
        raw_name=d["raw_name"],
        normalized_name=d["normalized_name"],
        entity_category=d["entity_category"],
        source=d["source"],
        category=d["category"],
        source_id=d["source_id"],
        email=d.get("email"),
        email_is_person=d["email_is_person"],
        raw_record=d.get("raw_record") or {},
        rules_applied=list(d.get("rules_applied", [])),
    )


def _compute_decision_key(
    tenant_id: Optional[str],
    source: str,
    source_id: str,
    top_canonical_id: Optional[str],
) -> str:
    """Idempotency key. Deliberately excludes score, timestamp and the
    full candidate list so ordinary score drift between pipeline runs
    does not mint a second row for the same (entity, top candidate)."""
    raw = f"{tenant_id}|{source}|{source_id}|{top_canonical_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_pending_decision(row: tuple[Any, ...]) -> PendingDecision:
    d = dict(zip(_COLUMNS, row))
    return PendingDecision(
        pending_id=d["pending_id"],
        tenant_id=d["tenant_id"],
        decision_key=d["decision_key"],
        status=d["status"],
        source_entity_id=d["source_entity_id"],
        action=d["action"],
        top_canonical_id=d["top_canonical_id"],
        top_score=d["top_score"],
        category_pair=d["category_pair"],
        cluster_conflict=bool(d["cluster_conflict"]),
        abbreviation_rescue=bool(d["abbreviation_rescue"]),
        llm_call_id=d["llm_call_id"],
        entity_json=json.loads(d["entity_json"]),
        disposition_json=json.loads(d["disposition_json"]),
        proposal_json=json.loads(d["proposal_json"]),
        created_at=d["created_at"],
        resolved_at=d["resolved_at"],
        resolved_by=d["resolved_by"],
        outcome_canonical_id=d["outcome_canonical_id"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue_pending(
    conn: sqlite3.Connection,
    disposition: Disposition,
    entity: NormalizedEntity,
    proposal: dict[str, Any],
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Write at most one `pending_decisions` row for `disposition`.

    Writes a row only when `disposition.action == "QUEUE_FOR_REVIEW"`;
    every other `Action` value returns `None` and writes nothing.

    Idempotent on `decision_key`: a live (`status='pending'`) row with
    the same key is refreshed in place (`disposition_json` /
    `proposal_json` / `top_score` / `action` updated) and its existing
    `pending_id` is returned; a terminal row with the same key is left
    untouched and `None` is returned.
    """
    if disposition.action != _ENQUEUABLE_ACTION:
        return None

    top = disposition.top_match
    if top is None:
        # Disposition's own contract (types.py docstring) guarantees
        # top_match is non-None whenever action != NO_MATCH, but this
        # module never assumes another module's invariant unchecked.
        return None

    top_canonical_id = top.canonical_id
    top_score = top.score
    category_pair = f"{top.category_pair[0]}:{top.category_pair[1]}"

    _assert_tenant_scope(conn, top_canonical_id, tenant_id)

    decision_key = _compute_decision_key(
        tenant_id, entity.source, entity.source_id, top_canonical_id
    )
    llm_call_id = (
        disposition.llm_assessment.call_id
        if disposition.llm_assessment is not None
        else None
    )
    entity_json = json.dumps(_entity_to_dict(entity), sort_keys=True)
    disposition_json = json.dumps(_disposition_to_dict(disposition), sort_keys=True)
    proposal_json = json.dumps(proposal, sort_keys=True)

    existing = conn.execute(
        "SELECT pending_id, status FROM pending_decisions WHERE decision_key = ?",
        (decision_key,),
    ).fetchone()

    if existing is not None:
        pending_id, status = existing
        if status != "pending":
            return None
        conn.execute(
            """
            UPDATE pending_decisions
               SET action = ?, top_canonical_id = ?, top_score = ?, category_pair = ?,
                   cluster_conflict = ?, abbreviation_rescue = ?, llm_call_id = ?,
                   entity_json = ?, disposition_json = ?, proposal_json = ?
             WHERE pending_id = ?
            """,
            (
                disposition.action,
                top_canonical_id,
                top_score,
                category_pair,
                int(disposition.cluster_conflict),
                int(disposition.abbreviation_rescue),
                llm_call_id,
                entity_json,
                disposition_json,
                proposal_json,
                pending_id,
            ),
        )
        return pending_id

    pending_id = "pending:" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO pending_decisions (
            pending_id, tenant_id, decision_key, status, source_entity_id, action,
            top_canonical_id, top_score, category_pair, cluster_conflict,
            abbreviation_rescue, llm_call_id, entity_json, disposition_json, proposal_json
        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pending_id,
            tenant_id,
            decision_key,
            disposition.source_entity_id,
            disposition.action,
            top_canonical_id,
            top_score,
            category_pair,
            int(disposition.cluster_conflict),
            int(disposition.abbreviation_rescue),
            llm_call_id,
            entity_json,
            disposition_json,
            proposal_json,
        ),
    )
    return pending_id


def list_pending(
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[PendingDecision]:
    """Return `status = 'pending'` rows, ordered by `top_score` DESC then
    `pending_id` ASC for deterministic paging. No filter when `tenant_id`
    is `None`; scoped to that tenant otherwise."""
    if tenant_id is None:
        rows = conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS_SQL} FROM pending_decisions
             WHERE status = 'pending'
             ORDER BY top_score DESC, pending_id ASC
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS_SQL} FROM pending_decisions
             WHERE status = 'pending' AND tenant_id = ?
             ORDER BY top_score DESC, pending_id ASC
             LIMIT ? OFFSET ?
            """,
            (tenant_id, limit, offset),
        ).fetchall()
    return [_row_to_pending_decision(row) for row in rows]


def get_pending(
    conn: sqlite3.Connection,
    pending_id: str,
    tenant_id: Optional[str] = None,
) -> Optional[PendingDecision]:
    """Return the row for `pending_id`, or `None` if absent or out of
    tenant scope."""
    if tenant_id is None:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS_SQL} FROM pending_decisions WHERE pending_id = ?",
            (pending_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS_SQL} FROM pending_decisions
             WHERE pending_id = ? AND tenant_id = ?
            """,
            (pending_id, tenant_id),
        ).fetchone()
    if row is None:
        return None
    return _row_to_pending_decision(row)


def rehydrate(
    pending: PendingDecision,
) -> tuple[Disposition, NormalizedEntity, dict[str, Any]]:
    """Pure function — no `conn`. Rebuilds the in-memory `Disposition` /
    `NormalizedEntity` / proposal-dict triple from a `PendingDecision`'s
    already-parsed JSON columns."""
    disposition = _disposition_from_dict(pending.disposition_json)
    entity = _entity_from_dict(pending.entity_json)
    proposal = pending.proposal_json
    return disposition, entity, proposal


def mark_decided(
    conn: sqlite3.Connection,
    pending_id: str,
    status: str,
    resolved_by: str,
    outcome_canonical_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """One-way terminal transition. Never deletes. Only mutates a row
    whose current `status` is still `'pending'`, so a second call (or a
    call against an already-terminal row) is a no-op — there is no
    un-decide. A `tenant_id` mismatch also leaves the row untouched."""
    resolved_at = datetime.now(timezone.utc).isoformat()
    if tenant_id is None:
        conn.execute(
            """
            UPDATE pending_decisions
               SET status = ?, resolved_at = ?, resolved_by = ?, outcome_canonical_id = ?
             WHERE pending_id = ? AND status = 'pending'
            """,
            (status, resolved_at, resolved_by, outcome_canonical_id, pending_id),
        )
    else:
        conn.execute(
            """
            UPDATE pending_decisions
               SET status = ?, resolved_at = ?, resolved_by = ?, outcome_canonical_id = ?
             WHERE pending_id = ? AND tenant_id = ? AND status = 'pending'
            """,
            (
                status,
                resolved_at,
                resolved_by,
                outcome_canonical_id,
                pending_id,
                tenant_id,
            ),
        )
