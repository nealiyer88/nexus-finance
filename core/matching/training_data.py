"""Pipeline Stage 6 training-data capture.

`store_training_pair` writes at most one row to the SQLite
`llm_training_data` table (shipped with feature 9,
`db/migrations/002_llm_training_data_sqlite.sql`) — the only
training-capture table any code writes to. It does NOT reuse
`core.matching.llm_fallback._write_training_row` (that helper commits
internally, which would break Stage 6's transaction atomicity); it
issues its own `conn.execute(INSERT ...)` with the identical column
list and never calls `conn.commit()` / `conn.rollback()` — the
transaction boundary belongs exclusively to `core.graph.resolution`.

Capture is LLM-gated: a row is written only when `pair.entity_pair`
carries a `"source_call_id"` key referencing an existing Stage 5
`llm_training_data` row (i.e. the disposition this pair captures
actually went through `core.matching.llm_fallback.llm_assess`).
Callers (`core.graph.resolution`) are responsible for setting
`entity_pair["source_call_id"]` to `disposition.llm_assessment.call_id`
when present, or omitting/None-ing it otherwise. Auto-approved and
NO_MATCH dispositions therefore produce no training row.

No new redaction path: the persisted `redacted_prompt` is a verbatim
copy of the Stage 5 row recovered by `source_call_id` — never a new
`redact_org` / `redact_person` call. `leak_check` (the only function
imported from `core.matching.redaction`) is a second-pass guard run
over BOTH persisted text columns before the INSERT.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from core.matching.redaction import leak_check


# Mirrors `llm_fallback._MIN_FORBIDDEN_TOKEN_LENGTH` /
# `redaction._MIN_TOKEN_LENGTH`. Tokens shorter than this are dropped —
# a 2-char fragment would spuriously fire `leak_check` on prose that
# legitimately passed Stage 5.
_MIN_FORBIDDEN_TOKEN_LENGTH: int = 3

# Allow-listed `entity_pair` string keys walked when building the
# leak-check forbidden-token set. `entity_pair` is free-form (built by
# `core.graph.resolution`); only these keys are treated as identifying.
_FORBIDDEN_TOKEN_STRING_KEYS: tuple[str, ...] = (
    "incoming_entity_raw",
    "incoming_entity_normalized",
    "incoming_email",
    "incoming_employee_id",
    "candidate_canonical_name",
    "candidate_email",
    "candidate_employee_id",
)


@dataclass(frozen=True)
class TrainingPair:
    """Structured training pair captured at Stage 6.

    `entity_pair` carries the raw (unredacted) identifying context —
    used ONLY to derive the `call_id` hash and the forbidden-token set;
    it is NEVER serialized into `llm_response_json`. The other five
    fields are the non-identifying payload that IS persisted.
    """

    entity_pair: Any
    signal_breakdown: Any
    graph_evidence: Any
    category_pair: str
    disposition: str
    reasoning_trace: str


def _build_forbidden_tokens(entity_pair: Any) -> frozenset[str]:
    """Derive the leak-check forbidden-token set from a raw `entity_pair`.

    Walks the allow-listed string keys plus the `candidate_aliases`
    list, splitting each value into whitespace tokens (mirrors
    `llm_fallback._build_forbidden_tokens`). Non-dict input yields an
    empty set.
    """
    if not isinstance(entity_pair, dict):
        return frozenset()

    tokens: set[str] = set()
    for key in _FORBIDDEN_TOKEN_STRING_KEYS:
        value = entity_pair.get(key)
        if isinstance(value, str) and value.strip():
            tokens.add(value)
            tokens.update(value.split())

    aliases = entity_pair.get("candidate_aliases")
    if isinstance(aliases, (list, tuple)):
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                tokens.add(alias)
                tokens.update(alias.split())

    return frozenset(
        token
        for token in tokens
        if isinstance(token, str) and len(token.strip()) >= _MIN_FORBIDDEN_TOKEN_LENGTH
    )


def _to_jsonable(value: Any) -> Any:
    """Best-effort conversion of a dataclass (or plain value) to a
    JSON-serializable shape. `SignalBreakdown` / `GraphEvidence` (and
    their nested `BoostEntry` tuples) are frozen dataclasses; plain
    dicts / strings pass through unchanged.
    """
    if hasattr(value, "__dataclass_fields__"):
        import dataclasses

        return dataclasses.asdict(value)
    return value


def store_training_pair(
    conn: sqlite3.Connection,
    pair: TrainingPair,
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Write at most one row to `llm_training_data` for `pair`.

    Returns the written `call_id`, or `None` when no row was written
    (no Stage 5 prompt for this pair, or its `source_call_id` has no
    matching Stage 5 row). Raises `ValueError` if a forbidden token is
    found in either persisted column — a leak is programmer error.
    """
    entity_pair = pair.entity_pair if isinstance(pair.entity_pair, dict) else {}
    source_call_id = entity_pair.get("source_call_id")
    if not source_call_id:
        return None

    stage5_row = conn.execute(
        """
        SELECT redacted_prompt, prompt_sha256, category_pair
          FROM llm_training_data
         WHERE call_id = ?
        """,
        (source_call_id,),
    ).fetchone()
    if stage5_row is None:
        return None
    redacted_prompt, prompt_sha256, _stage5_category_pair = stage5_row

    canonical_id = entity_pair.get("canonical_id")
    incoming_entity_raw = entity_pair.get("incoming_entity_raw")
    iso_timestamp = datetime.now(timezone.utc).isoformat()
    call_id = "resolution:" + hashlib.sha256(
        f"{canonical_id}|{incoming_entity_raw}|{iso_timestamp}".encode("utf-8")
    ).hexdigest()[:32]

    payload = {
        "signal_breakdown": _to_jsonable(pair.signal_breakdown),
        "graph_evidence": _to_jsonable(pair.graph_evidence),
        "category_pair": pair.category_pair,
        "disposition": pair.disposition,
        "reasoning_trace": pair.reasoning_trace,
        "entity_pair_ref": {"canonical_id": canonical_id, "source_call_id": source_call_id},
    }
    llm_response_json = json.dumps(payload, sort_keys=True)

    forbidden_tokens = _build_forbidden_tokens(entity_pair)
    for column_name, column_value in (
        ("redacted_prompt", redacted_prompt),
        ("llm_response_json", llm_response_json),
    ):
        leaked = leak_check(column_value, forbidden_tokens)
        if leaked is not None:
            raise ValueError(
                f"store_training_pair: forbidden token leaked into {column_name}; aborting write"
            )

    conn.execute(
        """
        INSERT INTO llm_training_data (
            call_id, tenant_id, category_pair,
            redacted_prompt, prompt_sha256, llm_response_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (call_id, tenant_id, pair.category_pair, redacted_prompt, prompt_sha256, llm_response_json),
    )
    return call_id
