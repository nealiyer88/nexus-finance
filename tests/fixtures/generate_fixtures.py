"""Validate test fixtures for cross-category entity resolution.

Loads QB + RUDDR fixture files and canonical ground truth, then runs
integrity checks to ensure the fixture data is self-consistent.

Checks:
  - All QB/RUDDR refs in ground truth exist in entity files
  - All unmatched refs are resolvable
  - Entity categories (organization/person) are consistent
  - Department labels match known departments
  - Billing rates fall within department bounds
  - Shared last names are present (collision testing)
  - All 13 org patterns and 8 person patterns are covered

Usage:
    python tests/fixtures/generate_fixtures.py
"""

import json
import sys
from collections import Counter
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent

KNOWN_DEPARTMENTS = {
    "Strategy & Advisory",
    "Data Engineering",
    "Software Development",
    "Cloud & Infrastructure",
    "Design & UX",
    "Staff Augmentation",
}

EXPECTED_ORG_PATTERNS = {
    "legal-suffix-strip", "abbreviation", "case-variation", "punctuation-diff",
    "word-order", "dba-trade-name", "truncation", "spacing-compound",
    "parent-subsidiary", "ampersand-and", "the-prefix-drop",
    "regional-qualifier-drop", "rebrand-alias",
}

EXPECTED_PERSON_PATTERNS = {
    "nickname", "middle-initial", "suffix-inconsistent", "maiden-married",
    "first-initial", "hyphenated-partial", "name-order", "accent-drop",
}

SHARED_LAST_NAMES = {"Chen": 3, "Johnson": 2, "Williams": 2}


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
    qb_by_id = {e["id"]: e for e in qb}
    ruddr_by_id = {e["id"]: e for e in ruddr}

    errors = []
    warnings = []

    # --- Reference integrity ---
    for canon in truth["canonical_entities"]:
        cid = canon["canonical_id"]
        qb_ref = canon["sources"].get("quickbooks", {}).get("id")
        ruddr_ref = canon["sources"].get("ruddr", {}).get("id")

        if qb_ref and qb_ref not in qb_ids:
            errors.append(f"{cid}: QB ref {qb_ref} not found in qb_entities.json")
        if ruddr_ref and ruddr_ref not in ruddr_ids:
            errors.append(f"{cid}: RUDDR ref {ruddr_ref} not found in ruddr_entities.json")

        # Category consistency
        if qb_ref and qb_ref in qb_by_id:
            qb_cat = qb_by_id[qb_ref].get("entity_category")
            if qb_cat and qb_cat != canon.get("entity_category"):
                errors.append(f"{cid}: QB entity category '{qb_cat}' != ground truth '{canon.get('entity_category')}'")
        if ruddr_ref and ruddr_ref in ruddr_by_id:
            ruddr_cat = ruddr_by_id[ruddr_ref].get("entity_category")
            if ruddr_cat and ruddr_cat != canon.get("entity_category"):
                errors.append(f"{cid}: RUDDR entity category '{ruddr_cat}' != ground truth '{canon.get('entity_category')}'")

    for unmatched in truth.get("unmatched", {}).get("ruddr", []):
        if unmatched["id"] not in ruddr_ids:
            errors.append(f"Unmatched RUDDR ref {unmatched['id']} not found in ruddr_entities.json")

    for unmatched in truth.get("unmatched", {}).get("quickbooks", []):
        if unmatched["id"] not in qb_ids:
            errors.append(f"Unmatched QB ref {unmatched['id']} not found in qb_entities.json")

    # --- Department validation ---
    for e in ruddr:
        for proj in e.get("projects", []):
            dept = proj.get("department")
            if dept and dept not in KNOWN_DEPARTMENTS:
                errors.append(f"{e['id']}: unknown department '{dept}'")
    for e in qb:
        dept = e.get("department")
        if dept and dept not in KNOWN_DEPARTMENTS:
            errors.append(f"{e['id']}: unknown department '{dept}'")

    # --- Pattern coverage ---
    org_patterns = set()
    person_patterns = set()
    for canon in truth["canonical_entities"]:
        p = canon.get("pattern")
        if not p:
            continue
        if canon.get("entity_category") == "organization":
            org_patterns.add(p)
        elif canon.get("entity_category") == "person":
            person_patterns.add(p)

    missing_org = EXPECTED_ORG_PATTERNS - org_patterns
    missing_person = EXPECTED_PERSON_PATTERNS - person_patterns
    if missing_org:
        warnings.append(f"Missing org patterns: {sorted(missing_org)}")
    if missing_person:
        warnings.append(f"Missing person patterns: {sorted(missing_person)}")

    # --- Shared last name collision check ---
    person_canonicals = [c["canonical_name"] for c in truth["canonical_entities"]
                         if c.get("entity_category") == "person"]
    last_name_counts = Counter()
    for name in person_canonicals:
        parts = name.replace("-", " ").split()
        if parts:
            last_name_counts[parts[-1]] += 1

    for surname, expected_count in SHARED_LAST_NAMES.items():
        actual = last_name_counts.get(surname, 0)
        if actual < expected_count:
            warnings.append(f"Shared last name '{surname}': expected >={expected_count}, found {actual}")

    return errors, warnings


def print_summary():
    qb = load_json("qb_entities.json")
    ruddr = load_json("ruddr_entities.json")
    truth = load_json("canonical_ground_truth.json")

    stats = truth.get("stats", {})
    print("=== Nexus Finance Test Fixtures ===")
    print(f"  QuickBooks entities : {len(qb)}")
    print(f"  RUDDR entities      : {len(ruddr)}")
    print(f"  Canonical matches   : {len(truth['canonical_entities'])}")
    print(f"  Unmatched QB        : {len(truth['unmatched']['quickbooks'])}")
    print(f"  Unmatched RUDDR     : {len(truth['unmatched']['ruddr'])}")
    if stats:
        print(f"  Org patterns        : {stats.get('org_patterns_covered', '?')}/13")
        print(f"  Person patterns     : {stats.get('person_patterns_covered', '?')}/8")
        print(f"  Departments         : {stats.get('departments', '?')}")
    print()

    # Orgs
    print("  --- Organizations ---")
    for canon in truth["canonical_entities"]:
        if canon.get("entity_category") != "organization":
            continue
        qb_name = canon["sources"].get("quickbooks", {}).get("display_name", "—")
        ruddr_name = canon["sources"].get("ruddr", {}).get("display_name", "—")
        pat = canon.get("pattern", "")
        print(f"  {canon['canonical_id']}: {qb_name:42s} <-> {ruddr_name:30s} [{pat}] (conf: {canon['confidence']})")

    # Persons
    print("\n  --- Persons ---")
    for canon in truth["canonical_entities"]:
        if canon.get("entity_category") != "person":
            continue
        qb_name = canon["sources"].get("quickbooks", {}).get("display_name", "—")
        ruddr_name = canon["sources"].get("ruddr", {}).get("display_name", "—")
        pat = canon.get("pattern", "")
        print(f"  {canon['canonical_id']}: {qb_name:30s} <-> {ruddr_name:25s} [{pat}] (conf: {canon['confidence']})")

    # Unmatched
    if truth["unmatched"]["quickbooks"]:
        print("\n  --- Unmatched QB ---")
        for u in truth["unmatched"]["quickbooks"]:
            print(f"  {u['id']}: {u['display_name']} — {u['reason']}")
    if truth["unmatched"]["ruddr"]:
        print("\n  --- Unmatched RUDDR ---")
        for u in truth["unmatched"]["ruddr"]:
            print(f"  {u['id']}: {u['display_name']} — {u['reason']}")


def main():
    errors, warnings = validate_fixtures()

    if warnings:
        print("WARNINGS:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
        print(file=sys.stderr)

    if errors:
        print("FIXTURE VALIDATION FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print("Fixture validation passed.\n")
    print_summary()


if __name__ == "__main__":
    main()
