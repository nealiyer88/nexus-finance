"""Performance smoke test for the normalizer.

Asserts that processing all 91 fixture entities completes in under 500ms.
This is a generous bound — the normalizer is pure regex / unicode work,
no I/O — and the threshold exists to catch accidental algorithmic regressions
(e.g. someone adds a slow library or quadratic loop).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.ingestion.normalizer import normalize_entity  # noqa: E402


FIXTURE_DIR = REPO / "tests" / "fixtures"


def test_perf_under_500ms():
    qb = json.loads((FIXTURE_DIR / "qb_entities.json").read_text())
    rd = json.loads((FIXTURE_DIR / "ruddr_entities.json").read_text())
    fixtures = qb + rd
    assert len(fixtures) == 91

    start = time.perf_counter()
    for entity in fixtures:
        normalize_entity(entity)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert elapsed_ms < 500.0, (
        f"normalizing 91 entities took {elapsed_ms:.2f}ms (limit 500ms)"
    )
