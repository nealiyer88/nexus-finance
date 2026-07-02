"""Tests for core/matching/embeddings.py — Stage 2c / Signal Set C helpers."""

from __future__ import annotations

import math

import pytest

from core.matching.embeddings import _model_present, cosine, embed


def test_embed_empty_returns_none() -> None:
    assert embed("") is None


def test_embed_whitespace_returns_none() -> None:
    assert embed("   ") is None


def test_cosine_none_operand_returns_zero() -> None:
    assert cosine(None, (1.0, 0.0)) == 0.0
    assert cosine((1.0, 0.0), None) == 0.0
    assert cosine(None, None) == 0.0


def test_cosine_identical_returns_one() -> None:
    vec = (1.0, 0.0, 0.0)
    result = cosine(vec, vec)
    assert math.isclose(result, 1.0, abs_tol=1e-9)


def test_cosine_orthogonal_returns_zero() -> None:
    va = (1.0, 0.0)
    vb = (0.0, 1.0)
    result = cosine(va, vb)
    assert math.isclose(result, 0.0, abs_tol=1e-9)


@pytest.mark.skipif(not _model_present(), reason="fasttext model not downloaded")
def test_embed_returns_tuple_of_floats_for_normal_word() -> None:
    result = embed("technology")
    assert result is not None
    assert isinstance(result, tuple)
    assert all(isinstance(x, float) for x in result)
    assert len(result) > 0
