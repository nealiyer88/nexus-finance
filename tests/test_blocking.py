"""Tests for Pipeline Stage 2: Blocking + inverted indices."""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import sqlite3
from typing import Optional

import pytest

from connectors.base import NormalizedEntity
from core.ingestion.normalizer import normalize_entity
from core.matching.blocking import CANDIDATE_CAP, generate_candidates
from core.matching.deterministic import deterministic_match
from core.matching.indices import EmbeddingIndex, NgramIndex, TokenIndex
from core.matching.types import CandidateSet


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = REPO_ROOT / "db" / "schema_sqlite.sql"
FIXTURE_GT = REPO_ROOT / "tests" / "fixtures" / "canonical_ground_truth.json"
FIXTURE_QB = REPO_ROOT / "tests" / "fixtures" / "qb_entities.json"
FIXTURE_RUDDR = REPO_ROOT / "tests" / "fixtures" / "ruddr_entities.json"

_SOURCE_TO_CATEGORY = {"quickbooks": "accounting", "ruddr": "psa"}


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SQLITE_SCHEMA.read_text())
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _insert_sysref(
    conn: sqlite3.Connection,
    canonical_id: str,
    source: str,
    category: str,
    external_id: str,
    external_fields: Optional[dict] = None,
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
            json.dumps(external_fields or {}),
        ),
    )


def _entity(
    *,
    normalized_name: str,
    source: str = "quickbooks",
    source_id: str = "QB-NEW",
    entity_category: str = "organization",
) -> NormalizedEntity:
    return NormalizedEntity(
        raw_name=normalized_name,
        normalized_name=normalized_name,
        entity_category=entity_category,
        source=source,
        category="accounting" if source == "quickbooks" else "psa",
        source_id=source_id,
        email=None,
        email_is_person=entity_category == "person",
        raw_record={},
        rules_applied=[],
    )


# ---------------------------------------------------------------------------
# Trigram + index unit tests
# ---------------------------------------------------------------------------


def test_trigrams_short_name_padded() -> None:
    grams = NgramIndex.trigrams("ibm")
    assert grams == ("^ib", "ibm", "bm$")


def test_trigrams_two_char_padded() -> None:
    grams = NgramIndex.trigrams("hp")
    assert grams == ("^hp", "hp$")


def test_trigrams_empty_string_yields_no_grams() -> None:
    assert NgramIndex.trigrams("") == ()


def test_token_index_lookup(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-T1", "acme widgets")
    _insert_canonical(conn, "CAN-T2", "widgets unlimited")
    conn.commit()

    idx = TokenIndex.build(conn)

    assert idx.lookup(["acme"]) == {"CAN-T1"}
    assert idx.lookup(["widgets"]) == {"CAN-T1", "CAN-T2"}
    assert idx.lookup(["nonexistent"]) == set()


def test_ngram_index_lookup(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-N1", "cenlar")
    conn.commit()

    idx = NgramIndex.build(conn)

    assert "CAN-N1" in idx.lookup("cenlarr")
    assert idx.lookup("") == set()
    assert idx.lookup("   ") == set()


def test_token_index_indexes_aliases_too(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-AL", "primary name")
    _insert_alias(conn, "CAN-AL", "alternate label")
    conn.commit()

    idx = TokenIndex.build(conn)
    assert idx.lookup(["alternate"]) == {"CAN-AL"}
    assert idx.lookup(["primary"]) == {"CAN-AL"}


def test_token_index_tenant_filter(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-TA", "acme", tenant_id="A")
    _insert_canonical(conn, "CAN-TB", "acme", tenant_id="B")
    conn.commit()

    idx_a = TokenIndex.build(conn, tenant_id="A")
    idx_b = TokenIndex.build(conn, tenant_id="B")
    idx_all = TokenIndex.build(conn)

    assert idx_a.lookup(["acme"]) == {"CAN-TA"}
    assert idx_b.lookup(["acme"]) == {"CAN-TB"}
    assert idx_all.lookup(["acme"]) == {"CAN-TA", "CAN-TB"}


# ---------------------------------------------------------------------------
# generate_candidates behaviors
# ---------------------------------------------------------------------------


def test_token_hit_produces_candidate(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-W", "acme widgets")
    _insert_sysref(conn, "CAN-W", "ruddr", "psa", "RUDDR-W", {})
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity = _entity(
        normalized_name="acme widgets",
        source="quickbooks",
        source_id="QB-NEW",
    )
    result = generate_candidates(entity, tok, ngm, conn)

    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.canonical_id == "CAN-W"
    token_signals = [s for s in cand.blocking_signals if s.startswith("token:")]
    assert "token:acme" in token_signals
    assert "token:widgets" in token_signals
    assert list(cand.blocking_signals) == sorted(cand.blocking_signals)


def test_trigram_hit_produces_candidate(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-G", "cenlar")
    _insert_sysref(conn, "CAN-G", "ruddr", "psa", "RUDDR-G", {})
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity = _entity(
        normalized_name="cenlarr",
        source="quickbooks",
        source_id="QB-FOREIGN",
    )
    result = generate_candidates(entity, tok, ngm, conn)

    assert len(result.candidates) == 1
    assert any(s.startswith("trigram:") for s in result.candidates[0].blocking_signals)


def test_empty_normalized_name_returns_empty_candidate_set(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-E", "any")
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity = NormalizedEntity(
        raw_name="",
        normalized_name="",
        entity_category="organization",
        source="quickbooks",
        category="accounting",
        source_id="QB-EMPTY",
        email=None,
        email_is_person=False,
        raw_record={},
        rules_applied=[],
    )
    result = generate_candidates(entity, tok, ngm, conn)

    assert isinstance(result, CandidateSet)
    assert result.source_entity_id == "QB-EMPTY"
    assert result.candidates == ()


def test_intra_system_exclusion(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-IS", "shared name")
    _insert_sysref(conn, "CAN-IS", "quickbooks", "accounting", "QB-OWN", {})
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity = _entity(
        normalized_name="shared name",
        source="quickbooks",
        source_id="QB-OWN",
    )
    result = generate_candidates(entity, tok, ngm, conn)
    assert result.candidates == ()


def test_intra_system_non_exclusion_when_canonical_spans_sources(
    conn: sqlite3.Connection,
) -> None:
    _insert_canonical(conn, "CAN-SP", "spanning")
    _insert_sysref(conn, "CAN-SP", "quickbooks", "accounting", "QB-100", {})
    _insert_sysref(conn, "CAN-SP", "ruddr", "psa", "RUDDR-100", {})
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity = _entity(
        normalized_name="spanning",
        source="quickbooks",
        source_id="QB-200",
    )
    result = generate_candidates(entity, tok, ngm, conn)

    assert len(result.candidates) == 1
    assert result.candidates[0].canonical_id == "CAN-SP"


def test_no_signal_overlap_returns_empty(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-X", "zulu")
    _insert_sysref(conn, "CAN-X", "ruddr", "psa", "RUDDR-X", {})
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity = _entity(
        normalized_name="unrelated stuff",
        source="quickbooks",
        source_id="QB-UNREL",
    )
    result = generate_candidates(entity, tok, ngm, conn)
    assert result.candidates == ()


def test_candidate_cap_enforced(
    conn: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    overflow = CANDIDATE_CAP + 10
    for i in range(overflow):
        cid = f"CAN-CAP-{i:03d}"
        _insert_canonical(conn, cid, "consulting")
        _insert_sysref(conn, cid, "ruddr", "psa", f"RUDDR-CAP-{i}", {})
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity = _entity(
        normalized_name="consulting",
        source="quickbooks",
        source_id="QB-CAP",
    )

    with caplog.at_level(logging.WARNING, logger="core.matching.blocking"):
        result = generate_candidates(entity, tok, ngm, conn)

    assert len(result.candidates) == CANDIDATE_CAP
    assert any("candidate cap exceeded" in r.message for r in caplog.records)


def test_candidate_set_source_entity_id_matches_query(conn: sqlite3.Connection) -> None:
    _insert_canonical(conn, "CAN-ID", "foo")
    _insert_sysref(conn, "CAN-ID", "ruddr", "psa", "RUDDR-ID", {})
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity = _entity(
        normalized_name="foo",
        source="quickbooks",
        source_id="QB-RT-7",
    )
    result = generate_candidates(entity, tok, ngm, conn)
    assert result.source_entity_id == "QB-RT-7"


# ---------------------------------------------------------------------------
# Hygiene: no rapidfuzz import in Stages 1–2
# ---------------------------------------------------------------------------


def test_no_rapidfuzz_in_matching_modules() -> None:
    targets = [
        REPO_ROOT / "core" / "matching" / "indices.py",
        REPO_ROOT / "core" / "matching" / "deterministic.py",
        REPO_ROOT / "core" / "matching" / "blocking.py",
        REPO_ROOT / "core" / "matching" / "types.py",
        REPO_ROOT / "core" / "graph" / "entity_store.py",
    ]
    for p in targets:
        src = p.read_text()
        assert "import rapidfuzz" not in src, f"{p} must not import rapidfuzz"
        assert "from rapidfuzz" not in src, f"{p} must not import from rapidfuzz"


def test_dataclasses_are_frozen() -> None:
    from core.matching.types import (
        CandidateEntity,
        CandidateSet,
        DeterministicMatch,
    )

    dm = DeterministicMatch(canonical_id="x", confidence=0.9, match_key_type="email")
    with pytest.raises(dataclasses.FrozenInstanceError):
        dm.canonical_id = "y"  # type: ignore[misc]

    ce = CandidateEntity(canonical_id="x", blocking_signals=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        ce.canonical_id = "y"  # type: ignore[misc]

    cs = CandidateSet(source_entity_id="x", candidates=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        cs.source_entity_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fixture end-to-end: every canonical resolves or has a non-empty candidate
# set under at least one of its QB / RUDDR source-record queries.
# ---------------------------------------------------------------------------


def _seed_full_fixture(
    conn: sqlite3.Connection,
    *,
    sysref_sources: tuple[str, ...] = ("ruddr",),
) -> dict[str, dict]:
    """Load all 44 canonicals + aliases (from both source display_names) plus
    system_references only for `sysref_sources`. Restricting sysrefs to one
    side lets the end-to-end test query through the OTHER side's raw record
    without tripping the Stage 2d intra-system filter."""
    gt = json.loads(FIXTURE_GT.read_text())
    canonicals = gt["canonical_entities"]
    by_id: dict[str, dict] = {ent["canonical_id"]: ent for ent in canonicals}

    for ent in canonicals:
        _insert_canonical(
            conn,
            ent["canonical_id"],
            _seed_normalized_name(ent["canonical_name"], ent["entity_category"]),
            entity_type=ent["entity_type"],
            entity_category=ent["entity_category"],
        )
        for source_key, source_rec in ent.get("sources", {}).items():
            category = _SOURCE_TO_CATEGORY.get(source_key, source_key)
            if source_key in sysref_sources:
                _insert_sysref(
                    conn,
                    ent["canonical_id"],
                    source_key,
                    category,
                    source_rec["id"],
                    source_rec,
                )
            display = source_rec.get("display_name")
            if isinstance(display, str) and display.strip():
                seed = _seed_normalized_name(display, ent["entity_category"])
                _insert_alias(
                    conn,
                    ent["canonical_id"],
                    seed,
                    source=source_key,
                    category=category,
                    confidence=0.95,
                )
    conn.commit()
    return by_id


def _seed_normalized_name(name: str, entity_category: str) -> str:
    norm = normalize_entity(
        {
            "id": "SEED",
            "source": "quickbooks",
            "entity_category": entity_category,
            "display_name": name,
        }
    )
    return norm.normalized_name


def test_fixture_end_to_end_each_canonical_reachable(conn: sqlite3.Connection) -> None:
    # Seed RUDDR-side sysrefs only; route QB raw records as the query side.
    # That way the Stage 2d intra-system filter (which excludes any candidate
    # that already has the query's (source, source_id) on file) does not
    # remove the true canonical.
    by_id = _seed_full_fixture(conn, sysref_sources=("ruddr",))

    qb_raw = json.loads(FIXTURE_QB.read_text())
    qb_by_id = {r["id"]: r for r in qb_raw}

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    checked = 0
    for canonical_id, ent in by_id.items():
        sources = ent.get("sources", {})
        if "quickbooks" not in sources or "ruddr" not in sources:
            continue
        qb_raw_rec = qb_by_id.get(sources["quickbooks"]["id"])
        if qb_raw_rec is None:
            continue
        norm = normalize_entity(qb_raw_rec)

        det = deterministic_match(norm, conn)
        if det is not None:
            assert det.canonical_id == canonical_id, (
                f"deterministic match for {qb_raw_rec['id']} resolved to "
                f"{det.canonical_id}; expected {canonical_id}"
            )
            checked += 1
            continue

        result = generate_candidates(norm, tok, ngm, conn)
        candidate_ids = {c.canonical_id for c in result.candidates}
        assert canonical_id in candidate_ids, (
            f"Stage 2 missed {canonical_id} for QB query {qb_raw_rec['id']!r} "
            f"(normalized={norm.normalized_name!r}); candidates={candidate_ids}"
        )
        assert len(result.candidates) <= CANDIDATE_CAP
        checked += 1

    assert checked >= 19, f"expected >= 19 cross-source canonicals; checked {checked}"


# ---------------------------------------------------------------------------
# Stage 2c: EmbeddingIndex surfaces abbreviation candidates
# ---------------------------------------------------------------------------


def test_stage_2c_embedding_surfaces_abbreviation_candidate(
    conn: sqlite3.Connection,
) -> None:
    # Use a canonical name with no trigram overlap with "pacrim tech" so that
    # token+trigram blocking alone cannot surface it.  The stub embed_fn maps
    # "northwest software" to a vector close to "pacrim tech", exercising the
    # embedding-only discovery path.
    _insert_canonical(conn, "CAN-PACRM", "northwest software")
    _insert_sysref(conn, "CAN-PACRM", "ruddr", "psa", "RUDDR-PACRM", {})
    conn.commit()

    tok = TokenIndex.build(conn)
    ngm = NgramIndex.build(conn)

    entity_pacrim_tech = _entity(
        normalized_name="pacrim tech",
        source="quickbooks",
        source_id="QB-PACRM",
    )

    # Without embedding index: abbreviation does NOT surface via token/trigram
    result_no_emb = generate_candidates(entity_pacrim_tech, tok, ngm, conn)
    candidate_ids_no_emb = {c.canonical_id for c in result_no_emb.candidates}
    assert "CAN-PACRM" not in candidate_ids_no_emb, (
        "token/trigram alone should NOT surface CAN-PACRM for 'pacrim tech'"
    )

    # Stub embed: "pacrim tech" -> [1,0,...], "northwest software" -> [0.9,0.44,...] (cosine 0.9)
    _STUB_VECS: dict[str, tuple[float, ...]] = {
        "pacrim tech":      (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "northwest software": (0.9, 0.43589, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    }

    def stub_embed(name: str):
        return _STUB_VECS.get(name.strip().lower())

    emb_idx = EmbeddingIndex.build(conn, embed_fn=stub_embed)

    # With embedding index: abbreviation IS surfaced
    result_with_emb = generate_candidates(
        entity_pacrim_tech, tok, ngm, conn, embedding_index=emb_idx
    )
    candidate_ids_with_emb = {c.canonical_id for c in result_with_emb.candidates}
    assert "CAN-PACRM" in candidate_ids_with_emb, (
        "embedding index should surface CAN-PACRM for 'pacrim tech'"
    )

    pacrm_cand = next(
        c for c in result_with_emb.candidates if c.canonical_id == "CAN-PACRM"
    )
    assert "embed:0" in pacrm_cand.blocking_signals, (
        f"expected 'embed:0' in blocking signals; got {pacrm_cand.blocking_signals}"
    )
