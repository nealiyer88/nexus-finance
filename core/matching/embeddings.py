"""Pre-trained fastText embedding helpers for Stage 2c (blocking ANN) and
Stage 3 Signal Set C (cosine similarity).

Model loading is lazy — the singleton is created on first call to `embed()`
and cached. When the model file is absent (CI / fresh checkout without
`scripts/fetch_fasttext.py` run), all calls return None / 0.0 without
raising. Integration tests that need the model are gated with
`@pytest.mark.skipif(not _model_present(), ...)`.
"""

from __future__ import annotations

import math
import pathlib
from typing import Optional

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODEL_PATH = _REPO_ROOT / "models" / "cc.en.300-compress.bin"

_model = None
_load_attempted = False


def _model_present() -> bool:
    return MODEL_PATH.exists()


def _load_model():
    global _model, _load_attempted
    if _load_attempted:
        return _model
    _load_attempted = True
    if not MODEL_PATH.exists():
        return None
    try:
        from compress_fasttext.models import CompressedFastTextKeyedVectors  # type: ignore[import]
        _model = CompressedFastTextKeyedVectors.load(str(MODEL_PATH))
    except Exception:
        _model = None
    return _model


def embed(name: str) -> Optional[tuple[float, ...]]:
    """Return the average fastText word vector for `name` as a float tuple.

    Returns None for empty / whitespace-only input or when the model file
    is absent. Never raises.
    """
    if not name or not name.strip():
        return None
    model = _load_model()
    if model is None:
        return None
    words = name.lower().split()
    vectors: list[list[float]] = []
    for w in words:
        try:
            vec = model[w]
            vectors.append(list(float(x) for x in vec))
        except Exception:
            pass
    if not vectors:
        return None
    dim = len(vectors[0])
    avg = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    return tuple(avg)


def cosine(
    a: Optional[tuple[float, ...]],
    b: Optional[tuple[float, ...]],
) -> float:
    """Cosine similarity between two pre-computed embedding tuples.

    Returns 0.0 when either operand is None or a zero vector. Never raises.
    """
    if a is None or b is None:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
