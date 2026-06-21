"""Pre-trained fastText embedding loader for Stage 2c blocking and Stage 3 Signal Set C.

Model file: models/cc.en.300.bin (relative to repo root).
Loaded lazily on first non-empty embed() call via compress-fasttext (pure Python, no C++).
"""

from __future__ import annotations

import logging
import math
import pathlib
from typing import Optional

_logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_LOAD_ATTEMPTED = False

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MODEL_PATH = _REPO_ROOT / "models" / "cc.en.300.bin"


def _load_model() -> None:
    global _MODEL, _MODEL_LOAD_ATTEMPTED
    if _MODEL_LOAD_ATTEMPTED:
        return
    _MODEL_LOAD_ATTEMPTED = True
    if not _MODEL_PATH.exists():
        _logger.warning(
            "fastText model not found at %s; embed() will return None. "
            "Run scripts/fetch_fasttext.py to download.",
            _MODEL_PATH,
        )
        return
    try:
        import compress_fasttext
        _MODEL = compress_fasttext.models.CompressedFastTextKeyedVectors.load(
            str(_MODEL_PATH)
        )
    except Exception as exc:
        _logger.warning("Failed to load fastText model: %s", exc)
        _MODEL = None


def embed(normalized_name: str) -> Optional[tuple[float, ...]]:
    """Return the fastText embedding for `normalized_name`, or None.

    Returns None for empty/whitespace input without touching the model.
    Returns None if the model file is absent or fails to load.
    """
    if not normalized_name or not normalized_name.strip():
        return None
    _load_model()
    if _MODEL is None:
        return None
    try:
        vec = _MODEL[normalized_name.strip()]
        return tuple(float(v) for v in vec)
    except Exception:
        return None


def cosine(
    a: Optional[tuple[float, ...]],
    b: Optional[tuple[float, ...]],
) -> float:
    """Cosine similarity between two vectors. Returns 0.0 when either is None."""
    if a is None or b is None:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)
