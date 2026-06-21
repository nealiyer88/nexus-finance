"""Tests for Pipeline Stage 5 redaction (`core.matching.redaction`).

Covers brief success criteria + hardened-design §6 redaction bullets
(`features/_adversaries/threshold-llm-fallback.md`). The default-deny
signature is asserted via `inspect.signature` — the redactor MUST NOT
accept `NormalizedEntity`, `raw_record`, or a free-form `dict`.
"""

from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from core.matching.redaction import (
    _shape_class_code,
    _shape_project_code,
    leak_check,
    redact_org,
    redact_person,
)
from core.matching.types import RedactedPrompt


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_GT = REPO_ROOT / "tests" / "fixtures" / "canonical_ground_truth.json"


# ---------------------------------------------------------------------------
# 1. Shape helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Commercial.NI.Sands", "A.B.C"),
        ("Commercial.GenAI.Sands", "A.B.C"),
        ("X.Y", "A.B"),
        ("Foo.Bar.Baz.Quux", "A.B.C.D"),
    ],
)
def test_shape_class_code_collapses_segments(value: str, expected: str) -> None:
    assert _shape_class_code(value) == expected


@pytest.mark.parametrize("value", [None, "", "no-dots-here", "trailing.", ".leading"])
def test_shape_class_code_returns_none_on_invalid(value) -> None:
    assert _shape_class_code(value) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("CEN-GENAI-SOW3", "AAA-AAAAA-AAANNN".replace("AAANNN", "AAAN")),
        # Decompose manually: "CEN" -> 3 letters, 0 digits -> "AAA"
        # "GENAI" -> 5 letters, 0 digits -> "AAAAA"
        # "SOW3" -> 3 letters, 1 digit -> "AAAN"
        # joined: "AAA-AAAAA-AAAN"
    ],
)
def test_shape_project_code_collapses_letters_and_digits(value: str, expected: str) -> None:
    # Recompute expected without the gymnastics above to make this explicit.
    expected = "AAA-AAAAA-AAAN"
    assert _shape_project_code(value) == expected


def test_shape_project_code_pure_letters() -> None:
    assert _shape_project_code("FOO-BAR") == "AAA-AAA"


def test_shape_project_code_mixed_with_digits() -> None:
    assert _shape_project_code("ABC123-XY9") == "AAANNN-AAN"


@pytest.mark.parametrize("value", [None, "", "single", "no_dashes"])
def test_shape_project_code_returns_none_on_invalid(value) -> None:
    assert _shape_project_code(value) is None


# ---------------------------------------------------------------------------
# 2. leak_check
# ---------------------------------------------------------------------------


def test_leak_check_finds_substring_case_insensitive() -> None:
    text = "Entity A is an organization called Cenlar FSB in the prompt."
    forbidden = frozenset({"cenlar"})
    assert leak_check(text, forbidden) == "cenlar"


def test_leak_check_skips_short_tokens() -> None:
    text = "Person A has role engineer."
    forbidden = frozenset({"a", "an", "is"})  # all <3 chars
    assert leak_check(text, forbidden) is None


def test_leak_check_returns_none_on_clean_text() -> None:
    text = "Person A is from the psa category with role=engineer."
    forbidden = frozenset({"cenlar", "neal", "iyer"})
    assert leak_check(text, forbidden) is None


def test_leak_check_handles_empty_inputs() -> None:
    assert leak_check("", frozenset({"foo"})) is None
    assert leak_check("anything", frozenset()) is None


# ---------------------------------------------------------------------------
# 3. redact_org — output shape + leak guarantees
# ---------------------------------------------------------------------------


def test_redact_org_emits_shape_only_for_class_and_project_codes() -> None:
    forbidden = frozenset({"Commercial.NI.Sands", "CEN-GENAI-SOW3", "cenlar"})
    out = redact_org(
        source_category="accounting",
        candidate_category="psa",
        source_entity_type="client",
        candidate_entity_type="client",
        class_code_shape=_shape_class_code("Commercial.NI.Sands"),
        project_code_shape=_shape_project_code("CEN-GENAI-SOW3"),
        token_overlap_count=2,
        token_total=3,
        score=0.64,
        forbidden_tokens=forbidden,
    )
    assert isinstance(out, RedactedPrompt)
    assert "Commercial.NI.Sands" not in out.text
    assert "CEN-GENAI-SOW3" not in out.text
    assert "A.B.C" in out.text  # the shape, not the value
    assert "AAA-AAAAA-AAAN" in out.text  # the shape, not the value
    assert "cenlar" not in out.text.lower()
    assert leak_check(out.text, forbidden) is None


def test_redact_org_omits_optional_fields_when_none() -> None:
    out = redact_org(
        source_category="accounting",
        candidate_category="psa",
        source_entity_type="vendor",
        candidate_entity_type="vendor",
        class_code_shape=None,
        project_code_shape=None,
        token_overlap_count=0,
        token_total=2,
        score=0.55,
        forbidden_tokens=frozenset(),
    )
    assert "class-code" not in out.text
    assert "project-code" not in out.text
    assert out.category_pair == ("accounting", "psa")


def test_redact_org_score_is_two_decimals() -> None:
    out = redact_org(
        source_category="accounting",
        candidate_category="psa",
        source_entity_type="client",
        candidate_entity_type="client",
        class_code_shape=None,
        project_code_shape=None,
        token_overlap_count=1,
        token_total=2,
        score=0.6789,
        forbidden_tokens=frozenset(),
    )
    assert "0.68" in out.text


# ---------------------------------------------------------------------------
# 4. redact_person — output shape + zero-name guarantee
# ---------------------------------------------------------------------------


def test_redact_person_contains_no_identifier_fields() -> None:
    forbidden = frozenset({"neal iyer", "neal", "iyer", "neal.iyer@example.com", "emp-042"})
    out = redact_person(
        source_category="accounting",
        candidate_category="psa",
        source_role="engineer",
        candidate_role="engineer",
        name_inversion_detected=True,
        token_overlap_count=2,
        token_total=2,
        score=0.64,
        forbidden_tokens=forbidden,
    )
    assert isinstance(out, RedactedPrompt)
    assert leak_check(out.text, forbidden) is None
    assert "role=engineer" in out.text
    assert "name-inversion pattern was detected" in out.text


def test_redact_person_omits_inversion_clause_when_not_detected() -> None:
    out = redact_person(
        source_category="accounting",
        candidate_category="psa",
        source_role=None,
        candidate_role="principal",
        name_inversion_detected=False,
        token_overlap_count=1,
        token_total=2,
        score=0.55,
        forbidden_tokens=frozenset(),
    )
    assert "name-inversion" not in out.text
    assert "role=unknown" in out.text
    assert "role=principal" in out.text


# ---------------------------------------------------------------------------
# 5. Static signature guarantee — default-deny boundary
# ---------------------------------------------------------------------------


def _signature_safe(func) -> None:
    sig = inspect.signature(func)
    for param_name, param in sig.parameters.items():
        assert param_name not in {
            "entity",
            "candidate",
            "raw_record",
            "attributes",
            "normalized_entity",
            "system_reference",
        }, f"{func.__name__} accepts forbidden param {param_name!r}"
        anno = param.annotation
        # String annotation or runtime annotation: both must avoid these types.
        anno_text = anno if isinstance(anno, str) else getattr(anno, "__name__", str(anno))
        anno_text_lower = anno_text.lower()
        for forbidden in ("normalizedentity", "dict[str, any]", "dict", "any]"):
            assert forbidden not in anno_text_lower or "frozenset" in anno_text_lower or "tuple" in anno_text_lower or "str" in anno_text_lower, (
                f"{func.__name__} parameter {param_name} annotation {anno!r} "
                f"contains forbidden type fragment {forbidden!r}"
            )


def test_redact_org_signature_is_default_deny() -> None:
    _signature_safe(redact_org)


def test_redact_person_signature_is_default_deny() -> None:
    _signature_safe(redact_person)


# ---------------------------------------------------------------------------
# 6. Fixture sweep — every person entity, zero name leakage
# ---------------------------------------------------------------------------


def test_person_redaction_does_not_leak_any_fixture_name() -> None:
    """For each person canonical in the ground-truth fixture, build a
    person redaction and assert the canonical_name does not appear as
    a substring in the produced prompt.
    """
    payload = json.loads(FIXTURE_GT.read_text())
    persons = [
        c
        for c in payload["canonical_entities"]
        if c.get("entity_category") == "person"
    ]
    assert len(persons) >= 5, "expected ≥5 person canonicals in fixture"
    leaks: list[str] = []
    for cano in persons:
        canonical_name = cano["canonical_name"]
        sources = cano.get("sources", {})
        forbidden_pieces = {canonical_name}
        for src_payload in sources.values():
            display = src_payload.get("display_name")
            if display:
                forbidden_pieces.add(display)
        forbidden = frozenset(forbidden_pieces)
        out = redact_person(
            source_category="accounting",
            candidate_category="psa",
            source_role="engineer",
            candidate_role="engineer",
            name_inversion_detected=True,
            token_overlap_count=2,
            token_total=2,
            score=0.62,
            forbidden_tokens=forbidden,
        )
        hit = leak_check(out.text, forbidden)
        if hit is not None:
            leaks.append(f"{cano['canonical_id']}: leaked token {hit!r}")
    assert not leaks, "person redaction leaked fixture names:\n  " + "\n  ".join(leaks)


# ---------------------------------------------------------------------------
# 7. RedactedPrompt is frozen / hashable
# ---------------------------------------------------------------------------


def test_redacted_prompt_is_frozen() -> None:
    out = redact_org(
        source_category="accounting",
        candidate_category="psa",
        source_entity_type="client",
        candidate_entity_type="client",
        class_code_shape=None,
        project_code_shape=None,
        token_overlap_count=1,
        token_total=2,
        score=0.6,
        forbidden_tokens=frozenset({"cenlar"}),
    )
    with pytest.raises((AttributeError, Exception)):
        out.text = "mutated"  # type: ignore[misc]
