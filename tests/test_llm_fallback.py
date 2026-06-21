"""Tests for Pipeline Stage 5 (`core.matching.llm_fallback`).

Covers hardened-design §6 llm_fallback bullets
(`features/_adversaries/threshold-llm-fallback.md`): dispatch,
never-auto-approve, training capture, factory error, budget, tool-call
schema, reasoning scrub, Tier-3 usage.

A local `FakeLLMClient` satisfies the `LLMClient` Protocol; no test
ever constructs `anthropic.Anthropic()`.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from dataclasses import replace
from typing import Any, Optional

import pytest

from core.ingestion.normalizer import normalize_entity
from core.matching.disposition import apply_thresholds
from core.matching.llm_fallback import (
    MAX_LLM_CALLS_PER_RUN,
    TOOL_SPEC,
    LLMBudgetExceededError,
    LLMNotConfiguredError,
    LLMResponseError,
    _default_llm_client_factory,
    llm_assess,
    reset_call_budget,
)
from core.matching.types import (
    Disposition,
    GraphEvidence,
    LLMAssessment,
    ScoredMatch,
    SignalBreakdown,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
TRAINING_MIGRATION = REPO_ROOT / "db" / "migrations" / "002_llm_training_data_sqlite.sql"
FIXTURE_GT = REPO_ROOT / "tests" / "fixtures" / "canonical_ground_truth.json"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    c.executescript(TRAINING_MIGRATION.read_text())
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _reset_budget():
    """Module-level call counter must not bleed across tests."""
    reset_call_budget()
    yield
    reset_call_budget()


class FakeLLMClient:
    """30-line in-test `LLMClient` Protocol implementation.

    Returns a canned dict; records every call for assertions.
    """

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def assess(
        self,
        system_prompt: str,
        user_prompt: str,
        tool_spec: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt, tool_spec))
        return dict(self._response)


def _insert_canonical(
    conn: sqlite3.Connection,
    canonical_id: str,
    canonical_name: str = "acme",
    entity_type: str = "client",
    entity_category: str = "organization",
    tenant_id: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO canonical_entities (
            canonical_id, tenant_id, canonical_name, entity_type, entity_category, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (canonical_id, tenant_id, canonical_name, entity_type, entity_category, 0.95),
    )


def _insert_system_reference(
    conn: sqlite3.Connection,
    canonical_id: str,
    source: str,
    category: str,
    external_id: str,
    external_fields: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO system_references (
            canonical_id, source, category, external_id, external_fields
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            canonical_id,
            source,
            category,
            external_id,
            json.dumps(external_fields),
        ),
    )


def _make_scored_match(canonical_id: str, score: float) -> ScoredMatch:
    return ScoredMatch(
        canonical_id=canonical_id,
        score=score,
        signal_breakdown=SignalBreakdown(
            token_sort_ratio=0.0,
            token_set_ratio=0.0,
            partial_ratio=0.0,
            jaro_winkler=0.0,
            ngram_jaccard=0.0,
            alias_boost_fired=False,
            abbreviation_bonus_fired=False,
        ),
        graph_evidence=GraphEvidence(
            shared_person_count=0,
            shared_person_bonus=0.0,
            neighborhood_overlap_count=0,
            neighborhood_overlap_bonus=0.0,
        ),
        category_pair=("accounting", "psa"),
        weight_profile_id="default_v1",
    )


def _make_entity(display_name: str, **overrides: Any):
    base = {
        "id": overrides.pop("source_id", "QB-X"),
        "source": overrides.pop("source", "quickbooks"),
        "entity_category": overrides.pop("entity_category", "organization"),
        "display_name": display_name,
    }
    base.update(overrides)
    return normalize_entity(base)


def _llm_fallback_disposition(
    candidate_cid: str = "CAN-A",
    score: float = 0.60,
    tenant_id: Optional[str] = None,
) -> Disposition:
    return Disposition(
        source_entity_id="src-1",
        action="LLM_FALLBACK",
        top_match=_make_scored_match(candidate_cid, score),
        candidates_ranked=(_make_scored_match(candidate_cid, score),),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# 1. Factory / configuration errors
# ---------------------------------------------------------------------------


def test_default_factory_raises_when_api_key_missing(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMNotConfiguredError):
        _default_llm_client_factory()


# ---------------------------------------------------------------------------
# 2. llm_assess contract: input gating
# ---------------------------------------------------------------------------


def test_llm_assess_rejects_non_llm_fallback_action(conn: sqlite3.Connection) -> None:
    disp = Disposition(
        source_entity_id="src-1",
        action="AUTO_APPROVE",
        top_match=_make_scored_match("CAN-A", 0.95),
        candidates_ranked=(_make_scored_match("CAN-A", 0.95),),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )
    entity = _make_entity("Acme Corp")
    with pytest.raises(ValueError):
        llm_assess(disp, entity, conn, client=FakeLLMClient({"match": True, "confidence": 0.9, "reasoning": "x", "signals": []}))


def test_llm_assess_rejects_disposition_with_no_top_match(
    conn: sqlite3.Connection,
) -> None:
    disp = Disposition(
        source_entity_id="src-1",
        action="LLM_FALLBACK",
        top_match=None,
        candidates_ranked=(),
        cluster_conflict=False,
        llm_assessment=None,
        tenant_id=None,
    )
    entity = _make_entity("Acme Corp")
    with pytest.raises(ValueError):
        llm_assess(disp, entity, conn, client=FakeLLMClient({"match": True, "confidence": 0.9, "reasoning": "x", "signals": []}))


# ---------------------------------------------------------------------------
# 3. Never auto-approves; QUEUE_FOR_REVIEW even when LLM says match
# ---------------------------------------------------------------------------


def test_llm_assess_never_returns_auto_approve(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-A", "acme group")
    conn.commit()
    entity = _make_entity("Acme Corp")
    disp = _llm_fallback_disposition("CAN-A", 0.60)
    client = FakeLLMClient(
        {"match": True, "confidence": 0.99, "reasoning": "looks same", "signals": ["category"]}
    )
    out = llm_assess(disp, entity, conn, client=client)
    assert out.action == "QUEUE_FOR_REVIEW"
    assert out.llm_assessment is not None
    assert out.llm_assessment.match is True
    assert out.llm_assessment.llm_confidence == 0.99


def test_llm_confidence_not_merged_into_top_match_score(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CAN-A", "acme group")
    conn.commit()
    entity = _make_entity("Acme Corp")
    disp = _llm_fallback_disposition("CAN-A", 0.60)
    client = FakeLLMClient(
        {"match": True, "confidence": 0.99, "reasoning": "x", "signals": []}
    )
    out = llm_assess(disp, entity, conn, client=client)
    assert out.top_match is not None and out.top_match.score == 0.60
    assert out.llm_assessment is not None and out.llm_assessment.llm_confidence == 0.99


# ---------------------------------------------------------------------------
# 4. Training data capture
# ---------------------------------------------------------------------------


def test_one_training_row_per_call(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-A", "acme group")
    conn.commit()
    entity = _make_entity("Acme Corp")
    disp = _llm_fallback_disposition("CAN-A", 0.60)
    client = FakeLLMClient(
        {"match": True, "confidence": 0.55, "reasoning": "x", "signals": ["category"]}
    )
    out = llm_assess(disp, entity, conn, client=client)
    rows = conn.execute(
        "SELECT call_id, tenant_id, category_pair, redacted_prompt, prompt_sha256, llm_response_json FROM llm_training_data"
    ).fetchall()
    assert len(rows) == 1
    call_id, tenant_id, category_pair, redacted_prompt, prompt_sha256, response_json = rows[0]
    assert call_id == out.llm_assessment.call_id
    assert tenant_id is None
    # category_pair format: "{source_system_category}:{candidate_system_category}"
    assert category_pair == "accounting:psa"
    assert redacted_prompt  # non-empty
    assert prompt_sha256 == out.llm_assessment.prompt_sha256
    persisted = json.loads(response_json)
    assert persisted["match"] is True
    assert persisted["confidence"] == 0.55


def test_tenant_id_propagated_to_training_row(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-A", "acme group", tenant_id="t-abc")
    conn.commit()
    entity = _make_entity("Acme Corp")
    disp = _llm_fallback_disposition("CAN-A", 0.60, tenant_id="t-abc")
    client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "x", "signals": []}
    )
    llm_assess(disp, entity, conn, tenant_id="t-abc", client=client)
    rows = conn.execute(
        "SELECT tenant_id FROM llm_training_data"
    ).fetchall()
    assert rows == [("t-abc",)]


# ---------------------------------------------------------------------------
# 5. Budget enforcement
# ---------------------------------------------------------------------------


def test_budget_exceeded_after_max_calls(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-A", "acme group")
    conn.commit()
    entity = _make_entity("Acme Corp")
    client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "x", "signals": []}
    )
    for _ in range(MAX_LLM_CALLS_PER_RUN):
        disp = _llm_fallback_disposition("CAN-A", 0.60)
        llm_assess(disp, entity, conn, client=client)
    # Next call must raise.
    disp = _llm_fallback_disposition("CAN-A", 0.60)
    with pytest.raises(LLMBudgetExceededError):
        llm_assess(disp, entity, conn, client=client)


# ---------------------------------------------------------------------------
# 6. Response validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_response",
    [
        {"match": True, "confidence": 1.5, "reasoning": "x", "signals": []},
        {"match": True, "confidence": -0.1, "reasoning": "x", "signals": []},
        {"match": "yes", "confidence": 0.5, "reasoning": "x", "signals": []},
        {"match": True, "reasoning": "x", "signals": []},  # missing confidence
        {"match": True, "confidence": 0.5, "reasoning": 123, "signals": []},
        {"match": True, "confidence": 0.5, "reasoning": "x", "signals": "category"},
        {"match": True, "confidence": 0.5, "reasoning": "x", "signals": [1, 2]},
    ],
)
def test_malformed_response_raises_llm_response_error(
    conn: sqlite3.Connection, bad_response: dict[str, Any]
) -> None:
    _insert_canonical(conn, "CAN-A", "acme group")
    conn.commit()
    entity = _make_entity("Acme Corp")
    disp = _llm_fallback_disposition("CAN-A", 0.60)
    client = FakeLLMClient(bad_response)
    with pytest.raises(LLMResponseError):
        llm_assess(disp, entity, conn, client=client)


# ---------------------------------------------------------------------------
# 7. Outbound reasoning scrub
# ---------------------------------------------------------------------------


def test_reasoning_with_forbidden_token_is_scrubbed(conn: sqlite3.Connection) -> None:
    """LLM 'reasoning' that re-leaks the source entity's normalized_name
    is replaced with the redaction placeholder before persistence."""
    _insert_canonical(conn, "CAN-A", "cenlar group")
    conn.commit()
    entity = _make_entity("Cenlar FSB", source="quickbooks", entity_category="organization")
    disp = _llm_fallback_disposition("CAN-A", 0.60)
    client = FakeLLMClient(
        {
            "match": True,
            "confidence": 0.55,
            "reasoning": "These both look like Cenlar variants.",
            "signals": ["token_overlap"],
        }
    )
    out = llm_assess(disp, entity, conn, client=client)
    assert out.llm_assessment is not None
    assert "cenlar" not in out.llm_assessment.reasoning.lower()
    assert "redacted" in out.llm_assessment.reasoning.lower()
    # Training row also stores the scrubbed reasoning.
    (response_json,) = conn.execute(
        "SELECT llm_response_json FROM llm_training_data"
    ).fetchone()
    persisted = json.loads(response_json)
    assert "cenlar" not in persisted["reasoning"].lower()


def test_clean_reasoning_passes_through(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-A", "acme group")
    conn.commit()
    entity = _make_entity("Acme Corp")
    disp = _llm_fallback_disposition("CAN-A", 0.60)
    clean_reasoning = "Token overlap is partial and categories agree."
    client = FakeLLMClient(
        {
            "match": True,
            "confidence": 0.55,
            "reasoning": clean_reasoning,
            "signals": ["category"],
        }
    )
    out = llm_assess(disp, entity, conn, client=client)
    assert out.llm_assessment is not None
    assert out.llm_assessment.reasoning == clean_reasoning


# ---------------------------------------------------------------------------
# 8. Tool-call schema sanity
# ---------------------------------------------------------------------------


def test_tool_spec_shape() -> None:
    assert TOOL_SPEC["name"] == "submit_assessment"
    schema = TOOL_SPEC["input_schema"]
    assert schema["type"] == "object"
    props = schema["properties"]
    assert set(props.keys()) == {"match", "confidence", "reasoning", "signals"}
    assert props["match"]["type"] == "boolean"
    assert props["confidence"]["type"] == "number"
    assert props["confidence"]["minimum"] == 0.0
    assert props["confidence"]["maximum"] == 1.0
    assert props["reasoning"]["type"] == "string"
    assert props["signals"]["type"] == "array"
    assert props["signals"]["items"]["type"] == "string"
    assert set(schema["required"]) == {"match", "confidence", "reasoning", "signals"}


def test_fake_client_receives_tool_spec(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-A", "acme group")
    conn.commit()
    entity = _make_entity("Acme Corp")
    disp = _llm_fallback_disposition("CAN-A", 0.60)
    client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "x", "signals": []}
    )
    llm_assess(disp, entity, conn, client=client)
    assert len(client.calls) == 1
    system_prompt, user_prompt, tool_spec = client.calls[0]
    assert tool_spec is TOOL_SPEC
    assert "submit_assessment" in system_prompt or "Stage 5" in system_prompt or "redacted" in system_prompt.lower()
    assert "Stage 3 pairwise score" in user_prompt


# ---------------------------------------------------------------------------
# 9. Person redaction dispatch
# ---------------------------------------------------------------------------


def test_person_dispatch_when_source_is_person(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-P", "michael chen", entity_type="person", entity_category="person"
    )
    conn.commit()
    entity = _make_entity(
        "Chen, Michael", source="quickbooks", entity_category="person"
    )
    disp = _llm_fallback_disposition("CAN-P", 0.60)
    client = FakeLLMClient(
        {"match": True, "confidence": 0.6, "reasoning": "x", "signals": []}
    )
    llm_assess(disp, entity, conn, client=client)
    (prompt,) = conn.execute(
        "SELECT redacted_prompt FROM llm_training_data"
    ).fetchone()
    # Person redaction template — must not include the names.
    assert "Person A" in prompt
    assert "Person B" in prompt
    assert "michael" not in prompt.lower()
    assert "chen" not in prompt.lower()


def test_person_dispatch_when_candidate_is_person(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-P", "jane doe", entity_type="person", entity_category="person"
    )
    conn.commit()
    entity = _make_entity(
        "Acme Consulting", source="quickbooks", entity_category="organization"
    )
    disp = _llm_fallback_disposition("CAN-P", 0.60)
    client = FakeLLMClient(
        {"match": False, "confidence": 0.4, "reasoning": "x", "signals": []}
    )
    llm_assess(disp, entity, conn, client=client)
    (prompt,) = conn.execute(
        "SELECT redacted_prompt FROM llm_training_data"
    ).fetchone()
    # Strictest wins: person template applies even though source is an org.
    assert "Person A" in prompt
    assert "jane" not in prompt.lower()
    assert "doe" not in prompt.lower()


def test_org_dispatch_when_neither_side_is_person(conn: sqlite3.Connection) -> None:
    _insert_canonical(
        conn, "CAN-O", "acme group", entity_type="client", entity_category="organization"
    )
    conn.commit()
    entity = _make_entity(
        "Acme Corp", source="quickbooks", entity_category="organization"
    )
    disp = _llm_fallback_disposition("CAN-O", 0.60)
    client = FakeLLMClient(
        {"match": True, "confidence": 0.6, "reasoning": "x", "signals": []}
    )
    llm_assess(disp, entity, conn, client=client)
    (prompt,) = conn.execute(
        "SELECT redacted_prompt FROM llm_training_data"
    ).fetchone()
    assert "Entity A" in prompt
    assert "Entity B" in prompt


# ---------------------------------------------------------------------------
# 10. Tier-3 usage — Stage 4 dispositions over fixture-derived pairs
# ---------------------------------------------------------------------------


def test_tier_3_usage_under_15_percent(conn: sqlite3.Connection) -> None:
    """Synthesize one ScoredMatch tuple per fixture canonical (44 total)
    and assert that the fraction routed to LLM_FALLBACK is <0.15.

    The 44 ground-truth pairs are correctly-matched in the fixture, so
    the score we synthesize for each is sampled from the AUTO_APPROVE
    band — the test asserts that at fixture scale, even with the band
    arithmetic in play, the LLM_FALLBACK fraction stays below the
    rules §1 budget. This is a regression guard against threshold or
    band changes leaking the fallback into a wider operating range.
    """
    payload = json.loads(FIXTURE_GT.read_text())
    entries = payload["canonical_entities"]
    assert len(entries) >= 30, "fixture too small for tier-3 assertion"
    # Synthesize representative score distribution: 80% in AUTO_APPROVE,
    # 12% in QUEUE_FOR_REVIEW, 7% in LLM_FALLBACK, 1% NO_MATCH — matches
    # the V1 production target where Tier 3 stays < 15%.
    bands = []
    n = len(entries)
    for i, entry in enumerate(entries):
        canonical_id = entry["canonical_id"]
        # Deterministic distribution by index modulo 100.
        bucket = (i * 7) % 100
        if bucket < 80:
            score = 0.95
        elif bucket < 92:
            score = 0.80
        elif bucket < 99:
            score = 0.60
        else:
            score = 0.40
        candidates = (_make_scored_match(canonical_id, score),)
        disp = apply_thresholds(canonical_id, candidates, conn)
        bands.append(disp.action)
    llm_fallback_count = sum(1 for action in bands if action == "LLM_FALLBACK")
    fraction = llm_fallback_count / len(bands)
    assert fraction < 0.15, f"Tier 3 fraction {fraction:.3f} >= 0.15 ({llm_fallback_count}/{len(bands)})"


# ---------------------------------------------------------------------------
# 11. Stage 4 → Stage 5 pipe-through
# ---------------------------------------------------------------------------


def test_stage4_then_stage5_pipeline(conn: sqlite3.Connection) -> None:
    """End-to-end Stage 4 output → Stage 5 input → QUEUE_FOR_REVIEW."""
    _insert_canonical(conn, "CAN-A", "acme group")
    conn.commit()
    entity = _make_entity("Acme Corp")
    disp4 = apply_thresholds(
        "src-1", (_make_scored_match("CAN-A", 0.60),), conn
    )
    assert disp4.action == "LLM_FALLBACK"
    client = FakeLLMClient(
        {"match": True, "confidence": 0.55, "reasoning": "x", "signals": []}
    )
    disp5 = llm_assess(disp4, entity, conn, client=client)
    assert disp5.action == "QUEUE_FOR_REVIEW"
    assert disp5.llm_assessment is not None
