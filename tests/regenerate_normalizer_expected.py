"""Regenerate tests/fixtures/normalizer_expected.json from the current
normalizer rule set.

Regeneration requires human review of diff before commit.

Usage:
    python tests/regenerate_normalizer_expected.py

Reads:
    tests/fixtures/qb_entities.json
    tests/fixtures/ruddr_entities.json

Writes:
    tests/fixtures/normalizer_expected.json
        flat dict: {fixture_id: normalized_name}, sorted by id.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.ingestion.normalizer import normalize_entity  # noqa: E402


FIXTURE_DIR = REPO / "tests" / "fixtures"
QB_FIXTURE = FIXTURE_DIR / "qb_entities.json"
RUDDR_FIXTURE = FIXTURE_DIR / "ruddr_entities.json"
OUT = FIXTURE_DIR / "normalizer_expected.json"


def main() -> None:
    out: dict[str, str] = {}
    for path in (QB_FIXTURE, RUDDR_FIXTURE):
        with path.open() as f:
            entities = json.load(f)
        for entity in entities:
            ne = normalize_entity(entity)
            out[entity["id"]] = ne.normalized_name

    sorted_out = {k: out[k] for k in sorted(out)}
    with OUT.open("w") as f:
        json.dump(sorted_out, f, indent=2, sort_keys=False)
        f.write("\n")
    print(f"Wrote {len(sorted_out)} entries to {OUT}")


if __name__ == "__main__":
    main()
