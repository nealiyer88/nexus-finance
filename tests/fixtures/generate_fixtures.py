"""Generate and validate test fixtures for cross-category entity resolution.

Loads QB + RUDDR fixture files and canonical ground truth, then runs
basic integrity checks to ensure the fixture data is self-consistent.

Usage:
    python tests/fixtures/generate_fixtures.py
"""

import json
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent


def load_json(filename: str):
    path = FIXTURES_DIR / filename
    with open(path) as f:
        return json.load(f)


def validate_fixtures():
    qb = load_json("qb_entities.json")
    ruddr = load_json("ruddr_entities.json")
    truth = load_json("canonical_ground_truth.json")

    qb_ids = {e["id"] for e in qb}
    ruddr_ids = {e["id"] for e in ruddr}

    errors = []

    for canon in truth["canonical_entities"]:
        cid = canon["canonical_id"]
        qb_ref = canon["sources"].get("quickbooks", {}).get("id")
        ruddr_ref = canon["sources"].get("ruddr", {}).get("id")

        if qb_ref and qb_ref not in qb_ids:
            errors.append(f"{cid}: QB ref {qb_ref} not found in qb_entities.json")
        if ruddr_ref and ruddr_ref not in ruddr_ids:
            errors.append(f"{cid}: RUDDR ref {ruddr_ref} not found in ruddr_entities.json")

    for unmatched in truth.get("unmatched", {}).get("ruddr", []):
        if unmatched["id"] not in ruddr_ids:
            errors.append(f"Unmatched RUDDR ref {unmatched['id']} not found in ruddr_entities.json")

    for unmatched in truth.get("unmatched", {}).get("quickbooks", []):
        if unmatched["id"] not in qb_ids:
            errors.append(f"Unmatched QB ref {unmatched['id']} not found in qb_entities.json")

    return errors


def print_summary():
    qb = load_json("qb_entities.json")
    ruddr = load_json("ruddr_entities.json")
    truth = load_json("canonical_ground_truth.json")

    print("=== Nexus Finance Test Fixtures ===")
    print(f"  QuickBooks entities : {len(qb)}")
    print(f"  RUDDR entities      : {len(ruddr)}")
    print(f"  Canonical matches   : {len(truth['canonical_entities'])}")
    print(f"  Unmatched QB        : {len(truth['unmatched']['quickbooks'])}")
    print(f"  Unmatched RUDDR     : {len(truth['unmatched']['ruddr'])}")
    print()

    for canon in truth["canonical_entities"]:
        qb_name = canon["sources"].get("quickbooks", {}).get("display_name", "—")
        ruddr_name = canon["sources"].get("ruddr", {}).get("display_name", "—")
        print(f"  {canon['canonical_id']}: {qb_name:35s} <-> {ruddr_name:30s} (conf: {canon['confidence']})")


def main():
    errors = validate_fixtures()
    if errors:
        print("FIXTURE VALIDATION FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print("Fixture validation passed.\n")
    print_summary()


if __name__ == "__main__":
    main()
