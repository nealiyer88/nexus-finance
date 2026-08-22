"""Tests for Pipeline Stage 3: Pairwise Scoring.

Covers brief success criteria + the 20 hardened-design test additions
(`features/_adversaries/pairwise-scoring.md` §6). Reuses the in-memory
SQLite + ground truth fixture pattern from `tests/test_blocking.py`.
"""

from __future__ import annotations

import json
import math
import pathlib
import random
import sqlite3
from typing import Any, Optional

import pytest

from core.graph.entity_store import count_amount_cooccurrence_periods
from core.ingestion.normalizer import normalize_entity
from core.matching.disposition import apply_thresholds
from core.matching.embeddings import _model_present
from core.matching.scoring import (
    MAX_B_BOOST,
    MAX_NEIGHBORHOOD_BONUS,
    MAX_SHARED_PERSON_BONUS,
    _base_weighted_score,
    _check_psa_abbreviation,
    _compute_b_boosts,
    _tokens_abbreviate,
    _weighted_score,
    ngram_jaccard,
    score_candidate_set,
    score_pair,
)
from core.matching.types import (
    CandidateEntity,
    CandidateSet,
    GraphEvidence,
    ScoredMatch,
    SignalBreakdown,
)
from core.matching.weights import (
    DEFAULT_WEIGHTS,
    PSA_ACCOUNTING_WEIGHTS,
    WeightConfig,
    get_weights,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
FIXTURE_GT = REPO_ROOT / "tests" / "fixtures" / "canonical_ground_truth.json"

_SOURCE_TO_CATEGORY = {"quickbooks": "accounting", "ruddr": "psa"}


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    try:
        yield c
    finally:
        c.close()


def _insert_canonical(
    conn: sqlite3.Connection,
    canonical_id: str,
    canonical_name: str,
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


def _insert_alias(
    conn: sqlite3.Connection,
    canonical_id: str,
    value: str,
    source: str = "canonical",
    category: str = "canonical",
    confidence: float = 0.95,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_aliases (canonical_id, value, source, category, confidence)
        VALUES (?, ?, ?, ?, ?)
        """,
        (canonical_id, value, source, category, confidence),
    )


def _insert_edge(
    conn: sqlite3.Connection,
    source_node: str,
    target_node: str,
    relationship: str = "RELATED",
    source_category: str = "accounting",
    target_category: str = "psa",
) -> None:
    conn.execute(
        """
        INSERT INTO entity_edges (
            source_node, target_node, relationship, source_category, target_category, weight
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_node, target_node, relationship, source_category, target_category, 1.0),
    )


def _insert_txn(
    conn: sqlite3.Connection,
    source: str,
    external_source_id: str,
    amount: float,
    txn_date: str,
    category: str = "accounting",
    txn_type: str = "invoice",
    currency: str = "USD",
    counterparty_source_id: Optional[str] = None,
    canonical_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    period: Optional[str] = None,
) -> None:
    """Seed a raw `transactions` row. `period` defaults to `txn_date[:7]`
    (correct derivation); tests exercising a mis-derived period pass it
    explicitly."""
    if period is None:
        period = txn_date[:7]
    conn.execute(
        """
        INSERT INTO transactions (
            tenant_id, source, category, external_source_id, txn_type,
            amount, currency, txn_date, period, counterparty_source_id,
            canonical_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id,
            source,
            category,
            external_source_id,
            txn_type,
            amount,
            currency,
            txn_date,
            period,
            counterparty_source_id,
            canonical_id,
        ),
    )


def _make_entity(
    display_name: str,
    source: str = "quickbooks",
    entity_category: str = "organization",
    source_id: str = "QB-X",
) -> Any:
    return normalize_entity(
        {
            "id": source_id,
            "source": source,
            "entity_category": entity_category,
            "display_name": display_name,
        }
    )


# ---------------------------------------------------------------------------
# 0. Import hygiene
# ---------------------------------------------------------------------------


def test_no_xgboost_no_llm_no_deep_learning_in_scoring() -> None:
    """Guard: NOT-SCOPE ML libs must not appear in scoring.py (§11 rules)."""
    scoring_src = (REPO_ROOT / "core" / "matching" / "scoring.py").read_text()
    for forbidden in ("xgboost", "anthropic", "openai", "fastembed",
                      "sentence_transformers", "torch", "transformers"):
        assert forbidden not in scoring_src, (
            f"NOT-SCOPE import '{forbidden}' found in scoring.py"
        )


# ---------------------------------------------------------------------------
# 1. Weight dispatch + WeightConfig invariants
# ---------------------------------------------------------------------------


def test_weights_sum_to_one() -> None:
    for w in (DEFAULT_WEIGHTS, PSA_ACCOUNTING_WEIGHTS):
        total = (
            w.token_sort_ratio
            + w.token_set_ratio
            + w.partial_ratio
            + w.jaro_winkler
            + w.ngram_jaccard
            + w.alias_boost
        )
        assert math.isclose(total, 1.0, abs_tol=1e-9), (
            f"profile {w.profile_id!r} weights sum to {total} (expected 1.0)"
        )


def test_dispatch_returns_psa_accounting_for_cross_pair() -> None:
    w_ap = get_weights("accounting", "psa")
    w_pa = get_weights("psa", "accounting")
    assert w_ap.profile_id == "psa_accounting_v1"
    assert w_pa.profile_id == "psa_accounting_v1"


def test_dispatch_returns_default_for_unconfigured_pair() -> None:
    w = get_weights("crm", "payments")
    assert w.profile_id == "default_v1"


def test_dispatch_selects_psa_accounting_weights() -> None:
    """Brief success criterion #4: dispatch selects different weights
    for PSA↔Accounting vs default."""
    cross = get_weights("accounting", "psa")
    other = get_weights("crm", "payments")
    assert cross.profile_id != other.profile_id
    assert cross.abbreviation_bonus > other.abbreviation_bonus


# ---------------------------------------------------------------------------
# 2. n-gram Jaccard primitive
# ---------------------------------------------------------------------------


def test_ngram_jaccard_identical_strings_returns_one() -> None:
    assert math.isclose(ngram_jaccard("cenlar fsb", "cenlar fsb"), 1.0)


def test_ngram_jaccard_empty_returns_zero() -> None:
    assert ngram_jaccard("", "anything") == 0.0
    assert ngram_jaccard("anything", "") == 0.0
    assert ngram_jaccard("  ", "anything") == 0.0


def test_ngram_jaccard_subtrigram_inputs_return_zero() -> None:
    """Sub-3-char inputs would produce spurious 1.0 via padding alone."""
    assert ngram_jaccard("ab", "ab") == 0.0
    assert ngram_jaccard("a", "abc") == 0.0


# ---------------------------------------------------------------------------
# 3. Empty-input guards
# ---------------------------------------------------------------------------


def test_empty_normalized_name_returns_zero_score(conn: sqlite3.Connection) -> None:
    entity = _make_entity("PlaceholderCorp", source="quickbooks")
    # Force the empty path by passing in a custom entity-like with empty name.
    from dataclasses import replace

    empty_entity = replace(entity, normalized_name="   ")
    result = score_pair(
        entity=empty_entity,
        candidate_id="CAN-Z",
        candidate_name="some candidate",
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
    )
    assert result.score == 0.0
    assert result.signal_breakdown.token_set_ratio == 0.0
    assert result.signal_breakdown.alias_boost_fired is False
    assert result.weight_profile_id == "psa_accounting_v1"


def test_empty_candidate_name_returns_zero_score(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Acme Corp", source="quickbooks")
    result = score_pair(
        entity=entity,
        candidate_id="CAN-Z",
        candidate_name="   ",
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
    )
    assert result.score == 0.0
    assert result.signal_breakdown == SignalBreakdown(
        token_sort_ratio=0.0,
        token_set_ratio=0.0,
        partial_ratio=0.0,
        jaro_winkler=0.0,
        ngram_jaccard=0.0,
        alias_boost_fired=False,
        abbreviation_bonus_fired=False,
    )


# ---------------------------------------------------------------------------
# 4. PSA abbreviation heuristic
# ---------------------------------------------------------------------------


def test_psa_abbreviation_heuristic_fires_on_prefix_match() -> None:
    """PSA shortcode 'cen' (≤4 chars) prefix-matches token in long side."""
    fires = _check_psa_abbreviation(
        entity_name="cen",
        candidate_name="cenlar fsb",
        candidate_aliases=(),
        entity_category="psa",
        candidate_category="accounting",
    )
    assert fires is True


def test_psa_abbreviation_heuristic_fires_on_initialism_match() -> None:
    """Shortcode equals consonant-skeleton initials of long side."""
    fires = _check_psa_abbreviation(
        entity_name="mcg",
        candidate_name="meridian consulting group",
        candidate_aliases=(),
        entity_category="psa",
        candidate_category="accounting",
    )
    assert fires is True


def test_psa_abbreviation_heuristic_does_not_fire_on_long_names() -> None:
    """Rebrand CAN-019: 'stratos cloud' / 'cloudnine infrastructure' —
    neither side ≤4 chars, heuristic must NOT fire."""
    fires = _check_psa_abbreviation(
        entity_name="stratos cloud",
        candidate_name="cloudnine infrastructure",
        candidate_aliases=(),
        entity_category="psa",
        candidate_category="accounting",
    )
    assert fires is False


def test_psa_abbreviation_heuristic_does_not_fire_outside_psa_accounting_pair() -> None:
    fires = _check_psa_abbreviation(
        entity_name="cen",
        candidate_name="cenlar fsb",
        candidate_aliases=(),
        entity_category="crm",
        candidate_category="payments",
    )
    assert fires is False


# ---------------------------------------------------------------------------
# 5. Alias boost behaviour
# ---------------------------------------------------------------------------


def test_alias_boost_single_application(conn: sqlite3.Connection) -> None:
    """Multiple aliases ≥0.85 must produce a SINGLE +0.15 bonus, not a
    stacked bonus.

    Using `candidate_name="acme group ventures"` ensures the weighted
    sum lands in the ~0.6–0.7 range so the clamp does not hide a
    stacking bug — if the boost stacked, the 3-alias score would be
    measurably higher than the 1-alias score. Both must instead be
    equal (single application).
    """
    entity = _make_entity("Acme Corp", source="quickbooks")
    # Three aliases, all near-identical to entity name; none equal
    # candidate_name (so the defensive guard does not skip them).
    aliases = ("acme corp", "acme corporation", "acme corp inc")
    result = score_pair(
        entity=entity,
        candidate_id="CAN-A",
        candidate_name="acme group ventures",
        candidate_aliases=aliases,
        candidate_category="psa",
        conn=conn,
    )
    assert result.signal_breakdown.alias_boost_fired is True
    # Re-score with a single matching alias to verify the same end score.
    single = score_pair(
        entity=entity,
        candidate_id="CAN-A",
        candidate_name="acme group ventures",
        candidate_aliases=("acme corp",),
        candidate_category="psa",
        conn=conn,
    )
    assert single.signal_breakdown.alias_boost_fired is True
    assert math.isclose(result.score, single.score), (
        f"alias_boost stacked: 3-alias score={result.score}, "
        f"1-alias score={single.score}"
    )


def test_alias_boost_does_not_fire_when_aliases_diverge(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Acme Corp", source="quickbooks")
    result = score_pair(
        entity=entity,
        candidate_id="CAN-A",
        candidate_name="acme corp",
        candidate_aliases=("zenith rocketry", "borealis labs"),
        candidate_category="psa",
        conn=conn,
    )
    assert result.signal_breakdown.alias_boost_fired is False


# ---------------------------------------------------------------------------
# 6. Score clamping
# ---------------------------------------------------------------------------


def test_score_clamped_to_unit_interval(conn: sqlite3.Connection) -> None:
    """Perfect string match + alias boost + abbreviation bonus + graph
    evidence pushes raw score >1.0; clamp must hold at 1.0."""
    # PSA↔accounting pair so abbreviation_bonus is in play
    entity = _make_entity("CEN", source="ruddr", entity_category="organization")
    _insert_canonical(conn, "CAN-CEN", "cen")
    _insert_canonical(conn, "CAN-PERSON-1", "alice", entity_type="person", entity_category="person")
    # Seed neighbors of CAN-CEN: an unresolved source canonical also
    # neighbors CAN-PERSON-1 so the shared_person_bonus fires.
    _insert_canonical(conn, "CAN-SRC", "cen")
    _insert_edge(conn, "CAN-SRC", "CAN-PERSON-1")
    _insert_edge(conn, "CAN-CEN", "CAN-PERSON-1")
    # Also add a non-person shared neighbor.
    _insert_canonical(conn, "CAN-PROJ", "project alpha")
    _insert_edge(conn, "CAN-SRC", "CAN-PROJ")
    _insert_edge(conn, "CAN-CEN", "CAN-PROJ")
    conn.commit()

    result = score_pair(
        entity=entity,
        candidate_id="CAN-CEN",
        candidate_name="cen",
        candidate_aliases=("cen",),
        candidate_category="accounting",
        conn=conn,
        source_canonical_id="CAN-SRC",
    )
    assert result.score <= 1.0
    assert result.score == 1.0  # clamp pinned at ceiling


# ---------------------------------------------------------------------------
# 7. Graph evidence caps and zero-edge behavior
# ---------------------------------------------------------------------------


def test_graph_evidence_zero_on_empty_edge_table(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Acme Corp", source="quickbooks")
    _insert_canonical(conn, "CAN-A", "acme")
    conn.commit()
    result = score_pair(
        entity=entity,
        candidate_id="CAN-A",
        candidate_name="acme",
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
        source_canonical_id="CAN-A",
    )
    assert result.graph_evidence.shared_person_count == 0
    assert result.graph_evidence.shared_person_bonus == 0.0
    assert result.graph_evidence.neighborhood_overlap_count == 0
    assert result.graph_evidence.neighborhood_overlap_bonus == 0.0


def test_shared_person_bonus_capped(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Acme Corp", source="quickbooks")
    _insert_canonical(conn, "CAN-CAND", "acme")
    _insert_canonical(conn, "CAN-SRC", "acme src")
    # Seed 5 shared person neighbors — bonus would be 5*0.05=0.25 unclamped.
    for i in range(5):
        pid = f"CAN-P{i}"
        _insert_canonical(conn, pid, f"person {i}", entity_type="person", entity_category="person")
        _insert_edge(conn, "CAN-SRC", pid)
        _insert_edge(conn, "CAN-CAND", pid)
    conn.commit()

    result = score_pair(
        entity=entity,
        candidate_id="CAN-CAND",
        candidate_name="acme",
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
        source_canonical_id="CAN-SRC",
    )
    assert result.graph_evidence.shared_person_count == 5
    assert math.isclose(result.graph_evidence.shared_person_bonus, MAX_SHARED_PERSON_BONUS)


def test_neighborhood_overlap_bonus_capped(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Acme Corp", source="quickbooks")
    _insert_canonical(conn, "CAN-CAND", "acme")
    _insert_canonical(conn, "CAN-SRC", "acme src")
    # 7 shared non-person neighbors — overlap bonus would be 7*0.025=0.175 unclamped.
    for i in range(7):
        nid = f"CAN-N{i}"
        _insert_canonical(conn, nid, f"node {i}")
        _insert_edge(conn, "CAN-SRC", nid)
        _insert_edge(conn, "CAN-CAND", nid)
    conn.commit()

    result = score_pair(
        entity=entity,
        candidate_id="CAN-CAND",
        candidate_name="acme",
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
        source_canonical_id="CAN-SRC",
    )
    assert result.graph_evidence.neighborhood_overlap_count == 7
    assert math.isclose(
        result.graph_evidence.neighborhood_overlap_bonus, MAX_NEIGHBORHOOD_BONUS
    )


# ---------------------------------------------------------------------------
# 8. score_candidate_set ordering
# ---------------------------------------------------------------------------


def test_score_candidate_set_orders_by_descending_score(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Cenlar FSB", source="ruddr", entity_category="organization")
    _insert_canonical(conn, "CAN-A", "cenlar fsb")  # perfect match
    _insert_canonical(conn, "CAN-B", "totally unrelated thing")
    _insert_canonical(conn, "CAN-C", "cenlar")  # close
    conn.commit()

    cs = CandidateSet(
        source_entity_id="RUDDR-X",
        candidates=(
            CandidateEntity("CAN-B", ("token:totally",)),
            CandidateEntity("CAN-A", ("token:cenlar",)),
            CandidateEntity("CAN-C", ("token:cenlar",)),
        ),
    )
    out = score_candidate_set(entity, cs, conn)
    scores = [m.score for m in out]
    assert scores == sorted(scores, reverse=True)
    assert out[0].canonical_id == "CAN-A"


def test_score_candidate_set_breaks_ties_by_canonical_id(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Acme", source="ruddr", entity_category="organization")
    _insert_canonical(conn, "CAN-Z", "totally different")
    _insert_canonical(conn, "CAN-A", "totally different")
    conn.commit()
    cs = CandidateSet(
        source_entity_id="RUDDR-X",
        candidates=(
            CandidateEntity("CAN-Z", ()),
            CandidateEntity("CAN-A", ()),
        ),
    )
    out = score_candidate_set(entity, cs, conn)
    assert [m.canonical_id for m in out] == ["CAN-A", "CAN-Z"]


def test_score_candidate_set_skips_missing_canonicals(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Acme", source="ruddr", entity_category="organization")
    _insert_canonical(conn, "CAN-A", "acme")
    conn.commit()
    cs = CandidateSet(
        source_entity_id="RUDDR-X",
        candidates=(
            CandidateEntity("CAN-MISSING", ()),
            CandidateEntity("CAN-A", ()),
        ),
    )
    out = score_candidate_set(entity, cs, conn)
    assert [m.canonical_id for m in out] == ["CAN-A"]


# ---------------------------------------------------------------------------
# 9. Signal breakdown completeness
# ---------------------------------------------------------------------------


def test_signal_breakdown_carries_every_weighted_signal(conn: sqlite3.Connection) -> None:
    entity = _make_entity("Acme", source="quickbooks")
    result = score_pair(
        entity=entity,
        candidate_id="CAN-A",
        candidate_name="acme",
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
    )
    breakdown = result.signal_breakdown
    # Every numeric field present and ≥0; booleans present.
    assert breakdown.token_sort_ratio >= 0
    assert breakdown.token_set_ratio >= 0
    assert breakdown.partial_ratio >= 0
    assert breakdown.jaro_winkler >= 0
    assert breakdown.ngram_jaccard >= 0
    assert isinstance(breakdown.alias_boost_fired, bool)
    assert isinstance(breakdown.abbreviation_bonus_fired, bool)
    assert breakdown.fasttext_cosine >= 0


# ---------------------------------------------------------------------------
# 10. Brief success criteria — CEN, rebrand, inversion
# ---------------------------------------------------------------------------


def test_cen_vs_cenlar_scores_above_surface(conn: sqlite3.Connection) -> None:
    """Brief success #6: 'CEN' (RUDDR) vs 'Cenlar, LLC' (QB) —
    abbreviation heuristic fires; score > 0.70."""
    entity = _make_entity("CEN", source="ruddr", entity_category="organization")
    candidate_name = _make_entity(
        "Cenlar, LLC.", source="quickbooks", entity_category="organization"
    ).normalized_name
    _insert_canonical(conn, "CAN-001", candidate_name)
    conn.commit()
    result = score_pair(
        entity=entity,
        candidate_id="CAN-001",
        candidate_name=candidate_name,
        candidate_aliases=(),
        candidate_category="accounting",
        conn=conn,
    )
    assert result.signal_breakdown.abbreviation_bonus_fired is True
    assert result.score > 0.70, (
        f"score={result.score}; breakdown={result.signal_breakdown}"
    )


def test_rebrand_pair_scores_below_no_match(conn: sqlite3.Connection) -> None:
    """Brief success #8: 'BrightPath Machine Learning Corp' (QB) vs
    'Luminos AI' (RUDDR) — rebrand pattern; score < 0.50."""
    entity = _make_entity(
        "BrightPath Machine Learning Corp",
        source="quickbooks",
        entity_category="organization",
    )
    candidate_name = _make_entity(
        "Luminos AI", source="ruddr", entity_category="organization"
    ).normalized_name
    _insert_canonical(conn, "CAN-013", candidate_name)
    conn.commit()
    result = score_pair(
        entity=entity,
        candidate_id="CAN-013",
        candidate_name=candidate_name,
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
    )
    assert result.score < 0.50, (
        f"score={result.score}; breakdown={result.signal_breakdown}"
    )


def test_person_inversion_pair_scores_at_least_0_95_via_string_metrics(
    conn: sqlite3.Connection,
) -> None:
    """Brief success #7 (hardened): 'Chen, Michael' (QB) and 'Michael
    Chen' (RUDDR) both normalize to 'michael chen'. The 0.95 direct
    override is dropped (Stage 0 owns inversion). The alias-driven
    string-metric path still produces ≥0.95 because the canonical seeds
    a token-reordered source-side alias (V1 canonical-write convention
    excludes `value == canonical_name`).
    """
    qb_norm = _make_entity(
        "Chen, Michael", source="quickbooks", entity_category="person"
    ).normalized_name
    ruddr_norm = _make_entity(
        "Michael Chen", source="ruddr", entity_category="person"
    ).normalized_name
    assert qb_norm == "michael chen"
    assert ruddr_norm == "michael chen"

    _insert_canonical(
        conn,
        "CAN-018",
        ruddr_norm,
        entity_type="person",
        entity_category="person",
    )
    # Aliases differ from canonical_name but still trigger the alias
    # boost. "chen michael" is a token-reorder of "michael chen":
    # RapidFuzz token_set_ratio == 100, well above the 85 threshold.
    inversion_alias = "chen michael"
    _insert_alias(
        conn, "CAN-018", inversion_alias, source="quickbooks", category="accounting"
    )
    conn.commit()

    entity = _make_entity(
        "Chen, Michael",
        source="quickbooks",
        entity_category="person",
        source_id="QB-018",
    )
    result = score_pair(
        entity=entity,
        candidate_id="CAN-018",
        candidate_name=ruddr_norm,
        candidate_aliases=(inversion_alias,),
        candidate_category="psa",
        conn=conn,
    )
    assert result.score >= 0.95, (
        f"score={result.score}; breakdown={result.signal_breakdown}"
    )


# ---------------------------------------------------------------------------
# 11. Ground-truth coverage
# ---------------------------------------------------------------------------


def _norm_or_none(raw_name: str, source: str, entity_category: str) -> Optional[str]:
    """Best-effort normalize; returns None if normalizer rejects (empty)."""
    try:
        n = normalize_entity(
            {
                "id": "TMP",
                "source": source,
                "entity_category": entity_category,
                "display_name": raw_name,
            }
        )
        return n.normalized_name
    except Exception:
        return None


def test_44_ground_truth_pairs_score_above_no_match(conn: sqlite3.Connection) -> None:
    """Brief success #9: all 44 ground-truth pairs score >0.50, EXCEPT
    the two rebrand-alias cases (CAN-013, CAN-019) — which the brief
    itself requires to score <0.50 (success #8). The 42 non-rebrand
    pairs are exhaustively asserted; the 2 rebrand pairs are excluded
    here and tested in `test_rebrand_pair_scores_below_no_match` (only
    CAN-013 case shown there; the rebrand exclusion list is explicit
    below).
    """
    payload = json.loads(FIXTURE_GT.read_text())
    rebrand_ids = {"CAN-013", "CAN-019"}
    failures: list[str] = []
    counted = 0
    for cano in payload["canonical_entities"]:
        if cano["canonical_id"] in rebrand_ids:
            continue
        sources = cano.get("sources", {})
        if "quickbooks" not in sources or "ruddr" not in sources:
            continue
        qb_name = sources["quickbooks"].get("display_name")
        ruddr_name = sources["ruddr"].get("display_name")
        if not qb_name or not ruddr_name:
            continue

        cat_kind = cano["entity_category"]  # 'organization' | 'person'
        entity_type = cano["entity_type"]
        qb_norm = _norm_or_none(qb_name, "quickbooks", cat_kind)
        ruddr_norm = _norm_or_none(ruddr_name, "ruddr", cat_kind)
        if qb_norm is None or ruddr_norm is None:
            continue

        # Seed candidate canonical (use the RUDDR-normalized name as
        # canonical_name) + the QB-normalized as an alias so the alias
        # boost path is available where the strings diverge.
        cid = cano["canonical_id"]
        _insert_canonical(
            conn,
            cid,
            ruddr_norm,
            entity_type=entity_type,
            entity_category=cat_kind,
        )
        _insert_alias(conn, cid, qb_norm, source="quickbooks", category="accounting")

        entity = _make_entity(
            qb_name,
            source="quickbooks",
            entity_category=cat_kind,
            source_id=sources["quickbooks"]["id"],
        )
        result = score_pair(
            entity=entity,
            candidate_id=cid,
            candidate_name=ruddr_norm,
            candidate_aliases=(qb_norm,),
            candidate_category="psa",
            conn=conn,
        )
        counted += 1
        if result.score <= 0.50:
            failures.append(
                f"{cid} [{cano.get('pattern','?')}] "
                f"QB={qb_name!r} (norm={qb_norm!r}) "
                f"RUDDR={ruddr_name!r} (norm={ruddr_norm!r}) "
                f"score={result.score:.3f}"
            )
    conn.commit()
    assert counted >= 40, f"expected ≥40 non-rebrand dual-source pairs; iterated {counted}"
    assert not failures, "ground-truth pairs scoring ≤0.50:\n  " + "\n  ".join(failures)


def test_10_synthesized_non_match_pairs_score_below_no_match(
    conn: sqlite3.Connection,
) -> None:
    """Random cross-canonical pairs (X.qb vs Y.ruddr where X≠Y AND
    they share NO common normalized tokens) must score <0.50. Seed
    pinned at 42.

    The shared-token filter excludes pairs where a common business
    word ("Group", "Capital", "Holdings") creates legitimate fuzzy
    overlap — those pairs are correctly surfaced into the 0.50–0.70
    SURFACE band, not below NO_MATCH. They are not "known non-matches"
    in the brief's sense.
    """
    payload = json.loads(FIXTURE_GT.read_text())
    entries = [
        c
        for c in payload["canonical_entities"]
        if "quickbooks" in c.get("sources", {}) and "ruddr" in c.get("sources", {})
    ]
    rng = random.Random(42)
    pairs: list[tuple[dict, dict]] = []
    attempts = 0
    while len(pairs) < 10 and attempts < 1000:
        attempts += 1
        x, y = rng.sample(entries, 2)
        if x["canonical_id"] == y["canonical_id"]:
            continue
        if x["entity_category"] != y["entity_category"]:
            continue  # only synthesize non-matches within same category
        cat_kind = x["entity_category"]
        qb_norm = _norm_or_none(
            x["sources"]["quickbooks"]["display_name"], "quickbooks", cat_kind
        )
        ruddr_norm = _norm_or_none(
            y["sources"]["ruddr"]["display_name"], "ruddr", cat_kind
        )
        if qb_norm is None or ruddr_norm is None:
            continue
        if set(qb_norm.split()) & set(ruddr_norm.split()):
            continue  # shared token → legitimate SURFACE-band overlap
        pairs.append((x, y))
    assert len(pairs) == 10, f"only synthesized {len(pairs)} non-match pairs"

    failures: list[str] = []
    for x, y in pairs:
        cat_kind = x["entity_category"]
        qb_name = x["sources"]["quickbooks"]["display_name"]
        ruddr_name = y["sources"]["ruddr"]["display_name"]
        qb_norm = _norm_or_none(qb_name, "quickbooks", cat_kind)
        ruddr_norm = _norm_or_none(ruddr_name, "ruddr", cat_kind)
        if qb_norm is None or ruddr_norm is None:
            continue

        entity = _make_entity(
            qb_name,
            source="quickbooks",
            entity_category=cat_kind,
            source_id=x["sources"]["quickbooks"]["id"],
        )
        result = score_pair(
            entity=entity,
            candidate_id=y["canonical_id"],
            candidate_name=ruddr_norm,
            candidate_aliases=(),
            candidate_category="psa",
            conn=conn,
        )
        if result.score >= 0.50:
            failures.append(
                f"X={x['canonical_id']} Y={y['canonical_id']} "
                f"QB={qb_name!r} (norm={qb_norm!r}) "
                f"RUDDR={ruddr_name!r} (norm={ruddr_norm!r}) "
                f"score={result.score:.3f}"
            )
    assert not failures, (
        "synthesized non-match pairs scoring ≥0.50:\n  " + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# 12. Signal Set B unit tests (B2, AC-24 cap, AC-25 negative)
# ---------------------------------------------------------------------------


def test_b2_fires_on_overlapping_project_code_fragments(conn: sqlite3.Connection) -> None:
    """AC-17 positive: two overlapping code fragments → B2 fires, raw=0.12."""
    result = _compute_b_boosts(
        conn=conn,
        source_id=None,
        candidate_id="CAN-X",
        source_category="accounting",
        candidate_category="psa",
        source_external_fields={"class": "GENAI-SOW3", "memo": "GENAI Q4"},
        candidate_external_fields={"project_codes": ["CEN-GENAI-SOW3", "CEN-GENAI"]},
        base_score=0.80,
    )
    b2 = next((e for e in result if e.signal_id == "B2"), None)
    assert b2 is not None, "B2 must fire when ≥2 project-code fragments overlap"
    assert b2.raw == 0.12


def test_b2_does_not_fire_on_disjoint_fragments(conn: sqlite3.Connection) -> None:
    """AC-17 negative: disjoint fragments → B2 does not fire."""
    result = _compute_b_boosts(
        conn=conn,
        source_id=None,
        candidate_id="CAN-X",
        source_category="accounting",
        candidate_category="psa",
        source_external_fields={"class": "ALPHA-PROJ"},
        candidate_external_fields={"project_codes": ["BETA-TASK"]},
        base_score=0.80,
    )
    assert all(e.signal_id != "B2" for e in result), "B2 must not fire when no fragments overlap"


def test_b_boosts_total_applied_capped_at_max(conn: sqlite3.Connection) -> None:
    """AC-24: when sum(raw) > MAX_B_BOOST, applied values are scaled
    proportionally so sum(applied) == MAX_B_BOOST exactly.

    Signals: B1 raw=0.10 (2 shared persons) + B2 raw=0.12 (2 code
    fragments) + B4 raw=0.08 (matching corporate email: source via
    raw_record, candidate via merged external_fields) + B5 raw=0.05
    (5-day creation delta) = 0.35 total raw; cap distributes 0.20.
    B6 is mocked to 0 so only B1+B2+B4+B5 fire.
    """
    import datetime
    import unittest.mock as _mock

    ts_base = datetime.datetime(2024, 1, 15)
    ts_offset = datetime.datetime(2024, 1, 20)

    with _mock.patch(
        "core.matching.scoring.count_shared_person_neighbors", return_value=2
    ), _mock.patch(
        "core.matching.scoring.get_created_at", side_effect=[ts_base, ts_offset]
    ), _mock.patch(
        "core.matching.scoring.count_shared_graph_neighbors", return_value=0
    ):
        result = _compute_b_boosts(
            conn=conn,
            source_id="CAN-SRC",
            candidate_id="CAN-CAND",
            source_category="accounting",
            candidate_category="psa",
            source_external_fields={
                "class": "GENAI-SOW3",
                "memo": "GENAI Q4",
                "email": "alice@acmecorp.com",
            },
            candidate_external_fields={
                "project_codes": ["CEN-GENAI-SOW3", "CEN-GENAI"],
                "email": "bob@acmecorp.com",
            },
            base_score=0.80,
        )

    assert len(result) == 4, f"expected B1+B2+B4+B5, got {result}"
    signal_ids = {e.signal_id for e in result}
    assert signal_ids == {"B1", "B2", "B4", "B5"}, f"unexpected signals: {signal_ids}"
    b1 = next(e for e in result if e.signal_id == "B1")
    b2 = next(e for e in result if e.signal_id == "B2")
    b4 = next(e for e in result if e.signal_id == "B4")
    b5 = next(e for e in result if e.signal_id == "B5")
    assert b1.raw == pytest.approx(0.10)
    assert b2.raw == pytest.approx(0.12)
    assert b4.raw == pytest.approx(0.08)
    assert b5.raw == pytest.approx(0.05)
    total_applied = sum(e.applied for e in result)
    assert math.isclose(total_applied, MAX_B_BOOST, abs_tol=1e-9), (
        f"expected sum(applied)=={MAX_B_BOOST}, got {total_applied}"
    )
    for e in result:
        assert e.applied <= e.raw + 1e-9, f"{e.signal_id}: applied {e.applied} > raw {e.raw}"


def test_b_boosts_do_not_fire_outside_ambiguous_band(conn: sqlite3.Connection) -> None:
    """B-boosts return () when base_score < 0.70 or base_score >= 0.90."""
    import unittest.mock as _mock

    for base in (0.50, 0.69, 0.90, 1.0):
        with _mock.patch(
            "core.matching.scoring.count_shared_person_neighbors", return_value=5
        ):
            result = _compute_b_boosts(
                conn=conn,
                source_id="CAN-SRC",
                candidate_id="CAN-CAND",
                source_category="accounting",
                candidate_category="psa",
                source_external_fields={},
                candidate_external_fields={},
                base_score=base,
            )
        assert result == (), f"expected () at base_score={base}, got {result}"


def test_ac25_brightpath_vs_luminos_scores_below_no_match(conn: sqlite3.Connection) -> None:
    """AC-25: 'brightpath machine learning' vs 'luminos ai' — distinct
    entities with no shared tokens score < 0.50 (NO_MATCH band)."""
    entity = _make_entity("brightpath machine learning", source="quickbooks")
    result = score_pair(
        entity=entity,
        candidate_id="CAN-LUM",
        candidate_name="luminos ai",
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
    )
    assert result.score < 0.50, (
        f"score={result.score}; breakdown={result.signal_breakdown}"
    )


# ---------------------------------------------------------------------------
# 13. SC-5 (amended 2026-07-05): abbreviation pairs reach the review queue
#
# Measured on the real pre-trained model, the SC-5 pairs' cosines are
# LOW (pacrim 0.2526, meridian 0.3679; token-level pacrim↔pacific
# ≈ 0.0), so no fasttext weight can lift 'pacrim tech' above 0.70 —
# the composite converges toward the cosine as the weight grows. The
# amended criterion: both pairs land in the human review queue, via
# composite score (meridian) or the Stage 4 abbreviation rescue
# (pacrim). The stub vector table below pins the pair cosines at the
# measured real-model values so the full scoring path (no vacuous
# patching — QA-006) runs deterministically without the model file.
# ---------------------------------------------------------------------------


import unittest.mock as _sc5_mock

_SC5_STUB_VECTORS: dict[str, tuple[float, ...]] = {
    # cosine('pacrim tech', 'pacific rim...') = 0.2526 (measured)
    "pacrim tech": (1.0, 0.0, 0.0, 0.0),
    "pacific rim technologies international": (0.2526, 0.96757, 0.0, 0.0),
    # cosine('meridian cap', 'meridian capital group') = 0.3679 (measured)
    "meridian cap": (0.0, 0.0, 1.0, 0.0),
    "meridian capital group": (0.0, 0.0, 0.3679, 0.92987),
}


def _sc5_stub_embed(name: str) -> Optional[tuple[float, ...]]:
    return _SC5_STUB_VECTORS.get(name)


def _sc5_score_and_dispose(
    conn: sqlite3.Connection, entity_name: str, candidate_name: str
) -> tuple[ScoredMatch, str]:
    cid = f"CAN-{candidate_name[:4].upper()}"
    exists = conn.execute(
        "SELECT 1 FROM canonical_entities WHERE canonical_id = ?", (cid,)
    ).fetchone()
    if not exists:
        _insert_canonical(conn, cid, candidate_name)
        conn.commit()
    result = score_pair(
        entity=_make_entity(entity_name, source="quickbooks"),
        candidate_id=cid,
        candidate_name=candidate_name,
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
    )
    disposition = apply_thresholds(entity_name, (result,), conn)
    return result, disposition.action


def test_sc5_pairs_route_to_review_queue_with_embeddings(
    conn: sqlite3.Connection,
) -> None:
    """With embeddings available at measured real-model cosine levels:
    'meridian cap' clears SURFACE on composite score; 'pacrim tech'
    lands mid-band with the abbreviation heuristic fired and Stage 4
    rescues it to QUEUE_FOR_REVIEW."""
    with _sc5_mock.patch(
        "core.matching.embeddings.embed", side_effect=_sc5_stub_embed
    ):
        meridian, meridian_action = _sc5_score_and_dispose(
            conn, "meridian cap", "meridian capital group"
        )
        pacrim, pacrim_action = _sc5_score_and_dispose(
            conn, "pacrim tech", "pacific rim technologies international"
        )

    assert meridian.signal_breakdown.fasttext_available is True
    assert meridian.score >= 0.70, f"meridian score={meridian.score:.4f}"
    assert meridian_action == "QUEUE_FOR_REVIEW"

    assert pacrim.signal_breakdown.fasttext_available is True
    assert pacrim.signal_breakdown.abbreviation_bonus_fired is True
    assert 0.50 <= pacrim.score < 0.70, f"pacrim score={pacrim.score:.4f}"
    assert pacrim_action == "QUEUE_FOR_REVIEW", (
        "abbreviation rescue must route the mid-band pacrim pair to review"
    )

    # Load-bearing check: the fasttext weight must actually participate.
    # With these sub-0.70 cosines, renormalization pulls the composite
    # strictly BELOW the no-model score — equal scores would mean the
    # signal was silently ignored.
    with _sc5_mock.patch("core.matching.embeddings.embed", return_value=None):
        meridian_nomodel, _ = _sc5_score_and_dispose(
            conn, "meridian cap", "meridian capital group"
        )
        pacrim_nomodel, _ = _sc5_score_and_dispose(
            conn, "pacrim tech", "pacific rim technologies international"
        )
    assert meridian.score < meridian_nomodel.score
    assert pacrim.score < pacrim_nomodel.score


def test_sc5_pairs_route_to_review_queue_without_model(
    conn: sqlite3.Connection,
) -> None:
    """Model-absent path (CI): same review-queue outcome for both pairs
    via the pre-8a-identical no-model score."""
    with _sc5_mock.patch("core.matching.embeddings.embed", return_value=None):
        meridian, meridian_action = _sc5_score_and_dispose(
            conn, "meridian cap", "meridian capital group"
        )
        pacrim, pacrim_action = _sc5_score_and_dispose(
            conn, "pacrim tech", "pacific rim technologies international"
        )

    assert meridian.signal_breakdown.fasttext_available is False
    assert meridian.score >= 0.70
    assert meridian_action == "QUEUE_FOR_REVIEW"

    assert pacrim.signal_breakdown.fasttext_available is False
    assert pacrim.signal_breakdown.abbreviation_bonus_fired is True
    assert 0.50 <= pacrim.score < 0.70
    assert pacrim_action == "QUEUE_FOR_REVIEW"


@pytest.mark.skipif(not _model_present(), reason="fasttext model not downloaded")
def test_sc5_end_to_end_with_real_model(conn: sqlite3.Connection) -> None:
    """Integration against the real downloaded model — no patching at
    all. Both SC-5 pairs must reach the review queue."""
    for entity_name, candidate_name in (
        ("meridian cap", "meridian capital group"),
        ("pacrim tech", "pacific rim technologies international"),
    ):
        result, action = _sc5_score_and_dispose(conn, entity_name, candidate_name)
        assert result.signal_breakdown.fasttext_available is True
        assert action == "QUEUE_FOR_REVIEW", (
            f"{entity_name!r} vs {candidate_name!r}: "
            f"score={result.score:.4f} action={action}"
        )


# ---------------------------------------------------------------------------
# 14. Dynamic weight renormalization (AC-5: tier-1 budget sums to 1.0)
# ---------------------------------------------------------------------------


def test_renormalized_budget_ceiling_is_one_with_embeddings() -> None:
    """A perfect pair (all signals maxed, alias fired, cosine 1.0) must
    score exactly 1.0 pre-clamp — the fasttext slot renormalizes into
    the budget instead of stacking past it."""
    perfect = SignalBreakdown(
        token_sort_ratio=100.0,
        token_set_ratio=100.0,
        partial_ratio=100.0,
        jaro_winkler=100.0,
        ngram_jaccard=1.0,
        alias_boost_fired=True,
        abbreviation_bonus_fired=False,
        fasttext_cosine=1.0,
        fasttext_available=True,
    )
    for w in (DEFAULT_WEIGHTS, PSA_ACCOUNTING_WEIGHTS):
        base = _base_weighted_score(w, perfect)
        assert math.isclose(base, 1.0, abs_tol=1e-9), (
            f"profile {w.profile_id!r}: perfect-signal base={base} (expected 1.0)"
        )


def test_fasttext_weight_inert_when_embedding_unavailable() -> None:
    """When no embedding is available the fasttext weight must not
    consume budget: scores are bit-identical across any configured
    fasttext weight (pre-8a parity on the no-model path)."""
    from dataclasses import replace

    bd = SignalBreakdown(
        token_sort_ratio=62.0,
        token_set_ratio=71.0,
        partial_ratio=88.0,
        jaro_winkler=79.5,
        ngram_jaccard=0.41,
        alias_boost_fired=True,
        abbreviation_bonus_fired=False,
        fasttext_cosine=0.0,
        fasttext_available=False,
    )
    w_hi = replace(PSA_ACCOUNTING_WEIGHTS, fasttext_cosine=0.5)
    assert _base_weighted_score(PSA_ACCOUNTING_WEIGHTS, bd) == _base_weighted_score(
        w_hi, bd
    )


# ---------------------------------------------------------------------------
# 15. Token-level abbreviation heuristic
# ---------------------------------------------------------------------------


def test_token_level_abbreviation_fires_for_sc5_pairs() -> None:
    assert _tokens_abbreviate(
        "pacrim tech", "pacific rim technologies international"
    )
    assert _tokens_abbreviate("meridian cap", "meridian capital group")
    # Side-agnostic: argument order must not matter.
    assert _tokens_abbreviate(
        "pacific rim technologies international", "pacrim tech"
    )


def test_token_level_abbreviation_rejects_non_matches() -> None:
    # Rebrand pairs: no token abbreviates the other side.
    assert not _tokens_abbreviate("brightpath machine learning", "luminos ai")
    assert not _tokens_abbreviate("stratos cloud", "cloudnine infrastructure")
    # Equal token counts never fire — string metrics own that regime.
    assert not _tokens_abbreviate("acme corp", "acme corporation")
    # Partial coverage is not enough: every short-side token must match.
    assert not _tokens_abbreviate(
        "pacrim holdings", "pacific rim technologies international"
    )
    # Pure token-subset truncations never fire: token_set_ratio already
    # scores them 100, and the bonus would outrank an exact match.
    assert not _tokens_abbreviate("cenlar", "cenlar fsb")
    assert not _tokens_abbreviate("meridian capital", "meridian capital group")


def test_token_level_abbreviation_gated_to_psa_accounting_pair() -> None:
    """The heuristic only applies to the PSA↔Accounting category pair."""
    fired = _check_psa_abbreviation(
        entity_name="pacrim tech",
        candidate_name="pacific rim technologies international",
        candidate_aliases=(),
        entity_category="accounting",
        candidate_category="accounting",
    )
    assert fired is False


# ---------------------------------------------------------------------------
# 13. Signal B3 — amount co-occurrence (feature 8b)
# ---------------------------------------------------------------------------


def test_b3_symmetry_via_mirrored_score_pair(conn: sqlite3.Connection) -> None:
    """(1) Symmetry: B3(entity -> candidate) == B3(candidate -> entity)
    at a boundary amount, asserted via two mirrored score_pair calls.
    `_base_weighted_score` is mocked to land both pairs in the band."""
    import unittest.mock as _mock

    _insert_txn(conn, "quickbooks", "QB-SRC-X", 1000.00, "2026-03-15",
                counterparty_source_id="QB-X")
    _insert_txn(conn, "ruddr", "RUDDR-CAND-Y", 1000.00, "2026-03-20",
                category="psa", canonical_id="CAN-Y")
    _insert_txn(conn, "ruddr", "RUDDR-SRC-Y", 1000.00, "2026-03-15",
                category="psa", counterparty_source_id="RUDDR-Y")
    _insert_txn(conn, "quickbooks", "QB-CAND-X", 1000.00, "2026-03-20",
                canonical_id="CAN-X")
    _insert_canonical(conn, "CAN-Y", "Cenlar PSA Co", entity_type="client")
    _insert_canonical(conn, "CAN-X", "Cenlar Accounting Co", entity_type="client")

    entity_x = _make_entity("Cenlar Accounting Co", source="quickbooks", source_id="QB-X")
    entity_y = _make_entity("Cenlar PSA Co", source="ruddr", source_id="RUDDR-Y")

    with _mock.patch("core.matching.scoring._base_weighted_score", return_value=0.80):
        forward = score_pair(
            entity=entity_x,
            candidate_id="CAN-Y",
            candidate_name="Cenlar PSA Co",
            candidate_aliases=(),
            candidate_category="psa",
            conn=conn,
        )
        backward = score_pair(
            entity=entity_y,
            candidate_id="CAN-X",
            candidate_name="Cenlar Accounting Co",
            candidate_aliases=(),
            candidate_category="accounting",
            conn=conn,
        )

    b3_forward = next(e for e in forward.signal_breakdown.b_boosts if e.signal_id == "B3")
    b3_backward = next(e for e in backward.signal_breakdown.b_boosts if e.signal_id == "B3")
    assert b3_forward.raw == b3_backward.raw == 0.10


def test_b3_credit_memo_amounts_cooccur(conn: sqlite3.Connection) -> None:
    """(2) Credit memo: -1000.00 and 1000.00 co-occur (ABS semantics)."""
    _insert_txn(conn, "quickbooks", "QB-CM", -1000.00, "2026-04-01",
                counterparty_source_id="QB-CM-CUST")
    _insert_txn(conn, "ruddr", "RUDDR-CM", 1000.00, "2026-04-10",
                category="psa", canonical_id="CAN-CM")
    count = count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-CM-CUST", "CAN-CM"
    )
    assert count == 1


def test_b3_tolerance_boundary_percentage_bound(conn: sqlite3.Connection) -> None:
    """(3) Tolerance boundary, percentage-bound side: delta exactly equal
    to MAX(|a|,|b|)*0.02 fires; one cent over does not. The candidate
    amount is kept <= the anchor amount so MAX(|a|,|b|) stays fixed at
    1000.00 across both cases — otherwise a larger delta would also
    inflate the tolerance itself."""
    # 1000.00 * 0.02 == 20.00
    _insert_txn(conn, "quickbooks", "QB-PCT-1", 1000.00, "2026-05-01",
                counterparty_source_id="QB-PCT-CUST")
    _insert_txn(conn, "ruddr", "RUDDR-PCT-1", 980.00, "2026-05-05",
                category="psa", canonical_id="CAN-PCT-1")
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-PCT-CUST", "CAN-PCT-1"
    ) == 1

    _insert_txn(conn, "quickbooks", "QB-PCT-2", 1000.00, "2026-06-01",
                counterparty_source_id="QB-PCT-CUST-2")
    _insert_txn(conn, "ruddr", "RUDDR-PCT-2", 979.99, "2026-06-05",
                category="psa", canonical_id="CAN-PCT-2")
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-PCT-CUST-2", "CAN-PCT-2"
    ) == 0


def test_b3_tolerance_boundary_dollar_cap_bound(conn: sqlite3.Connection) -> None:
    """(3) Tolerance boundary, $500-cap side: at 100000.00 the percentage
    bound (2000.00) exceeds the flat cap, so 500.00 exactly fires and
    500.01 does not. The candidate amount stays <= the anchor so
    MAX(|a|,|b|) is fixed at 100000.00 in both cases."""
    _insert_txn(conn, "quickbooks", "QB-CAP-1", 100000.00, "2026-07-01",
                counterparty_source_id="QB-CAP-CUST")
    _insert_txn(conn, "ruddr", "RUDDR-CAP-1", 99500.00, "2026-07-05",
                category="psa", canonical_id="CAN-CAP-1")
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-CAP-CUST", "CAN-CAP-1"
    ) == 1

    _insert_txn(conn, "quickbooks", "QB-CAP-2", 100000.00, "2026-08-01",
                counterparty_source_id="QB-CAP-CUST-2")
    _insert_txn(conn, "ruddr", "RUDDR-CAP-2", 99499.99, "2026-08-05",
                category="psa", canonical_id="CAN-CAP-2")
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-CAP-CUST-2", "CAN-CAP-2"
    ) == 0


def test_b3_tiering_one_period_two_periods_and_multi_pair_dedup(
    conn: sqlite3.Connection,
) -> None:
    """(4) Tiering: 1 distinct period -> raw +0.10 (via _compute_b_boosts);
    2 distinct periods -> raw +0.15; a single period containing three
    qualifying row pairs still counts once."""
    # One period, three qualifying row pairs within it.
    _insert_txn(conn, "quickbooks", "QB-T1", 500.00, "2026-01-10",
                counterparty_source_id="QB-TIER-CUST")
    _insert_txn(conn, "ruddr", "RUDDR-T1a", 500.00, "2026-01-05",
                category="psa", canonical_id="CAN-TIER")
    _insert_txn(conn, "ruddr", "RUDDR-T1b", 500.10, "2026-01-15",
                category="psa", canonical_id="CAN-TIER")
    _insert_txn(conn, "ruddr", "RUDDR-T1c", 499.90, "2026-01-20",
                category="psa", canonical_id="CAN-TIER")
    one_period_count = count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-TIER-CUST", "CAN-TIER"
    )
    assert one_period_count == 1
    one_period_boosts = _compute_b_boosts(
        conn=conn,
        source_id=None,
        candidate_id="CAN-TIER",
        source_category="accounting",
        candidate_category="psa",
        source_external_fields={},
        candidate_external_fields={},
        base_score=0.80,
        amount_cooccurrence_periods=one_period_count,
    )
    b3_one = next(e for e in one_period_boosts if e.signal_id == "B3")
    assert b3_one.raw == 0.10

    # A second, distinct period for the same pair.
    _insert_txn(conn, "quickbooks", "QB-T2", 500.00, "2026-02-10",
                counterparty_source_id="QB-TIER-CUST")
    _insert_txn(conn, "ruddr", "RUDDR-T2", 500.00, "2026-02-15",
                category="psa", canonical_id="CAN-TIER")
    two_period_count = count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-TIER-CUST", "CAN-TIER"
    )
    assert two_period_count == 2
    two_period_boosts = _compute_b_boosts(
        conn=conn,
        source_id=None,
        candidate_id="CAN-TIER",
        source_category="accounting",
        candidate_category="psa",
        source_external_fields={},
        candidate_external_fields={},
        base_score=0.80,
        amount_cooccurrence_periods=two_period_count,
    )
    b3_two = next(e for e in two_period_boosts if e.signal_id == "B3")
    assert b3_two.raw == 0.15


def test_b3_cross_currency_never_cooccurs(conn: sqlite3.Connection) -> None:
    """(5) Cross-currency: USD and EUR rows with identical amounts never
    co-occur."""
    _insert_txn(conn, "quickbooks", "QB-FX", 1000.00, "2026-09-01",
                counterparty_source_id="QB-FX-CUST", currency="USD")
    _insert_txn(conn, "ruddr", "RUDDR-FX", 1000.00, "2026-09-05",
                category="psa", canonical_id="CAN-FX", currency="EUR")
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-FX-CUST", "CAN-FX"
    ) == 0


def test_b3_same_source_never_cooccurs(conn: sqlite3.Connection) -> None:
    """(6) Same-source: two quickbooks rows never co-occur with each
    other."""
    _insert_txn(conn, "quickbooks", "QB-SS-1", 1000.00, "2026-10-01",
                counterparty_source_id="QB-SS-CUST")
    _insert_txn(conn, "quickbooks", "QB-SS-2", 1000.00, "2026-10-05",
                canonical_id="CAN-SS")
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-SS-CUST", "CAN-SS"
    ) == 0


def test_b3_band_gate_no_entry_outside_ambiguous_band(conn: sqlite3.Connection) -> None:
    """(7) Band gate: B3 produces no entry at base_score 0.65 or 0.92
    even with perfect co-occurrence (amount_cooccurrence_periods=2)."""
    for base in (0.65, 0.92):
        result = _compute_b_boosts(
            conn=conn,
            source_id=None,
            candidate_id="CAN-BAND",
            source_category="accounting",
            candidate_category="psa",
            source_external_fields={},
            candidate_external_fields={},
            base_score=base,
            amount_cooccurrence_periods=2,
        )
        assert result == ()


def test_b3_cap_collision_five_signal_renormalization(conn: sqlite3.Connection) -> None:
    """(8) Cap collision (NEW test): B1+B2+B3+B4+B5 with raw sum > 0.20
    renormalizes proportionally: sum(applied) == 0.20 exactly, each
    applied == raw * (0.20 / total_raw), all five entries present. The
    shipped 4-signal cap test (test_b_boosts_total_applied_capped_at_max)
    is untouched."""
    import datetime
    import unittest.mock as _mock

    ts_base = datetime.datetime(2024, 1, 15)
    ts_offset = datetime.datetime(2024, 1, 20)

    with _mock.patch(
        "core.matching.scoring.count_shared_person_neighbors", return_value=2
    ), _mock.patch(
        "core.matching.scoring.get_created_at", side_effect=[ts_base, ts_offset]
    ), _mock.patch(
        "core.matching.scoring.count_shared_graph_neighbors", return_value=0
    ):
        result = _compute_b_boosts(
            conn=conn,
            source_id="CAN-SRC5",
            candidate_id="CAN-CAND5",
            source_category="accounting",
            candidate_category="psa",
            source_external_fields={
                "class": "GENAI-SOW3",
                "memo": "GENAI Q4",
                "email": "alice@acmecorp.com",
            },
            candidate_external_fields={
                "project_codes": ["CEN-GENAI-SOW3", "CEN-GENAI"],
                "email": "bob@acmecorp.com",
            },
            base_score=0.80,
            amount_cooccurrence_periods=1,
        )

    assert len(result) == 5, f"expected B1+B2+B3+B4+B5, got {result}"
    signal_ids = {e.signal_id for e in result}
    assert signal_ids == {"B1", "B2", "B3", "B4", "B5"}
    raw_by_signal = {e.signal_id: e.raw for e in result}
    total_raw = sum(raw_by_signal.values())
    assert total_raw > MAX_B_BOOST
    total_applied = sum(e.applied for e in result)
    assert math.isclose(total_applied, MAX_B_BOOST, abs_tol=1e-9)
    for e in result:
        expected_applied = e.raw * (MAX_B_BOOST / total_raw)
        assert math.isclose(e.applied, expected_applied, rel_tol=1e-9)


def test_b3_period_derivation_and_mismatched_period_contributes_nothing(
    conn: sqlite3.Connection,
) -> None:
    """(9) Period derivation: a seeded row with txn_date='2026-03-15'
    carries period='2026-03'; the helper contributes 0 for a row whose
    period disagrees with its txn_date month."""
    _insert_txn(conn, "quickbooks", "QB-PERIOD", 250.00, "2026-03-15",
                counterparty_source_id="QB-PERIOD-CUST")
    row = conn.execute(
        "SELECT period FROM transactions WHERE external_source_id = ?",
        ("QB-PERIOD",),
    ).fetchone()
    assert row[0] == "2026-03"

    # Candidate row's stored period disagrees with its own txn_date month
    # (simulates a load-time-derived period bug) — must not co-occur.
    _insert_txn(conn, "ruddr", "RUDDR-PERIOD-BUG", 250.00, "2026-03-20",
                category="psa", canonical_id="CAN-PERIOD-BUG", period="2026-04")
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-PERIOD-CUST", "CAN-PERIOD-BUG"
    ) == 0


def test_b3_tenant_scoping(conn: sqlite3.Connection) -> None:
    """(10) Tenant scoping: rows under a different tenant_id are
    invisible to the helper when tenant_id is set."""
    _insert_txn(conn, "quickbooks", "QB-TEN", 750.00, "2026-11-01",
                counterparty_source_id="QB-TEN-CUST", tenant_id="TENANT_A")
    _insert_txn(conn, "ruddr", "RUDDR-TEN", 750.00, "2026-11-05",
                category="psa", canonical_id="CAN-TEN", tenant_id="TENANT_A")
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-TEN-CUST", "CAN-TEN", tenant_id="TENANT_A"
    ) == 1
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-TEN-CUST", "CAN-TEN", tenant_id="TENANT_B"
    ) == 0


def test_b3_dark_by_default_proven_in_process(conn: sqlite3.Connection) -> None:
    """(11) Dark by default, proven in-process: with an empty
    transactions table, (a) the helper returns 0, (b) no BoostEntry with
    signal_id=='B3' appears, and (c) the score is bit-identical between
    the pre-8b path (amount_cooccurrence_periods=None) and the 8b path
    with zero evidence (amount_cooccurrence_periods=0) — computed both
    sides in-process, exact `==`, no baseline fixture."""
    assert count_amount_cooccurrence_periods(
        conn, "quickbooks", "QB-DARK-CUST", "CAN-DARK"
    ) == 0

    import datetime
    import unittest.mock as _mock

    ts_base = datetime.datetime(2024, 1, 15)
    ts_offset = datetime.datetime(2024, 1, 20)

    common_kwargs = dict(
        conn=conn,
        source_id="CAN-SRC-DARK",
        candidate_id="CAN-DARK",
        source_category="accounting",
        candidate_category="psa",
        source_external_fields={
            "class": "GENAI-SOW3",
            "email": "alice@acmecorp.com",
        },
        candidate_external_fields={
            "project_codes": ["CEN-GENAI-SOW3"],
            "email": "bob@acmecorp.com",
        },
        base_score=0.80,
    )

    with _mock.patch(
        "core.matching.scoring.count_shared_person_neighbors", return_value=1
    ), _mock.patch(
        "core.matching.scoring.get_created_at", side_effect=[ts_base, ts_offset, ts_base, ts_offset]
    ), _mock.patch(
        "core.matching.scoring.count_shared_graph_neighbors", return_value=0
    ):
        boosts_disabled = _compute_b_boosts(
            **common_kwargs, amount_cooccurrence_periods=None
        )
        boosts_enabled = _compute_b_boosts(
            **common_kwargs, amount_cooccurrence_periods=0
        )

    assert all(e.signal_id != "B3" for e in boosts_disabled)
    assert all(e.signal_id != "B3" for e in boosts_enabled)
    assert len(boosts_disabled) == len(boosts_enabled)
    for a, b in zip(boosts_disabled, boosts_enabled):
        assert a.signal_id == b.signal_id
        assert a.raw == b.raw
        assert a.applied == b.applied

    assert _weighted_score(0.80, boosts_disabled) == _weighted_score(0.80, boosts_enabled)


def test_b3_skips_entirely_when_periods_is_none(conn: sqlite3.Connection) -> None:
    """`_compute_b_boosts(..., amount_cooccurrence_periods=None)` skips
    B3 entirely, regardless of DB state — the parameter, not a DB query,
    gates B3 inside the function."""
    _insert_txn(conn, "quickbooks", "QB-SKIP", 500.00, "2026-12-01",
                counterparty_source_id="QB-SKIP-CUST")
    _insert_txn(conn, "ruddr", "RUDDR-SKIP", 500.00, "2026-12-05",
                category="psa", canonical_id="CAN-SKIP")
    result = _compute_b_boosts(
        conn=conn,
        source_id=None,
        candidate_id="CAN-SKIP",
        source_category="accounting",
        candidate_category="psa",
        source_external_fields={},
        candidate_external_fields={},
        base_score=0.80,
        amount_cooccurrence_periods=None,
    )
    assert all(e.signal_id != "B3" for e in result)


def test_b3_fires_on_unresolved_source_path(conn: sqlite3.Connection) -> None:
    """(12) Unresolved-source path: B3 fires for a pair whose source
    entity has source_canonical_id is None, proving the join keys do
    not require resolution."""
    import unittest.mock as _mock

    _insert_txn(conn, "quickbooks", "QB-UNRES", 1200.00, "2026-02-01",
                counterparty_source_id="QB-UNRES-CUST")
    _insert_txn(conn, "ruddr", "RUDDR-UNRES", 1200.00, "2026-02-05",
                category="psa", canonical_id="CAN-UNRES")
    _insert_canonical(conn, "CAN-UNRES", "Cenlar Unresolved Co", entity_type="client")

    entity = _make_entity(
        "Cenlar Unresolved Co", source="quickbooks", source_id="QB-UNRES-CUST"
    )

    with _mock.patch("core.matching.scoring._base_weighted_score", return_value=0.80):
        result = score_pair(
            entity=entity,
            candidate_id="CAN-UNRES",
            candidate_name="Cenlar Unresolved Co",
            candidate_aliases=(),
            candidate_category="psa",
            conn=conn,
            source_canonical_id=None,
        )

    b3 = next((e for e in result.signal_breakdown.b_boosts if e.signal_id == "B3"), None)
    assert b3 is not None
    assert b3.raw == 0.10


def test_score_pair_invokes_amount_cooccurrence_helper_at_most_once(
    conn: sqlite3.Connection,
) -> None:
    """score_pair invokes count_amount_cooccurrence_periods at most once
    per pair and only when in_b_band."""
    import unittest.mock as _mock

    entity = _make_entity("Acme Corp", source="quickbooks", source_id="QB-CALLCOUNT")

    with _mock.patch(
        "core.matching.scoring.count_amount_cooccurrence_periods", return_value=0
    ) as mocked, _mock.patch(
        "core.matching.scoring._base_weighted_score", return_value=0.30
    ):
        score_pair(
            entity=entity,
            candidate_id="CAN-CALLCOUNT",
            candidate_name="Completely Unrelated Candidate",
            candidate_aliases=(),
            candidate_category="psa",
            conn=conn,
        )
    mocked.assert_not_called()

    with _mock.patch(
        "core.matching.scoring.count_amount_cooccurrence_periods", return_value=1
    ) as mocked, _mock.patch(
        "core.matching.scoring._base_weighted_score", return_value=0.80
    ):
        score_pair(
            entity=entity,
            candidate_id="CAN-CALLCOUNT",
            candidate_name="Completely Unrelated Candidate",
            candidate_aliases=(),
            candidate_category="psa",
            conn=conn,
        )
    mocked.assert_called_once()
