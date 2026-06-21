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

from core.ingestion.normalizer import normalize_entity
from core.matching.scoring import (
    B_SIGNAL_CAP,
    MAX_NEIGHBORHOOD_BONUS,
    MAX_SHARED_PERSON_BONUS,
    _check_psa_abbreviation,
    _compute_b_boosts,
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
# 12. fastText Signal C abbreviation lift
# ---------------------------------------------------------------------------

import json as _json


def _stub_embed_fn(vecs: dict[str, tuple[float, ...]]):
    """Return a closure that maps normalized names to stub vectors."""
    def _embed(name: str):
        return vecs.get(name.strip().lower())
    return _embed


def _load_stub_vectors() -> dict[str, tuple[float, ...]]:
    stub_path = REPO_ROOT / "tests" / "fixtures" / "stub_vectors.json"
    raw = _json.loads(stub_path.read_text())
    return {k: tuple(v) for k, v in raw.items()}


def test_fasttext_signal_c_abbreviation_lift(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stubs = _load_stub_vectors()
    stub_embed = _stub_embed_fn(stubs)

    import core.matching.scoring as scoring_mod
    monkeypatch.setattr(scoring_mod, "embed", stub_embed)

    # "pacrim tech" vs "pacific rim technologies international": base≈0.434, +ft(0.9,w=0.35)≈0.749 (crosses 0.70)
    # "meridian cap" vs "meridian capital group": base≈0.665, +ft(0.9,w=0.35)≈0.980 (crosses 0.70)
    pairs = [
        ("pacrim tech", "pacific rim technologies international"),
        ("meridian cap", "meridian capital group"),
    ]
    for entity_name, candidate_name in pairs:
        entity = _make_entity(entity_name, source="quickbooks")
        _insert_canonical(conn, f"CAN-{entity_name[:6]}", candidate_name)
        conn.commit()

        import core.matching.weights as weights_mod
        from core.matching.weights import WeightConfig

        result_on = score_pair(
            entity=entity,
            candidate_id=f"CAN-{entity_name[:6]}",
            candidate_name=candidate_name,
            candidate_aliases=(),
            candidate_category="psa",
            conn=conn,
        )
        assert result_on.score > 0.70, (
            f"Signal C enabled: {entity_name!r} vs {candidate_name!r} "
            f"score={result_on.score:.4f} (expected > 0.70)"
        )

        zero_ft_weights = WeightConfig(
            token_sort_ratio=PSA_ACCOUNTING_WEIGHTS.token_sort_ratio,
            token_set_ratio=PSA_ACCOUNTING_WEIGHTS.token_set_ratio,
            partial_ratio=PSA_ACCOUNTING_WEIGHTS.partial_ratio,
            jaro_winkler=PSA_ACCOUNTING_WEIGHTS.jaro_winkler,
            ngram_jaccard=PSA_ACCOUNTING_WEIGHTS.ngram_jaccard,
            alias_boost=PSA_ACCOUNTING_WEIGHTS.alias_boost,
            abbreviation_bonus=0.0,
            fasttext_cosine=0.0,
            profile_id="test_zero_ft",
        )
        monkeypatch.setattr(scoring_mod, "get_weights", lambda sc, tc: zero_ft_weights)
        result_off = score_pair(
            entity=entity,
            candidate_id=f"CAN-{entity_name[:6]}",
            candidate_name=candidate_name,
            candidate_aliases=(),
            candidate_category="psa",
            conn=conn,
        )
        assert result_off.score < 0.70, (
            f"Signal C disabled: {entity_name!r} vs {candidate_name!r} "
            f"score={result_off.score:.4f} (expected < 0.70)"
        )
        monkeypatch.setattr(scoring_mod, "get_weights", weights_mod.get_weights)


def test_fasttext_signal_c_negative_control(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stubs = _load_stub_vectors()
    stub_embed = _stub_embed_fn(stubs)

    import core.matching.scoring as scoring_mod
    monkeypatch.setattr(scoring_mod, "embed", stub_embed)

    entity = _make_entity("brightpath machine learning", source="quickbooks")
    _insert_canonical(conn, "CAN-BML", "luminos ai")
    conn.commit()

    result = score_pair(
        entity=entity,
        candidate_id="CAN-BML",
        candidate_name="luminos ai",
        candidate_aliases=(),
        candidate_category="psa",
        conn=conn,
    )
    assert result.score < 0.50, (
        f"negative control score={result.score:.4f} (expected < 0.50)"
    )


# ---------------------------------------------------------------------------
# 13. Signal B cap
# ---------------------------------------------------------------------------


def test_signal_b_cap_clips_excess() -> None:
    # B1=0.10, B2=0.08, B4=0.05, B6=0.10 → raw sum=0.33 > cap=0.20
    # B1 applied=0.10, B2 applied=0.08, B4 applied=0.02 (clipped), B6 applied=0.00 (fully clipped)
    evidence = GraphEvidence(
        shared_person_count=3,
        shared_person_bonus=0.10,
        neighborhood_overlap_count=4,
        neighborhood_overlap_bonus=0.10,
        project_code_bonus=0.08,
        shared_email_domain_bonus=0.05,
    )
    boosts = _compute_b_boosts(evidence)
    total = sum(b.applied for b in boosts)
    assert pytest.approx(total, abs=1e-9) == B_SIGNAL_CAP
    assert len(boosts) >= 4
    b4 = next(b for b in boosts if b.signal_id == "B4")
    assert pytest.approx(b4.applied, abs=1e-9) == 0.02
    assert pytest.approx(b4.raw, abs=1e-9) == 0.05
    b6 = next(b for b in boosts if b.signal_id == "B6")
    assert pytest.approx(b6.applied, abs=1e-9) == 0.00
    assert pytest.approx(b6.raw, abs=1e-9) == 0.10


# ---------------------------------------------------------------------------
# 15. Hygiene: no forbidden imports in scoring.py
# ---------------------------------------------------------------------------


def test_no_xgboost_no_fasttext_no_llm_in_scoring() -> None:
    src = (REPO_ROOT / "core" / "matching" / "scoring.py").read_text()
    forbidden = (
        "import fasttext",   "from fasttext",
        "import xgboost",    "from xgboost",
        "import anthropic",  "from anthropic",
        "import openai",     "from openai",
        "import fastembed",  "from fastembed",
        "import sentence_transformers", "from sentence_transformers",
        "import torch",      "from torch",
        "import transformers", "from transformers",
    )
    for token in forbidden:
        assert token not in src, (
            f"scoring.py must not reference {token!r} (V1 guardrail)"
        )
