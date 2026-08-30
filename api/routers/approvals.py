"""Nexus Finance — approvals router (feature 11).

Exposes the pending-decision queue (10b's `pending_decisions` table) as
an HTTP API: list, single-item detail, and the three human decisions
(approve / reject / correct). This module opens SQLite only, through
`core.matching.pending_store.get_connection` — it never constructs a
connection of its own and never holds one open across requests.

Tenant handling: this router reads `request.state.tenant_id` exactly as
`api/routers/connectors.py` does. It parses no tenant header, reads no
tenant environment variable, and defines no fallback tenant constant —
that resolution belongs entirely to `api.middleware.tenant`, which is
not itself authentication or isolation (see that module's docstring).
This feature does not describe its own tenant-scoped reads/writes as
security or access-control boundaries of any kind — it is data shaping
only: any caller may assert any tenant.

Each write path follows one shape: `get_pending` (tenant-scoped, `None`
-> 404) -> `rehydrate(pending)` -> dispatch the Stage 6 writer
(`core.graph.resolution.resolve_match` / `reject_match`) with the
rehydrated triple plus the caller-supplied values -> `mark_decided(...)`
-> commit. The writer commits its own transaction before `mark_decided`
runs; they are not one transaction. A failure between the two leaves a
resolved graph with a still-`pending` row — visible and safe to
re-drive, since the Stage 6 writers are idempotent. `pending_store`
itself never commits or rolls back (10b's contract; unchanged here).

`create_new_entity` is never wired to any route — `/correct` resolves
to an existing canonical id only.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from core.graph.resolution import reject_match, resolve_match
from core.matching.pending_store import (
    PendingDecision,
    get_connection,
    get_pending,
    list_pending,
    mark_decided,
    rehydrate,
)

router = APIRouter(prefix="/approvals", tags=["approvals"])


# ---------------------------------------------------------------------------
# Decision-path exceptions — internal to this module, translated to HTTP
# status codes at the route layer.
# ---------------------------------------------------------------------------


class PendingNotFoundError(Exception):
    """Raised when a `pending_id` is absent or out of tenant scope."""


class TerminalRowError(Exception):
    """Raised when a decision is attempted against an already-terminal row."""


# ---------------------------------------------------------------------------
# Stage 6 argument derivation — derive, never transcribe.
# ---------------------------------------------------------------------------

_BASE_CALLER_SUPPLIED: frozenset[str] = frozenset(
    {"conn", "disposition", "entity", "reasoning_trace", "tenant_id"}
)


def _dispatch_stage6_writer(
    fn: Any,
    conn: Any,
    disposition: Any,
    entity: Any,
    reasoning_trace: str,
    tenant_id: Optional[str],
    proposal: dict[str, Any],
    extra: dict[str, Any],
) -> Any:
    """Call a Stage 6 writer (`resolve_match` / `reject_match`) with its
    caller-supplied arguments (the connection, the rehydrated
    `disposition` / `entity`, `reasoning_trace`, `tenant_id`, plus
    whatever this call site supplies via `extra` — `approved_by` on the
    approve/correct paths, `rejected_canonical_id` on the reject path)
    and every remaining required argument sourced from the rehydrated
    `proposal`. Never splats `proposal` wholesale — a duplicate-kwarg
    `TypeError` would result whenever a caller-supplied name is also a
    proposal key.
    """
    params = inspect.signature(fn).parameters
    caller_supplied = _BASE_CALLER_SUPPLIED | set(extra.keys())

    kwargs: dict[str, Any] = dict(extra)
    if "reasoning_trace" in params:
        kwargs["reasoning_trace"] = reasoning_trace
    if "tenant_id" in params:
        kwargs["tenant_id"] = tenant_id

    for name, param in params.items():
        if name in caller_supplied:
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        kwargs[name] = proposal[name]

    return fn(conn, disposition, entity, **kwargs)


# ---------------------------------------------------------------------------
# Decision logic — shared between the HTTP routes below and the
# approval-queue dashboard page's callbacks, which obtain their own
# connection via the same `get_connection` provider and call these
# directly rather than round-tripping through HTTP.
# ---------------------------------------------------------------------------


def approve_decision(
    conn: Any,
    tenant_id: Optional[str],
    pending_id: str,
    approved_by: str = "api",
    reasoning_trace: str = "",
) -> str:
    """Approve drives Stage 6 for real, from the row alone: the row's own
    top candidate, no substitution."""
    pending = get_pending(conn, pending_id, tenant_id)
    if pending is None:
        raise PendingNotFoundError(pending_id)
    if pending.status != "pending":
        raise TerminalRowError(pending_id)

    disposition, entity, proposal = rehydrate(pending)
    canonical_id = _dispatch_stage6_writer(
        resolve_match,
        conn,
        disposition,
        entity,
        reasoning_trace,
        tenant_id,
        proposal,
        {"approved_by": approved_by},
    )
    mark_decided(
        conn,
        pending_id,
        status="approved",
        resolved_by=approved_by,
        outcome_canonical_id=canonical_id,
        tenant_id=tenant_id,
    )
    conn.commit()
    return canonical_id


def reject_decision(
    conn: Any,
    tenant_id: Optional[str],
    pending_id: str,
    rejected_canonical_id: Optional[str] = None,
    reasoning_trace: str = "",
    approved_by: str = "api",
) -> str:
    """Reject validates membership of `rejected_canonical_id` in the
    rehydrated `disposition.candidates_ranked` (a `ValueError` there
    must never escape as a 500), defaulting to the row's top candidate
    when the caller supplies none."""
    pending = get_pending(conn, pending_id, tenant_id)
    if pending is None:
        raise PendingNotFoundError(pending_id)
    if pending.status != "pending":
        raise TerminalRowError(pending_id)

    disposition, entity, proposal = rehydrate(pending)
    target = (
        rejected_canonical_id
        if rejected_canonical_id is not None
        else pending.top_canonical_id
    )
    _dispatch_stage6_writer(
        reject_match,
        conn,
        disposition,
        entity,
        reasoning_trace,
        tenant_id,
        proposal,
        {"rejected_canonical_id": target},
    )
    mark_decided(
        conn,
        pending_id,
        status="rejected",
        resolved_by=approved_by,
        outcome_canonical_id=target,
        tenant_id=tenant_id,
    )
    conn.commit()
    return target


def correct_decision(
    conn: Any,
    tenant_id: Optional[str],
    pending_id: str,
    canonical_id: str,
    approved_by: str = "api",
    reasoning_trace: str = "",
) -> str:
    """Correct substitutes the human-supplied `canonical_id` for every
    `proposal` value that equals the row's `top_canonical_id`, located
    by comparing against `top_canonical_id` rather than assuming which
    field holds it, then dispatches `resolve_match`. The human's id is
    not required to appear in `candidates_ranked`."""
    pending = get_pending(conn, pending_id, tenant_id)
    if pending is None:
        raise PendingNotFoundError(pending_id)
    if pending.status != "pending":
        raise TerminalRowError(pending_id)

    disposition, entity, proposal = rehydrate(pending)
    substituted = {
        key: (canonical_id if value == pending.top_canonical_id else value)
        for key, value in proposal.items()
    }
    result_canonical_id = _dispatch_stage6_writer(
        resolve_match,
        conn,
        disposition,
        entity,
        reasoning_trace,
        tenant_id,
        substituted,
        {"approved_by": approved_by},
    )
    mark_decided(
        conn,
        pending_id,
        status="corrected",
        resolved_by=approved_by,
        outcome_canonical_id=canonical_id,
        tenant_id=tenant_id,
    )
    conn.commit()
    return result_canonical_id


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def _pending_summary(pending: PendingDecision) -> dict[str, Any]:
    return {
        "pending_id": pending.pending_id,
        "source_entity_id": pending.source_entity_id,
        "top_canonical_id": pending.top_canonical_id,
        "top_score": pending.top_score,
        "category_pair": pending.category_pair,
        "status": pending.status,
        "created_at": pending.created_at,
    }


def _pending_detail(pending: PendingDecision, disposition: Any, entity: Any) -> dict[str, Any]:
    top = disposition.top_match
    return {
        "pending_id": pending.pending_id,
        "status": pending.status,
        "source_entity_id": pending.source_entity_id,
        "top_canonical_id": pending.top_canonical_id,
        "top_score": pending.top_score,
        "category_pair": pending.category_pair,
        "created_at": pending.created_at,
        "incoming_entity_raw_name": entity.raw_name,
        "incoming_entity_normalized_name": entity.normalized_name,
        "signal_breakdown": (
            dataclasses.asdict(top.signal_breakdown) if top is not None else None
        ),
        "graph_evidence": (
            dataclasses.asdict(top.graph_evidence) if top is not None else None
        ),
        "llm_reasoning": (
            disposition.llm_assessment.reasoning
            if disposition.llm_assessment is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/pending")
def list_pending_route(request: Request, limit: int = 100, offset: int = 0) -> list:
    tenant_id = request.state.tenant_id
    with get_connection() as conn:
        rows = list_pending(conn, tenant_id=tenant_id, limit=limit, offset=offset)
    return [_pending_summary(row) for row in rows]


@router.get("/{pending_id}")
def get_pending_route(pending_id: str, request: Request) -> dict:
    tenant_id = request.state.tenant_id
    with get_connection() as conn:
        pending = get_pending(conn, pending_id, tenant_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="pending item not found")
        disposition, entity, _proposal = rehydrate(pending)
    return _pending_detail(pending, disposition, entity)


@router.post("/{pending_id}/approve")
def approve_pending_route(
    pending_id: str, request: Request, payload: dict = Body(default={})
) -> dict:
    tenant_id = request.state.tenant_id
    approved_by = payload.get("approved_by", "api")
    reasoning_trace = payload.get("reasoning_trace", "")
    with get_connection() as conn:
        try:
            canonical_id = approve_decision(
                conn, tenant_id, pending_id, approved_by, reasoning_trace
            )
        except PendingNotFoundError:
            raise HTTPException(status_code=404, detail="pending item not found")
        except TerminalRowError:
            raise HTTPException(status_code=409, detail="pending item already decided")
    return {"pending_id": pending_id, "status": "approved", "canonical_id": canonical_id}


@router.post("/{pending_id}/reject")
def reject_pending_route(
    pending_id: str, request: Request, payload: dict = Body(default={})
) -> dict:
    tenant_id = request.state.tenant_id
    approved_by = payload.get("approved_by", "api")
    reasoning_trace = payload.get("reasoning_trace", "")
    rejected_canonical_id = payload.get("rejected_canonical_id")
    with get_connection() as conn:
        try:
            target = reject_decision(
                conn,
                tenant_id,
                pending_id,
                rejected_canonical_id,
                reasoning_trace,
                approved_by,
            )
        except PendingNotFoundError:
            raise HTTPException(status_code=404, detail="pending item not found")
        except TerminalRowError:
            raise HTTPException(status_code=409, detail="pending item already decided")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"pending_id": pending_id, "status": "rejected", "rejected_canonical_id": target}


@router.post("/{pending_id}/correct")
def correct_pending_route(
    pending_id: str, request: Request, payload: dict = Body(default={})
) -> dict:
    tenant_id = request.state.tenant_id
    canonical_id = payload.get("canonical_id")
    if not canonical_id:
        raise HTTPException(status_code=400, detail="canonical_id is required")
    approved_by = payload.get("approved_by", "api")
    reasoning_trace = payload.get("reasoning_trace", "")
    with get_connection() as conn:
        try:
            result_canonical_id = correct_decision(
                conn, tenant_id, pending_id, canonical_id, approved_by, reasoning_trace
            )
        except PendingNotFoundError:
            raise HTTPException(status_code=404, detail="pending item not found")
        except TerminalRowError:
            raise HTTPException(status_code=409, detail="pending item already decided")
    return {"pending_id": pending_id, "status": "corrected", "canonical_id": result_canonical_id}
