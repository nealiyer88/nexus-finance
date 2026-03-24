"""Generate synthetic cross-category test data for Nexus Finance.

Produces QB + RUDDR fixture files with deliberate naming mismatches,
structural differences, and edge cases that exercise the entity
resolution engine.

Usage:
    python scripts/generate_test_data.py [--output-dir tests/fixtures]
"""

import argparse
import json
import random
import string
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# --- Templates ---

COMPANIES = [
    {
        "canonical": "Cenlar FSB",
        "qb_name": "Cenlar, LLC.",
        "ruddr_slug": "cenlar-fsb",
        "ruddr_name": "Cenlar FSB",
        "industry": "financial-services",
        "type": "client",
        "city": "Ewing",
        "state": "NJ",
    },
    {
        "canonical": "Meridian Capital Group",
        "qb_name": "Meridian Capital Group LLC",
        "ruddr_slug": "meridian-cap",
        "ruddr_name": "Meridian Capital",
        "industry": "financial-services",
        "type": "client",
        "city": "New York",
        "state": "NY",
    },
    {
        "canonical": "TechVentures",
        "qb_name": "TechVentures Inc",
        "ruddr_slug": "techventures-io",
        "ruddr_name": "TechVentures",
        "industry": "technology",
        "type": "client",
        "city": "San Francisco",
        "state": "CA",
    },
    {
        "canonical": "Apex Logistics",
        "qb_name": "Apex Logistics Co.",
        "ruddr_slug": "apex-log",
        "ruddr_name": "Apex Logistics",
        "industry": "logistics",
        "type": "client",
        "city": "Chicago",
        "state": "IL",
    },
    {
        "canonical": "Greenfield Analytics",
        "qb_name": "GreenField Analytics, LLC",
        "ruddr_slug": "greenfield-analytics",
        "ruddr_name": "Greenfield Analytics",
        "industry": "analytics",
        "type": "client",
        "city": "Austin",
        "state": "TX",
    },
    {
        "canonical": "Northstar Health Systems",
        "qb_name": "Northstar Health Systems",
        "ruddr_slug": "northstar-health",
        "ruddr_name": "NorthStar Health",
        "industry": "healthcare",
        "type": "client",
        "city": "Boston",
        "state": "MA",
    },
    {
        "canonical": "Summit Partners",
        "qb_name": "Summit Partners Capital",
        "ruddr_slug": "summit-partners",
        "ruddr_name": "Summit Partners",
        "industry": "financial-services",
        "type": "client",
        "city": "Boston",
        "state": "MA",
    },
    {
        "canonical": "DataBridge Solutions",
        "qb_name": "DataBridge Solutions",
        "ruddr_slug": "databridge-sol",
        "ruddr_name": "DataBridge",
        "industry": "technology",
        "type": "vendor",
        "city": "Reston",
        "state": "VA",
    },
]

# RUDDR-only entity (no QB match)
RUDDR_ONLY = [
    {
        "ruddr_slug": "riverstone-cap",
        "ruddr_name": "Riverstone Capital Advisors",
        "industry": "financial-services",
        "type": "client",
    }
]

PROJECT_NAMES = [
    "GenAI Platform Build",
    "Data Migration",
    "Lending Platform Redesign",
    "MLOps Infrastructure",
    "ERP Integration",
    "Executive Dashboard",
    "BI Platform Phase 2",
    "EMR Interoperability",
    "Portfolio Analytics Tool",
    "Staff Augmentation",
    "CRM Migration",
]


def random_phone():
    area = random.randint(200, 999)
    return f"{area}-555-{random.randint(100, 999):04d}"


def random_email(company_slug: str):
    prefixes = ["ap", "billing", "finance", "accounts", "invoices", "admin", "pay", "ar"]
    domain = company_slug.replace("-", "") + ".com"
    return f"{random.choice(prefixes)}@{domain}"


def random_date(start_year=2024, end_year=2025):
    start = datetime(start_year, 1, 1, tzinfo=timezone.utc)
    end = datetime(end_year, 12, 31, tzinfo=timezone.utc)
    delta = end - start
    rand_days = random.randint(0, delta.days)
    return (start + timedelta(days=rand_days)).isoformat()


def generate_qb_entity(idx: int, company: dict) -> dict:
    return {
        "id": f"QB-{idx:03d}",
        "source": "quickbooks",
        "display_name": company["qb_name"],
        "type": "Vendor" if company["type"] == "vendor" else "Customer",
        "email": random_email(company["ruddr_slug"]),
        "phone": random_phone(),
        "balance": round(random.uniform(5000, 120000) * (-1 if company["type"] == "vendor" else 1), 2),
        "currency": "USD",
        "address": {
            "line1": f"{random.randint(1, 2000)} {random.choice(['Main', 'Market', 'Congress', 'Phillips', 'Adams', 'Berkeley', 'Francis'])} St",
            "city": company["city"],
            "state": company["state"],
            "postal_code": f"{random.randint(10000, 99999)}",
        },
        "created_at": random_date(2024, 2024),
        "last_modified": random_date(2025, 2025),
    }


def generate_ruddr_entity(idx: int, company: dict, is_matched: bool = True) -> dict:
    project_count = random.randint(1, 3)
    projects = []
    for j in range(project_count):
        code_prefix = company["ruddr_slug"].split("-")[0].upper()[:3]
        suffix = "".join(random.choices(string.ascii_uppercase, k=random.randint(2, 5)))
        budget = random.choice([300, 400, 500, 600, 750, 800, 900, 1000, 1200, 2000])
        logged = round(random.uniform(50, budget * 0.95), 1)
        projects.append({
            "code": f"{code_prefix}-{suffix}-SOW{j + 1}",
            "name": random.choice(PROJECT_NAMES),
            "status": random.choice(["active", "active", "completed"]),
            "budget_hours": budget,
            "logged_hours": logged,
            "hourly_rate": round(random.uniform(150, 280), 2),
        })

    return {
        "id": f"RUDDR-{idx:03d}",
        "source": "ruddr",
        "slug": company["ruddr_slug"],
        "display_name": company.get("ruddr_name", company.get("canonical", "")),
        "type": company["type"],
        "projects": projects,
        "tags": [company.get("industry", "general"), random.choice(["enterprise", "mid-market", "startup", "boutique"])],
        "created_at": random_date(2024, 2024),
        "last_activity": random_date(2025, 2025),
    }


def generate_ground_truth(qb_entities, ruddr_entities, companies):
    canonical = []
    for i, company in enumerate(companies):
        qb_e = qb_entities[i]
        ruddr_e = ruddr_entities[i]

        signals = ["name_fuzzy"]
        if company["qb_name"].lower().replace(",", "").replace(".", "").split()[0] == \
           company["ruddr_name"].lower().split()[0]:
            signals.append("name_prefix_match")

        canonical.append({
            "canonical_id": f"CAN-{i + 1:03d}",
            "canonical_name": company["canonical"],
            "entity_type": company["type"],
            "sources": {
                "quickbooks": {"id": qb_e["id"], "display_name": qb_e["display_name"]},
                "ruddr": {"id": ruddr_e["id"], "slug": ruddr_e["slug"], "display_name": ruddr_e["display_name"]},
            },
            "match_signals": signals,
            "confidence": round(random.uniform(0.88, 0.99), 2),
        })

    unmatched_ruddr = []
    for j, ro in enumerate(RUDDR_ONLY):
        ruddr_idx = len(companies) + j + 1
        unmatched_ruddr.append({
            "id": f"RUDDR-{ruddr_idx:03d}",
            "display_name": ro["ruddr_name"],
            "reason": "No corresponding QB record — client onboarded in RUDDR only",
        })

    return {
        "description": "Ground truth entity mappings for cross-category resolution validation.",
        "canonical_entities": canonical,
        "unmatched": {
            "quickbooks": [],
            "ruddr": unmatched_ruddr,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Generate Nexus Finance test fixtures")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Generate matched entities
    qb_entities = [generate_qb_entity(i + 1, c) for i, c in enumerate(COMPANIES)]
    ruddr_entities = [generate_ruddr_entity(i + 1, c) for i, c in enumerate(COMPANIES)]

    # Add RUDDR-only entities
    for j, ro in enumerate(RUDDR_ONLY):
        ruddr_entities.append(generate_ruddr_entity(len(COMPANIES) + j + 1, ro, is_matched=False))

    # Generate ground truth
    truth = generate_ground_truth(qb_entities, ruddr_entities, COMPANIES)

    # Write output files
    def write_json(filename, data):
        path = args.output_dir / filename
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  Written: {path}")

    print(f"Generating test fixtures (seed={args.seed})...\n")
    write_json("qb_entities.json", qb_entities)
    write_json("ruddr_entities.json", ruddr_entities)
    write_json("canonical_ground_truth.json", truth)

    print(f"\nDone. {len(qb_entities)} QB + {len(ruddr_entities)} RUDDR entities, "
          f"{len(truth['canonical_entities'])} canonical matches.")


if __name__ == "__main__":
    main()
