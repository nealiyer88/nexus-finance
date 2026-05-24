"""Pipeline Stage 1: Deterministic Match.

Resolves a `NormalizedEntity` against the canonical registry via three
deterministic sub-stages, in this exact order:

    1a. Exact alias / canonical_name match  (entity_category-agnostic).
    1b. Email anchor                        (person entities only).
    1c. Employee ID anchor                  (person entities only).

Any sub-stage that yields a single canonical_id produces a `DeterministicMatch`
and short-circuits the rest. A multi-hit (collision) at any sub-stage is
treated as ambiguous: the function falls through to the next sub-stage,
ultimately returning `None`. Callers (the matcher orchestrator, not yet
implemented) pass `None` results to Stage 2 (Blocking).

Stage 1 does NOT call rapidfuzz, does NOT consult inverted indices, and
does NOT mutate the graph store.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from connectors.base import NormalizedEntity
from core.graph.entity_store import (
    get_entity_category,
    lookup_alias_exact,
    lookup_email,
    lookup_employee_id,
)
from core.matching.types import DeterministicMatch


ALIAS_EXACT_CEILING: float = 0.99
EMAIL_CONFIDENCE: float = 0.99
EMPLOYEE_ID_CONFIDENCE: float = 1.0


def deterministic_match(
    entity: NormalizedEntity,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> Optional[DeterministicMatch]:
    alias_hits = lookup_alias_exact(conn, entity.normalized_name, tenant_id)
    if len(alias_hits) == 1:
        canonical_id, stored_conf = alias_hits[0]
        confidence = min(stored_conf, ALIAS_EXACT_CEILING)
        return DeterministicMatch(
            canonical_id=canonical_id,
            confidence=confidence,
            match_key_type="alias_exact",
        )

    if entity.entity_category == "person" and entity.email:
        email_hits = lookup_email(conn, entity.email, tenant_id)
        person_hits = [
            cid for cid in email_hits if get_entity_category(conn, cid) == "person"
        ]
        if len(person_hits) == 1:
            return DeterministicMatch(
                canonical_id=person_hits[0],
                confidence=EMAIL_CONFIDENCE,
                match_key_type="email",
            )

    if entity.entity_category == "person":
        raw_employee_id = entity.raw_record.get("employee_id")
        if isinstance(raw_employee_id, (str, int)) and str(raw_employee_id).strip():
            eid_hits = lookup_employee_id(conn, str(raw_employee_id), tenant_id)
            if len(eid_hits) == 1:
                return DeterministicMatch(
                    canonical_id=eid_hits[0],
                    confidence=EMPLOYEE_ID_CONFIDENCE,
                    match_key_type="employee_id",
                )

    return None
