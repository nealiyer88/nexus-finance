"""Pipeline Stage 5: PII redaction for the LLM fallback.

The security boundary in this module is **default-deny**: the public
`redact_org` and `redact_person` functions take only allow-listed
primitives (categories, entity types, structural code shapes, role
strings, integer token counts, a score). They never receive a
`NormalizedEntity`, a `raw_record`, a `Dict[str, Any]` attributes
payload, or any string that has not already been reduced to its
structural shape upstream. Future maintainers cannot leak fields the
redactor never receives.

Layered on top: `leak_check` is a runtime second-pass guard. Stage 5
(`core.matching.llm_fallback`) calls it on the produced prompt before
sending to the LLM AND on the LLM's response reasoning before
persistence. The caller assembles `forbidden_tokens` from the source
entity's name, aliases, emails, employee_ids, plus the candidate's
canonical_name, aliases, and any email / employee_id in
`system_references.external_fields`. If a forbidden token is detected
in either direction, the prompt is rejected (programmer error in the
redactor) or the reasoning is replaced (LLM output leak).

Known V1 limitations (documented, not implemented):
- NFKC unicode normalization / homoglyph detection (e.g. Cyrillic 'е'
  vs Latin 'e'). Re-evaluate when real customer data lands.
- Recursive scrub of free-form attribute payloads — out of scope
  because the default-deny architecture refuses to accept them.

This module is a pure function. No SQLite, no env reads, no SDK
imports, no logging.
"""

from __future__ import annotations

import re
from typing import Optional

from core.matching.types import RedactedPrompt


# Minimum substring length for the leak check — single-character / two-character
# tokens (e.g. a one-letter middle initial) would over-fire and never let any
# templated phrase through. The leak guard is a belt-and-suspenders runtime
# check; the architectural guarantee is the default-deny signature.
_MIN_TOKEN_LENGTH: int = 3


_CLASS_CODE_RE = re.compile(r"^[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+$")
_PROJECT_CODE_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$")


def _shape_class_code(value: Optional[str]) -> Optional[str]:
    """Collapse a class code like ``Commercial.NI.Sands`` to ``X.Y.Z``.

    Returns None for None / empty / non-matching inputs. The returned
    shape uses A, B, C, ... as segment placeholders to preserve the
    segment-count signal without leaking any literal characters.
    """
    if not value:
        return None
    if not _CLASS_CODE_RE.match(value):
        return None
    segments = value.split(".")
    placeholders = []
    for i in range(len(segments)):
        # A, B, C, ... Z, AA, AB ...
        if i < 26:
            placeholders.append(chr(ord("A") + i))
        else:
            placeholders.append(chr(ord("A") + i // 26 - 1) + chr(ord("A") + i % 26))
    return ".".join(placeholders)


def _shape_project_code(value: Optional[str]) -> Optional[str]:
    """Collapse a project code like ``CEN-GENAI-SOW3`` to ``AAA-BBBBB-NNNN``.

    Each segment becomes a run of `A`s sized to the segment's letter
    count followed by a run of `N`s sized to its digit count. Dashes
    preserved. Returns None for None / empty / non-matching inputs.
    """
    if not value:
        return None
    if not _PROJECT_CODE_RE.match(value):
        return None
    shaped_segments = []
    for segment in value.split("-"):
        letter_count = sum(1 for ch in segment if ch.isalpha())
        digit_count = sum(1 for ch in segment if ch.isdigit())
        shaped_segments.append("A" * letter_count + "N" * digit_count)
    return "-".join(shaped_segments)


def leak_check(text: str, forbidden_tokens: frozenset[str]) -> Optional[str]:
    """Return the first forbidden token found as a case-insensitive
    substring of `text`, or None if `text` is clean.

    Tokens with fewer than `_MIN_TOKEN_LENGTH` characters are skipped to
    avoid false positives on bare initials and common digrams. The
    architectural guarantee is the default-deny signature on
    `redact_org` / `redact_person`; this function is a runtime
    second-pass check that catches caller mistakes.
    """
    if not text or not forbidden_tokens:
        return None
    haystack = text.lower()
    for token in forbidden_tokens:
        if not token:
            continue
        needle = token.lower().strip()
        if len(needle) < _MIN_TOKEN_LENGTH:
            continue
        if needle in haystack:
            return token
    return None


def redact_org(
    source_category: str,
    candidate_category: str,
    source_entity_type: str,
    candidate_entity_type: str,
    class_code_shape: Optional[str],
    project_code_shape: Optional[str],
    token_overlap_count: int,
    token_total: int,
    score: float,
    forbidden_tokens: frozenset[str],
) -> RedactedPrompt:
    """Build the LLM prompt for an organizational entity pair.

    All literal identifiers (names, class-code values, project-code
    values) MUST have been shaped or stripped by the caller. This
    function receives only categories, entity types, the structural
    code shapes (e.g. ``X.Y.Z``), token-overlap counts, and the score.
    """
    parts: list[str] = []
    parts.append(
        f"Entity A is an organization from the {source_category} category "
        f"(type: {source_entity_type})."
    )
    parts.append(
        f"Entity B is an organization from the {candidate_category} category "
        f"(type: {candidate_entity_type})."
    )
    if class_code_shape:
        parts.append(f"Entity A has a class-code pattern of {class_code_shape}.")
    if project_code_shape:
        parts.append(f"Entity B has a project-code pattern of {project_code_shape}.")
    parts.append(
        f"Normalized-token overlap: {token_overlap_count}/{token_total} tokens match."
    )
    parts.append(f"Stage 3 pairwise score: {score:.2f}.")
    parts.append("Are Entity A and Entity B the same organization?")
    text = " ".join(parts)
    return RedactedPrompt(
        category_pair=(source_category, candidate_category),
        text=text,
        forbidden_tokens=forbidden_tokens,
    )


def redact_person(
    source_category: str,
    candidate_category: str,
    source_role: Optional[str],
    candidate_role: Optional[str],
    name_inversion_detected: bool,
    token_overlap_count: int,
    token_total: int,
    score: float,
    forbidden_tokens: frozenset[str],
) -> RedactedPrompt:
    """Build the LLM prompt for a person entity pair.

    No identifying information is received: names, emails, employee_ids
    are not part of the signature. The prompt mentions only categories,
    optional role strings (which should themselves carry no name —
    e.g., "engineer", "principal", "manager"), token-overlap counts,
    name-inversion bool, and the score.
    """
    parts: list[str] = []
    role_a = source_role or "unknown"
    role_b = candidate_role or "unknown"
    parts.append(
        f"Person A is from the {source_category} category with role={role_a}."
    )
    parts.append(
        f"Person B is from the {candidate_category} category with role={role_b}."
    )
    if name_inversion_detected:
        parts.append(
            "A name-inversion pattern was detected between the two normalized forms "
            "(e.g., 'Last, First' on one side vs 'First Last' on the other)."
        )
    parts.append(
        f"Normalized-token overlap: {token_overlap_count}/{token_total} tokens match."
    )
    parts.append(f"Stage 3 pairwise score: {score:.2f}.")
    parts.append("Are Person A and Person B the same individual?")
    text = " ".join(parts)
    return RedactedPrompt(
        category_pair=(source_category, candidate_category),
        text=text,
        forbidden_tokens=forbidden_tokens,
    )
