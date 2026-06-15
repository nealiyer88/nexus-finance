"""Pipeline Stage 5: LLM Fallback.

Invoked only when Stage 4 routes a disposition with
`action == "LLM_FALLBACK"` (score band 0.50-0.70). Wraps a Claude API
call in a redaction sandwich:

  1. Build allow-listed inputs from the candidate's canonical row plus
     the source `NormalizedEntity` (default-deny: extract only what the
     redactor's signature accepts).
  2. Build the `forbidden_tokens` set from BOTH sides' names, aliases,
     emails, and employee_ids — the runtime second-pass leak guard.
  3. Call `redact_org` or `redact_person` (dispatched on entity_category;
     person-grade redaction wins if either side is a person).
  4. `leak_check` the produced prompt. Programmer error if it fires.
  5. Issue the Claude `tools=[submit_assessment]` call via the injected
     `LLMClient` Protocol. Tests pass a `FakeLLMClient`; the default
     factory wraps `anthropic.Anthropic` and is the only place the SDK
     is instantiated.
  6. Validate the response shape. Out-of-range / wrong-type / missing
     fields → `LLMResponseError`.
  7. `leak_check` the LLM's `reasoning` field against the same
     `forbidden_tokens`. If it leaks, replace with a literal redaction
     placeholder; log a `redacted_leak` outcome.
  8. Persist the (call_id, tenant_id, category_pair, redacted_prompt,
     prompt_sha256, llm_response_json) row to `llm_training_data`.
  9. Return a NEW `Disposition` with `action="QUEUE_FOR_REVIEW"` (LLM
     NEVER auto-approves) and the populated `llm_assessment`.

Belt-and-suspenders: a module-level `_call_counter` capped at
`MAX_LLM_CALLS_PER_RUN` (50). Tests reset via `reset_call_budget()`.

The `anthropic` SDK and `python-dotenv` are imported lazily so this
module loads in environments where they are not installed (CI, tests
using only `FakeLLMClient`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from typing import Any, Optional, Protocol

from connectors.base import NormalizedEntity
from core.graph.entity_store import get_aliases, get_canonical_name_and_category
from core.matching.redaction import (
    _shape_class_code,
    _shape_project_code,
    leak_check,
    redact_org,
    redact_person,
)
from core.matching.types import Disposition, LLMAssessment, RedactedPrompt


log = logging.getLogger(__name__)


# Idempotent .env load: only attempted when ANTHROPIC_API_KEY is not already
# in environ AND `python-dotenv` is installed. Tests using FakeLLMClient never
# need this path; CI / live runs do.
if os.environ.get("ANTHROPIC_API_KEY") is None:
    try:
        from dotenv import load_dotenv as _load_dotenv

        _load_dotenv()
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


MAX_LLM_CALLS_PER_RUN: int = 50

REDACTED_REASONING_PLACEHOLDER: str = "[redacted: PII detected in LLM output]"

# `_MIN_FORBIDDEN_TOKEN_LENGTH` mirrors `redaction._MIN_TOKEN_LENGTH`. Tokens
# shorter than this are dropped from forbidden_tokens — bare initials and
# common digrams would over-fire and never let any templated phrase through.
_MIN_FORBIDDEN_TOKEN_LENGTH: int = 3

# Allow-listed keys for email / employee_id extraction from
# `NormalizedEntity.raw_record` and `system_references.external_fields`.
# Anything outside this set is ignored (default-deny on the input side).
_EMAIL_KEYS: tuple[str, ...] = ("email", "Email", "PrimaryEmail", "primary_email")
_EMPLOYEE_ID_KEYS: tuple[str, ...] = (
    "employee_id",
    "EmployeeId",
    "EmployeeNumber",
    "employee_number",
)
_ROLE_KEYS: tuple[str, ...] = ("role", "Role", "title", "Title", "job_title")
_CLASS_CODE_KEYS: tuple[str, ...] = ("class", "Class", "class_code", "ClassCode")
_PROJECT_CODE_KEYS: tuple[str, ...] = (
    "project_code",
    "ProjectCode",
    "project_id",
    "ProjectId",
)


# Anthropic tool-use spec used by `_AnthropicAdapter`. Exposed as a module
# constant so tests can assert its shape without constructing the SDK adapter.
TOOL_NAME: str = "submit_assessment"
TOOL_SPEC: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Submit a structured match assessment for the redacted entity pair. "
        "Set `match` to true only when the two entities are the same; "
        "`confidence` is your own [0,1] confidence; `reasoning` is a short "
        "human-readable explanation; `signals` lists the high-level signals "
        "you used (e.g., 'category', 'token_overlap')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "match": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reasoning": {"type": "string"},
            "signals": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["match", "confidence", "reasoning", "signals"],
    },
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMNotConfiguredError(RuntimeError):
    """Raised by the default factory when ANTHROPIC_API_KEY is absent."""


class LLMBudgetExceededError(RuntimeError):
    """Raised when MAX_LLM_CALLS_PER_RUN is hit."""


class LLMResponseError(RuntimeError):
    """Raised on malformed / out-of-range LLM responses, or a programmer
    error such as a redacted-prompt leak detected before send."""


# ---------------------------------------------------------------------------
# Call budget
# ---------------------------------------------------------------------------


_call_counter: int = 0


def reset_call_budget() -> None:
    """Reset the module-level call counter. Test-only helper."""
    global _call_counter
    _call_counter = 0


# ---------------------------------------------------------------------------
# LLMClient Protocol + default factory
# ---------------------------------------------------------------------------


class LLMClient(Protocol):
    """Minimal LLM client surface used by `llm_assess`.

    `assess` returns the parsed `submit_assessment` tool input dict
    (`{"match": bool, "confidence": float, "reasoning": str,
    "signals": list[str]}`). Implementations are responsible for the
    network call, JSON parsing, and tool-use selection.
    """

    def assess(
        self,
        system_prompt: str,
        user_prompt: str,
        tool_spec: dict[str, Any],
    ) -> dict[str, Any]: ...


def _default_llm_client_factory() -> LLMClient:
    """Construct the production Claude API client.

    Imports `anthropic` lazily so this module loads in environments that
    don't have the SDK installed (CI test paths using `FakeLLMClient`).
    Raises `LLMNotConfiguredError` when `ANTHROPIC_API_KEY` is absent —
    surfaces missing configuration as a specific error instead of the
    SDK's authentication error at first call.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMNotConfiguredError(
            "ANTHROPIC_API_KEY not set — required for Stage 5 LLM fallback"
        )
    try:
        import anthropic  # noqa: WPS433 — intentional lazy import
    except ImportError as exc:
        raise LLMNotConfiguredError(
            "anthropic SDK not installed — required for Stage 5 LLM fallback"
        ) from exc
    return _AnthropicAdapter(anthropic.Anthropic(api_key=api_key))


class _AnthropicAdapter:
    """Adapter wrapping the `anthropic.Anthropic` SDK client to satisfy
    `LLMClient`. Uses the latest V1 Sonnet model with a tool-use call.
    """

    DEFAULT_MODEL: str = "claude-sonnet-4-6"
    MAX_TOKENS: int = 512

    def __init__(self, client: Any) -> None:
        self._client = client

    def assess(
        self,
        system_prompt: str,
        user_prompt: str,
        tool_spec: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._client.messages.create(
            model=self.DEFAULT_MODEL,
            max_tokens=self.MAX_TOKENS,
            system=system_prompt,
            tools=[tool_spec],
            tool_choice={"type": "tool", "name": tool_spec["name"]},
            messages=[{"role": "user", "content": user_prompt}],
        )
        # Find the tool_use block carrying the structured output.
        content_blocks = getattr(response, "content", None) or []
        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use":
                input_obj = getattr(block, "input", None)
                if isinstance(input_obj, dict):
                    return input_obj
        raise LLMResponseError("LLM response contained no tool_use block")


# ---------------------------------------------------------------------------
# Allow-list extraction from NormalizedEntity / candidate row
# ---------------------------------------------------------------------------


def _first_string_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    """Return the first non-empty string value found under any of `keys`.
    Walks nested dicts one level deep (matches `external_fields` shape).
    """
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload:
            value = payload[key]
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                nested = _first_string_value(value, keys)
                if nested:
                    return nested
    return None


def _extract_source_role(entity: NormalizedEntity) -> Optional[str]:
    return _first_string_value(entity.raw_record, _ROLE_KEYS)


def _extract_source_class_code(entity: NormalizedEntity) -> Optional[str]:
    return _first_string_value(entity.raw_record, _CLASS_CODE_KEYS)


def _extract_source_project_code(entity: NormalizedEntity) -> Optional[str]:
    return _first_string_value(entity.raw_record, _PROJECT_CODE_KEYS)


def _load_candidate_system_refs(
    conn: sqlite3.Connection,
    candidate_cid: str,
) -> list[dict[str, Any]]:
    """Return parsed `external_fields` JSON payloads for the candidate."""
    rows = conn.execute(
        """
        SELECT external_fields FROM system_references
         WHERE canonical_id = ?
        """,
        (candidate_cid,),
    ).fetchall()
    payloads: list[dict[str, Any]] = []
    for (fields_json,) in rows:
        if not fields_json:
            continue
        try:
            obj = json.loads(fields_json)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            payloads.append(obj)
    return payloads


def _extract_candidate_role(
    candidate_payloads: list[dict[str, Any]],
) -> Optional[str]:
    for payload in candidate_payloads:
        role = _first_string_value(payload, _ROLE_KEYS)
        if role:
            return role
    return None


def _extract_candidate_class_code(
    candidate_payloads: list[dict[str, Any]],
) -> Optional[str]:
    for payload in candidate_payloads:
        code = _first_string_value(payload, _CLASS_CODE_KEYS)
        if code:
            return code
    return None


def _extract_candidate_project_code(
    candidate_payloads: list[dict[str, Any]],
) -> Optional[str]:
    for payload in candidate_payloads:
        code = _first_string_value(payload, _PROJECT_CODE_KEYS)
        if code:
            return code
    return None


def _build_forbidden_tokens(
    entity: NormalizedEntity,
    candidate_cid: str,
    candidate_canonical_name: str,
    candidate_payloads: list[dict[str, Any]],
    conn: sqlite3.Connection,
    tenant_id: Optional[str],
) -> frozenset[str]:
    """Build the leak-check forbidden_tokens set.

    Walks both sides of the pair: the source `NormalizedEntity`
    (normalized_name, raw_name, email, plus a small allow-list of
    employee-id-like keys in raw_record) AND the candidate
    (canonical_name, aliases from `entity_aliases`, email /
    employee_id from each `system_references.external_fields` payload).
    Tokens shorter than `_MIN_FORBIDDEN_TOKEN_LENGTH` are dropped to
    avoid over-firing on bare initials.
    """
    tokens: set[str] = set()

    # Source side
    if entity.normalized_name:
        tokens.add(entity.normalized_name)
        for word in entity.normalized_name.split():
            tokens.add(word)
    if entity.raw_name:
        tokens.add(entity.raw_name)
    if entity.email:
        tokens.add(entity.email)
    employee_id = _first_string_value(entity.raw_record, _EMPLOYEE_ID_KEYS)
    if employee_id:
        tokens.add(employee_id)

    # Candidate side
    if candidate_canonical_name:
        tokens.add(candidate_canonical_name)
        for word in candidate_canonical_name.split():
            tokens.add(word)
    for alias in get_aliases(conn, candidate_cid, tenant_id):
        tokens.add(alias)
        for word in alias.split():
            tokens.add(word)
    for payload in candidate_payloads:
        cand_email = _first_string_value(payload, _EMAIL_KEYS)
        if cand_email:
            tokens.add(cand_email)
        cand_employee_id = _first_string_value(payload, _EMPLOYEE_ID_KEYS)
        if cand_employee_id:
            tokens.add(cand_employee_id)

    return frozenset(
        token
        for token in tokens
        if isinstance(token, str) and len(token.strip()) >= _MIN_FORBIDDEN_TOKEN_LENGTH
    )


# ---------------------------------------------------------------------------
# Training-data persistence
# ---------------------------------------------------------------------------


def _write_training_row(
    conn: sqlite3.Connection,
    call_id: str,
    tenant_id: Optional[str],
    category_pair: tuple[str, str],
    redacted_prompt: str,
    prompt_sha256: str,
    llm_response_json: str,
) -> None:
    """Insert one row into `llm_training_data`. Append-only within tenant
    lifetime (DELETE permitted on offboarding per migration carve-out).
    """
    conn.execute(
        """
        INSERT INTO llm_training_data (
            call_id, tenant_id, category_pair,
            redacted_prompt, prompt_sha256, llm_response_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            tenant_id,
            f"{category_pair[0]}:{category_pair[1]}",
            redacted_prompt,
            prompt_sha256,
            llm_response_json,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


def _validate_response_shape(response: dict[str, Any]) -> None:
    """Raise `LLMResponseError` on any deviation from the tool_use schema."""
    if not isinstance(response, dict):
        raise LLMResponseError(f"LLM response must be a dict; got {type(response)!r}")
    match = response.get("match")
    confidence = response.get("confidence")
    reasoning = response.get("reasoning")
    signals = response.get("signals")
    if not isinstance(match, bool):
        raise LLMResponseError(f"`match` must be bool; got {type(match)!r}")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise LLMResponseError(
            f"`confidence` must be a numeric in [0,1]; got {confidence!r}"
        )
    if not (0.0 <= float(confidence) <= 1.0):
        raise LLMResponseError(
            f"`confidence` must be in [0,1]; got {confidence!r}"
        )
    if not isinstance(reasoning, str):
        raise LLMResponseError(f"`reasoning` must be str; got {type(reasoning)!r}")
    if not isinstance(signals, list) or not all(
        isinstance(s, str) for s in signals
    ):
        raise LLMResponseError("`signals` must be a list[str]")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def llm_assess(
    disposition: Disposition,
    entity: NormalizedEntity,
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
    client: Optional[LLMClient] = None,
) -> Disposition:
    """Run Stage 5 LLM fallback over a `LLM_FALLBACK` disposition.

    Returns a NEW `Disposition` with `action="QUEUE_FOR_REVIEW"` and
    `llm_assessment` populated. Calling with any other input action
    raises `ValueError` — Stage 5 must not run on AUTO_APPROVE,
    QUEUE_FOR_REVIEW, or NO_MATCH inputs.
    """
    global _call_counter

    if disposition.action != "LLM_FALLBACK":
        raise ValueError(
            f"llm_assess requires action='LLM_FALLBACK'; got {disposition.action!r}"
        )
    if disposition.top_match is None:
        raise ValueError("llm_assess requires a non-None disposition.top_match")

    if _call_counter >= MAX_LLM_CALLS_PER_RUN:
        raise LLMBudgetExceededError(
            f"MAX_LLM_CALLS_PER_RUN={MAX_LLM_CALLS_PER_RUN} reached"
        )
    _call_counter += 1

    resolved_client: LLMClient = client if client is not None else _default_llm_client_factory()

    top = disposition.top_match
    candidate_cid = top.canonical_id
    candidate_row = get_canonical_name_and_category(conn, candidate_cid, tenant_id)
    if candidate_row is None:
        raise LLMResponseError(
            f"candidate {candidate_cid!r} not found in canonical_entities"
        )
    candidate_name, candidate_category, candidate_entity_type = candidate_row
    candidate_payloads = _load_candidate_system_refs(conn, candidate_cid)

    # Dispatch: person-grade redaction wins if either side is a person.
    is_person_pair = (
        entity.entity_category == "person" or candidate_category == "person"
    )

    forbidden_tokens = _build_forbidden_tokens(
        entity=entity,
        candidate_cid=candidate_cid,
        candidate_canonical_name=candidate_name,
        candidate_payloads=candidate_payloads,
        conn=conn,
        tenant_id=tenant_id,
    )

    source_tokens = (
        entity.normalized_name.split() if entity.normalized_name else []
    )
    candidate_tokens = candidate_name.split() if candidate_name else []
    token_overlap_count = len(set(source_tokens) & set(candidate_tokens))
    token_total = max(len(set(source_tokens) | set(candidate_tokens)), 1)

    # System-category pair (e.g. ("accounting", "psa")) is carried on the
    # Stage 3 ScoredMatch — use it directly so we don't conflate it with
    # the canonical entity_category ("organization" | "person").
    source_sys_category, candidate_sys_category = top.category_pair

    if is_person_pair:
        # Token-set tokens already excluded the names themselves; roles are
        # already allow-listed primitives.
        redacted: RedactedPrompt = redact_person(
            source_category=source_sys_category,
            candidate_category=candidate_sys_category,
            source_role=_extract_source_role(entity),
            candidate_role=_extract_candidate_role(candidate_payloads),
            name_inversion_detected=(
                set(source_tokens) == set(candidate_tokens) and source_tokens != candidate_tokens
            ),
            token_overlap_count=token_overlap_count,
            token_total=token_total,
            score=top.score,
            forbidden_tokens=forbidden_tokens,
        )
    else:
        redacted = redact_org(
            source_category=source_sys_category,
            candidate_category=candidate_sys_category,
            source_entity_type=entity.entity_category,
            candidate_entity_type=candidate_entity_type,
            class_code_shape=_shape_class_code(_extract_source_class_code(entity)),
            project_code_shape=_shape_project_code(
                _extract_candidate_project_code(candidate_payloads)
            ),
            token_overlap_count=token_overlap_count,
            token_total=token_total,
            score=top.score,
            forbidden_tokens=forbidden_tokens,
        )

    leak = leak_check(redacted.text, redacted.forbidden_tokens)
    if leak is not None:
        # Programmer error in the redactor — never silently send.
        raise LLMResponseError(
            f"redacted prompt leaked forbidden token; aborting send"
        )

    call_id = uuid.uuid4().hex
    prompt_sha256 = hashlib.sha256(redacted.text.encode("utf-8")).hexdigest()

    system_prompt = (
        "You are an entity-matching assistant. The inputs you receive are "
        "redacted: names, emails, and identifiers have been stripped. "
        "Decide whether the two redacted entities refer to the same "
        "real-world entity. Submit your answer via the submit_assessment tool."
    )

    response = resolved_client.assess(
        system_prompt=system_prompt,
        user_prompt=redacted.text,
        tool_spec=TOOL_SPEC,
    )
    _validate_response_shape(response)

    reasoning = response["reasoning"]
    outbound_leak = leak_check(reasoning, redacted.forbidden_tokens)
    outcome = "ok"
    if outbound_leak is not None:
        outcome = "redacted_leak"
        log.warning(
            "Stage5 LLM response carried forbidden token; reasoning scrubbed",
            extra={
                "call_id": call_id,
                "prompt_sha256": prompt_sha256,
                "category_pair": f"{redacted.category_pair[0]}:{redacted.category_pair[1]}",
                "outcome": outcome,
            },
        )
        reasoning = REDACTED_REASONING_PLACEHOLDER

    persisted_response = {
        "match": response["match"],
        "confidence": float(response["confidence"]),
        "reasoning": reasoning,
        "signals": list(response["signals"]),
    }

    _write_training_row(
        conn=conn,
        call_id=call_id,
        tenant_id=tenant_id,
        category_pair=redacted.category_pair,
        redacted_prompt=redacted.text,
        prompt_sha256=prompt_sha256,
        llm_response_json=json.dumps(persisted_response, sort_keys=True),
    )

    log.info(
        "Stage5 LLM assessment complete",
        extra={
            "call_id": call_id,
            "prompt_sha256": prompt_sha256,
            "category_pair": f"{redacted.category_pair[0]}:{redacted.category_pair[1]}",
            "outcome": outcome,
        },
    )

    assessment = LLMAssessment(
        call_id=call_id,
        match=bool(response["match"]),
        llm_confidence=float(response["confidence"]),
        reasoning=reasoning,
        signals_examined=tuple(persisted_response["signals"]),
        prompt_sha256=prompt_sha256,
    )

    # LLM NEVER auto-approves. Always QUEUE_FOR_REVIEW.
    return Disposition(
        source_entity_id=disposition.source_entity_id,
        action="QUEUE_FOR_REVIEW",
        top_match=disposition.top_match,
        candidates_ranked=disposition.candidates_ranked,
        cluster_conflict=disposition.cluster_conflict,
        llm_assessment=assessment,
        tenant_id=disposition.tenant_id,
    )
