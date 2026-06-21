"""Tests for core.matching.embeddings: embed() and cosine() functions."""

from __future__ import annotations

import json
import math
import pathlib

import pytest

from core.matching.embeddings import cosine, embed

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
STUB_VECTORS_PATH = REPO_ROOT / "tests" / "fixtures" / "stub_vectors.json"


def _load_stub_vectors() -> dict[str, tuple[float, ...]]:
    raw = json.loads(STUB_VECTORS_PATH.read_text())
    return {k: tuple(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# embed() null-input guards
# ---------------------------------------------------------------------------


def test_embed_empty_string_returns_none() -> None:
    assert embed("") is None


def test_embed_whitespace_only_returns_none() -> None:
    assert embed("   ") is None


# ---------------------------------------------------------------------------
# cosine() null guards
# ---------------------------------------------------------------------------


def test_cosine_both_none_returns_zero() -> None:
    assert cosine(None, None) == 0.0


def test_cosine_first_none_returns_zero() -> None:
    assert cosine(None, (1.0, 0.0)) == 0.0


def test_cosine_second_none_returns_zero() -> None:
    assert cosine((1.0, 0.0), None) == 0.0


# ---------------------------------------------------------------------------
# cosine() correctness
# ---------------------------------------------------------------------------


def test_cosine_orthogonal_vectors_returns_zero() -> None:
    assert cosine((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)


def test_cosine_identical_vectors_returns_one() -> None:
    assert cosine((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)


def test_cosine_zero_magnitude_returns_zero() -> None:
    assert cosine((0.0, 0.0), (1.0, 0.0)) == 0.0


# ---------------------------------------------------------------------------
# Stub vector cosines
# ---------------------------------------------------------------------------


def test_stub_pacrim_cosine() -> None:
    vecs = _load_stub_vectors()
    a = vecs["pacrim tech"]
    b = vecs["pacific rim technologies international"]
    result = cosine(a, b)
    assert result == pytest.approx(0.9, abs=1e-4)


def test_stub_meridian_cosine() -> None:
    vecs = _load_stub_vectors()
    a = vecs["meridian cap"]
    b = vecs["meridian capital group"]
    result = cosine(a, b)
    assert result == pytest.approx(0.9, abs=1e-4)


def test_stub_negative_control_cosine() -> None:
    vecs = _load_stub_vectors()
    a = vecs["brightpath machine learning"]
    b = vecs["luminos ai"]
    result = cosine(a, b)
    assert result == pytest.approx(0.0, abs=1e-9)


def test_stub_vector_magnitudes_are_unit() -> None:
    vecs = _load_stub_vectors()
    for name, vec in vecs.items():
        mag = math.sqrt(sum(x * x for x in vec))
        assert mag == pytest.approx(1.0, abs=1e-4), (
            f"stub vector {name!r} has magnitude {mag:.6f}, expected 1.0"
        )


# ---------------------------------------------------------------------------
# Optional integration test (skipped when model absent)
# ---------------------------------------------------------------------------


def test_embed_real_model_optional() -> None:
    model_path = REPO_ROOT / "models" / "cc.en.300.bin"
    if not model_path.exists():
        pytest.skip("model file not present; skipping integration test")
    import core.matching.embeddings as emb_mod
    emb_mod._MODEL = None
    emb_mod._MODEL_LOAD_ATTEMPTED = False
    vec = embed("cenlar fsb")
    assert vec is not None
    assert len(vec) > 0
