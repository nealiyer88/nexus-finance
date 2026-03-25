"""Generate synthetic cross-category test data for Nexus Finance.

Produces QB + RUDDR fixture files with deliberate naming mismatches,
structural differences, and edge cases that exercise the entity
resolution engine.

Coverage:
  - 19 org entity pairs  (13 naming-entropy patterns)
  - 25 person entity pairs (8 fragmentation patterns)
  - 3 shared last names  (Chen x3, Johnson x2, Williams x2)
  - 6 departments, billing rates $75-$310/hr
  - 2 QB-only + 1 RUDDR-only negatives
  - 44 canonical ground truth entries

Usage:
    python scripts/generate_test_data.py [--output-dir tests/fixtures] [--seed 42]
"""

import argparse
import json
import random
import string
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# ---------------------------------------------------------------------------
# Departments & billing rates
# ---------------------------------------------------------------------------

DEPARTMENTS = {
    "strategy-advisory":     {"label": "Strategy & Advisory",      "rate_min": 285, "rate_max": 310},
    "data-engineering":      {"label": "Data Engineering",         "rate_min": 200, "rate_max": 245},
    "software-development":  {"label": "Software Development",     "rate_min": 175, "rate_max": 220},
    "cloud-infrastructure":  {"label": "Cloud & Infrastructure",   "rate_min": 190, "rate_max": 230},
    "design-ux":             {"label": "Design & UX",              "rate_min": 150, "rate_max": 185},
    "staff-augmentation":    {"label": "Staff Augmentation",       "rate_min":  75, "rate_max": 120},
}

# ---------------------------------------------------------------------------
# 19 Org entity pairs — each tagged with the naming-entropy pattern it covers
# ---------------------------------------------------------------------------

ORG_PAIRS = [
    # Pattern 1: legal-suffix-strip  (LLC/Inc/Corp in QB, dropped in RUDDR)
    {
        "canonical": "Cenlar FSB",
        "qb_name": "Cenlar, LLC.",
        "ruddr_slug": "cenlar-fsb",
        "ruddr_name": "Cenlar FSB",
        "pattern": "legal-suffix-strip",
        "industry": "financial-services",
        "type": "client",
        "city": "Ewing", "state": "NJ",
        "department": "data-engineering",
    },
    # Pattern 2: abbreviation  (Group → Grp, Capital → Cap in slug)
    {
        "canonical": "Meridian Capital Group",
        "qb_name": "Meridian Capital Group LLC",
        "ruddr_slug": "meridian-cap",
        "ruddr_name": "Meridian Capital",
        "pattern": "abbreviation",
        "industry": "financial-services",
        "type": "client",
        "city": "New York", "state": "NY",
        "department": "strategy-advisory",
    },
    # Pattern 3: case-variation  (mixed case vs lowercase slug, CamelCase display)
    {
        "canonical": "TechVentures",
        "qb_name": "TechVentures Inc",
        "ruddr_slug": "techventures-io",
        "ruddr_name": "TechVentures",
        "pattern": "case-variation",
        "industry": "technology",
        "type": "client",
        "city": "San Francisco", "state": "CA",
        "department": "software-development",
    },
    # Pattern 4: punctuation-diff  (Co. with period in QB, clean in RUDDR)
    {
        "canonical": "Apex Logistics",
        "qb_name": "Apex Logistics Co.",
        "ruddr_slug": "apex-log",
        "ruddr_name": "Apex Logistics",
        "pattern": "punctuation-diff",
        "industry": "logistics",
        "type": "client",
        "city": "Chicago", "state": "IL",
        "department": "cloud-infrastructure",
    },
    # Pattern 5: word-order  (Capital before Partners in QB, reversed in RUDDR)
    {
        "canonical": "Summit Partners",
        "qb_name": "Summit Partners Capital",
        "ruddr_slug": "summit-partners",
        "ruddr_name": "Summit Partners",
        "pattern": "word-order",
        "industry": "financial-services",
        "type": "client",
        "city": "Boston", "state": "MA",
        "department": "strategy-advisory",
    },
    # Pattern 6: dba-trade-name  (legal name in QB, trade/DBA in RUDDR)
    {
        "canonical": "Greenfield Analytics",
        "qb_name": "GreenField Analytics, LLC",
        "ruddr_slug": "greenfield-analytics",
        "ruddr_name": "Greenfield Analytics",
        "pattern": "dba-trade-name",
        "industry": "analytics",
        "type": "client",
        "city": "Austin", "state": "TX",
        "department": "data-engineering",
    },
    # Pattern 7: truncation  (full name in QB, truncated slug in RUDDR)
    {
        "canonical": "Northstar Health Systems",
        "qb_name": "Northstar Health Systems",
        "ruddr_slug": "northstar-health",
        "ruddr_name": "NorthStar Health",
        "pattern": "truncation",
        "industry": "healthcare",
        "type": "client",
        "city": "Boston", "state": "MA",
        "department": "software-development",
    },
    # Pattern 8: spacing-compound  (DataBridge one word in QB, two words in RUDDR display)
    {
        "canonical": "DataBridge Solutions",
        "qb_name": "DataBridge Solutions",
        "ruddr_slug": "databridge-sol",
        "ruddr_name": "DataBridge",
        "pattern": "spacing-compound",
        "industry": "technology",
        "type": "vendor",
        "city": "Reston", "state": "VA",
        "department": "software-development",
    },
    # Pattern 9: parent-subsidiary  (parent name in QB, subsidiary brand in RUDDR)
    {
        "canonical": "Vanguard Digital Labs",
        "qb_name": "Vanguard Holdings Inc.",
        "ruddr_slug": "vanguard-digital",
        "ruddr_name": "Vanguard Digital Labs",
        "pattern": "parent-subsidiary",
        "industry": "technology",
        "type": "client",
        "city": "Seattle", "state": "WA",
        "department": "cloud-infrastructure",
    },
    # Pattern 10: ampersand-and  (& in QB, "and" in RUDDR)
    {
        "canonical": "Beck & Howell Consulting",
        "qb_name": "Beck & Howell Consulting Group",
        "ruddr_slug": "beck-howell",
        "ruddr_name": "Beck and Howell Consulting",
        "pattern": "ampersand-and",
        "industry": "consulting",
        "type": "client",
        "city": "Philadelphia", "state": "PA",
        "department": "strategy-advisory",
    },
    # Pattern 11: the-prefix-drop  ("The" in QB, dropped in RUDDR)
    {
        "canonical": "Briarwood Group",
        "qb_name": "The Briarwood Group, LLC",
        "ruddr_slug": "briarwood-group",
        "ruddr_name": "Briarwood Group",
        "pattern": "the-prefix-drop",
        "industry": "real-estate",
        "type": "client",
        "city": "Atlanta", "state": "GA",
        "department": "design-ux",
    },
    # Pattern 12: regional-qualifier-drop  (regional qualifier in QB, dropped in RUDDR)
    {
        "canonical": "Pinnacle Engineering",
        "qb_name": "Pinnacle Engineering (Northeast)",
        "ruddr_slug": "pinnacle-eng",
        "ruddr_name": "Pinnacle Engineering",
        "pattern": "regional-qualifier-drop",
        "industry": "engineering",
        "type": "client",
        "city": "Hartford", "state": "CT",
        "department": "cloud-infrastructure",
    },
    # Pattern 13: rebrand-alias  (old name in QB, new brand in RUDDR)
    {
        "canonical": "Luminos AI",
        "qb_name": "BrightPath Machine Learning Corp",
        "ruddr_slug": "luminos-ai",
        "ruddr_name": "Luminos AI",
        "pattern": "rebrand-alias",
        "industry": "artificial-intelligence",
        "type": "client",
        "city": "Palo Alto", "state": "CA",
        "department": "data-engineering",
    },
    # --- Additional orgs to reach 19, reusing patterns for depth ---
    # Pattern 1 again: legal-suffix-strip
    {
        "canonical": "Hargrove Financial",
        "qb_name": "Hargrove Financial Services, Inc.",
        "ruddr_slug": "hargrove-fin",
        "ruddr_name": "Hargrove Financial",
        "pattern": "legal-suffix-strip",
        "industry": "financial-services",
        "type": "client",
        "city": "Charlotte", "state": "NC",
        "department": "strategy-advisory",
    },
    # Pattern 2 again: abbreviation
    {
        "canonical": "Pacific Rim Technologies",
        "qb_name": "Pacific Rim Technologies International",
        "ruddr_slug": "pacrim-tech",
        "ruddr_name": "PacRim Tech",
        "pattern": "abbreviation",
        "industry": "technology",
        "type": "client",
        "city": "Portland", "state": "OR",
        "department": "software-development",
    },
    # Pattern 4 again: punctuation-diff  (ampersand + comma mess)
    {
        "canonical": "Calloway Reed Partners",
        "qb_name": "Calloway, Reed & Partners LLC",
        "ruddr_slug": "calloway-reed",
        "ruddr_name": "Calloway Reed Partners",
        "pattern": "punctuation-diff",
        "industry": "consulting",
        "type": "client",
        "city": "Denver", "state": "CO",
        "department": "staff-augmentation",
    },
    # Pattern 6 again: dba-trade-name  (legal vs operating name)
    {
        "canonical": "Ironclad Security",
        "qb_name": "Ironclad Cybersecurity Solutions LLC",
        "ruddr_slug": "ironclad-sec",
        "ruddr_name": "Ironclad Security",
        "pattern": "dba-trade-name",
        "industry": "cybersecurity",
        "type": "vendor",
        "city": "Arlington", "state": "VA",
        "department": "cloud-infrastructure",
    },
    # Pattern 9 again: parent-subsidiary
    {
        "canonical": "Atlas Media Group",
        "qb_name": "Atlas Communications Corp.",
        "ruddr_slug": "atlas-media",
        "ruddr_name": "Atlas Media Group",
        "pattern": "parent-subsidiary",
        "industry": "media",
        "type": "client",
        "city": "Los Angeles", "state": "CA",
        "department": "design-ux",
    },
    # Pattern 13 again: rebrand-alias
    {
        "canonical": "Stratos Cloud",
        "qb_name": "CloudNine Infrastructure Ltd.",
        "ruddr_slug": "stratos-cloud",
        "ruddr_name": "Stratos Cloud",
        "pattern": "rebrand-alias",
        "industry": "cloud-services",
        "type": "vendor",
        "city": "Dallas", "state": "TX",
        "department": "cloud-infrastructure",
    },
]

# ---------------------------------------------------------------------------
# 25 Person entity pairs — tagged with fragmentation pattern
# Shared last names: Chen x3, Johnson x2, Williams x2
# ---------------------------------------------------------------------------

PERSON_PAIRS = [
    # Pattern 1: nickname  (formal first → informal nick)
    {"canonical": "Robert Chen", "qb_name": "Robert Chen", "ruddr_name": "Bob Chen",
     "pattern": "nickname", "department": "strategy-advisory",
     "qb_email": "robert.chen@clientcorp.com", "ruddr_email": "bob.chen@clientcorp.com"},

    # Pattern 2: middle-initial  (middle initial in QB, absent in RUDDR)
    {"canonical": "Sarah J. Martinez", "qb_name": "Sarah J. Martinez", "ruddr_name": "Sarah Martinez",
     "pattern": "middle-initial", "department": "data-engineering",
     "qb_email": "smartinez@clientcorp.com", "ruddr_email": "sarah.martinez@clientcorp.com"},

    # Pattern 3: suffix-inconsistent  (Jr. in QB, dropped in RUDDR)
    {"canonical": "Marcus Williams", "qb_name": "Marcus Williams Jr.", "ruddr_name": "Marcus Williams",
     "pattern": "suffix-inconsistent", "department": "software-development",
     "qb_email": "mwilliams@clientcorp.com", "ruddr_email": "marcus.williams@clientcorp.com"},

    # Pattern 4: maiden-married  (maiden name in QB, married name in RUDDR)
    {"canonical": "Jennifer Park-Nakamura", "qb_name": "Jennifer Park", "ruddr_name": "Jennifer Nakamura",
     "pattern": "maiden-married", "department": "design-ux",
     "qb_email": "jpark@clientcorp.com", "ruddr_email": "jnakamura@clientcorp.com"},

    # Pattern 5: first-initial  (full name in QB, initial only in RUDDR)
    {"canonical": "David Thompson", "qb_name": "David Thompson", "ruddr_name": "D. Thompson",
     "pattern": "first-initial", "department": "cloud-infrastructure",
     "qb_email": "david.thompson@clientcorp.com", "ruddr_email": "dthompson@clientcorp.com"},

    # Pattern 6: hyphenated-partial  (hyphenated in QB, one part in RUDDR)
    {"canonical": "Lisa Hernandez-Garcia", "qb_name": "Lisa Hernandez-Garcia", "ruddr_name": "Lisa Garcia",
     "pattern": "hyphenated-partial", "department": "data-engineering",
     "qb_email": "lhernandezgarcia@clientcorp.com", "ruddr_email": "lisa.garcia@clientcorp.com"},

    # Pattern 7: name-order  (Last, First in QB; First Last in RUDDR)
    {"canonical": "Michael Chen", "qb_name": "Chen, Michael", "ruddr_name": "Michael Chen",
     "pattern": "name-order", "department": "software-development",
     "qb_email": "mchen@clientcorp.com", "ruddr_email": "michael.chen@clientcorp.com"},

    # Pattern 8: accent-drop  (accented in QB, ASCII in RUDDR)
    {"canonical": "André Dubois", "qb_name": "André Dubois", "ruddr_name": "Andre Dubois",
     "pattern": "accent-drop", "department": "strategy-advisory",
     "qb_email": "adubois@clientcorp.com", "ruddr_email": "andre.dubois@clientcorp.com"},

    # --- Additional persons to reach 25, with shared-last-name collisions ---

    # Chen #3 — Pattern 1 nickname variant
    {"canonical": "Wei Chen", "qb_name": "Wei Chen", "ruddr_name": "William Chen",
     "pattern": "nickname", "department": "cloud-infrastructure",
     "qb_email": "wei.chen@clientcorp.com", "ruddr_email": "william.chen@clientcorp.com"},

    # Johnson #1 — Pattern 2: middle-initial
    {"canonical": "Tyler R. Johnson", "qb_name": "Tyler R. Johnson", "ruddr_name": "Tyler Johnson",
     "pattern": "middle-initial", "department": "staff-augmentation",
     "qb_email": "tjohnson@clientcorp.com", "ruddr_email": "tyler.johnson@clientcorp.com"},

    # Johnson #2 — Pattern 5: first-initial
    {"canonical": "Amanda Johnson", "qb_name": "Amanda Johnson", "ruddr_name": "A. Johnson",
     "pattern": "first-initial", "department": "design-ux",
     "qb_email": "ajohnson@clientcorp.com", "ruddr_email": "a.johnson@clientcorp.com"},

    # Williams #2 — Pattern 7: name-order
    {"canonical": "Denise Williams", "qb_name": "Williams, Denise", "ruddr_name": "Denise Williams",
     "pattern": "name-order", "department": "data-engineering",
     "qb_email": "dwilliams@clientcorp.com", "ruddr_email": "denise.williams@clientcorp.com"},

    # Pattern 3: suffix-inconsistent  (III)
    {"canonical": "James Whitfield III", "qb_name": "James Whitfield III", "ruddr_name": "James Whitfield",
     "pattern": "suffix-inconsistent", "department": "strategy-advisory",
     "qb_email": "jwhitfield@clientcorp.com", "ruddr_email": "james.whitfield@clientcorp.com"},

    # Pattern 4: maiden-married
    {"canonical": "Rachel Kim-Okonkwo", "qb_name": "Rachel Kim", "ruddr_name": "Rachel Okonkwo",
     "pattern": "maiden-married", "department": "software-development",
     "qb_email": "rkim@clientcorp.com", "ruddr_email": "rokonkwo@clientcorp.com"},

    # Pattern 6: hyphenated-partial
    {"canonical": "Carlos Ruiz-Fernandez", "qb_name": "Carlos Ruiz-Fernandez", "ruddr_name": "Carlos Fernandez",
     "pattern": "hyphenated-partial", "department": "cloud-infrastructure",
     "qb_email": "cruiz@clientcorp.com", "ruddr_email": "carlos.fernandez@clientcorp.com"},

    # Pattern 8: accent-drop
    {"canonical": "José Álvarez", "qb_name": "José Álvarez", "ruddr_name": "Jose Alvarez",
     "pattern": "accent-drop", "department": "staff-augmentation",
     "qb_email": "jalvarez@clientcorp.com", "ruddr_email": "jose.alvarez@clientcorp.com"},

    # Pattern 1: nickname
    {"canonical": "Katherine Ellis", "qb_name": "Katherine Ellis", "ruddr_name": "Kate Ellis",
     "pattern": "nickname", "department": "design-ux",
     "qb_email": "kellis@clientcorp.com", "ruddr_email": "kate.ellis@clientcorp.com"},

    # Pattern 2: middle-initial
    {"canonical": "Brian M. Patel", "qb_name": "Brian M. Patel", "ruddr_name": "Brian Patel",
     "pattern": "middle-initial", "department": "data-engineering",
     "qb_email": "bpatel@clientcorp.com", "ruddr_email": "brian.patel@clientcorp.com"},

    # Pattern 5: first-initial
    {"canonical": "Samantha Cruz", "qb_name": "Samantha Cruz", "ruddr_name": "S. Cruz",
     "pattern": "first-initial", "department": "software-development",
     "qb_email": "scruz@clientcorp.com", "ruddr_email": "s.cruz@clientcorp.com"},

    # Pattern 7: name-order
    {"canonical": "Nathan Brooks", "qb_name": "Brooks, Nathan", "ruddr_name": "Nathan Brooks",
     "pattern": "name-order", "department": "cloud-infrastructure",
     "qb_email": "nbrooks@clientcorp.com", "ruddr_email": "nathan.brooks@clientcorp.com"},

    # Pattern 1: nickname
    {"canonical": "William Torres", "qb_name": "William Torres", "ruddr_name": "Will Torres",
     "pattern": "nickname", "department": "staff-augmentation",
     "qb_email": "wtorres@clientcorp.com", "ruddr_email": "will.torres@clientcorp.com"},

    # Pattern 3: suffix-inconsistent (Sr.)
    {"canonical": "Franklin Moore Sr.", "qb_name": "Franklin Moore Sr.", "ruddr_name": "Franklin Moore",
     "pattern": "suffix-inconsistent", "department": "strategy-advisory",
     "qb_email": "fmoore@clientcorp.com", "ruddr_email": "franklin.moore@clientcorp.com"},

    # Pattern 4: maiden-married
    {"canonical": "Emily Sato-Rivera", "qb_name": "Emily Sato", "ruddr_name": "Emily Rivera",
     "pattern": "maiden-married", "department": "design-ux",
     "qb_email": "esato@clientcorp.com", "ruddr_email": "erivera@clientcorp.com"},

    # Pattern 6: hyphenated-partial
    {"canonical": "Priya Sharma-Gupta", "qb_name": "Priya Sharma-Gupta", "ruddr_name": "Priya Gupta",
     "pattern": "hyphenated-partial", "department": "data-engineering",
     "qb_email": "psharma@clientcorp.com", "ruddr_email": "priya.gupta@clientcorp.com"},

    # Pattern 8: accent-drop
    {"canonical": "François Lefèvre", "qb_name": "François Lefèvre", "ruddr_name": "Francois Lefevre",
     "pattern": "accent-drop", "department": "software-development",
     "qb_email": "flefevre@clientcorp.com", "ruddr_email": "francois.lefevre@clientcorp.com"},
]

# ---------------------------------------------------------------------------
# Negative examples (no match in other system)
# ---------------------------------------------------------------------------

QB_ONLY = [
    {
        "qb_name": "Thornton & Marsh LLP",
        "type": "vendor",
        "industry": "legal",
        "city": "Washington", "state": "DC",
        "reason": "Outside counsel — not tracked in RUDDR",
    },
    {
        "qb_name": "Regency Office Supplies",
        "type": "vendor",
        "industry": "office-supplies",
        "city": "Edison", "state": "NJ",
        "reason": "Office vendor — no project work, QB-only",
    },
]

RUDDR_ONLY = [
    {
        "ruddr_slug": "riverstone-cap",
        "ruddr_name": "Riverstone Capital Advisors",
        "industry": "financial-services",
        "type": "client",
        "department": "strategy-advisory",
        "reason": "No corresponding QB record — client onboarded in RUDDR only",
    },
]

# ---------------------------------------------------------------------------
# Project name pool
# ---------------------------------------------------------------------------

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
    "Cloud Cost Optimization",
    "Zero Trust Implementation",
    "Mobile App Rewrite",
    "Predictive Analytics MVP",
    "API Gateway Modernization",
    "Data Warehouse Migration",
    "Customer 360 Platform",
]

# ---------------------------------------------------------------------------
# Match-signal definitions per pattern
# ---------------------------------------------------------------------------

ORG_MATCH_SIGNALS = {
    "legal-suffix-strip":       ["name_fuzzy", "name_prefix_match", "suffix_strip"],
    "abbreviation":             ["name_fuzzy", "name_prefix_match", "abbreviation_expansion"],
    "case-variation":           ["name_fuzzy", "name_prefix_match", "case_normalize"],
    "punctuation-diff":         ["name_fuzzy", "name_prefix_match", "punctuation_strip"],
    "word-order":               ["name_fuzzy", "name_prefix_match", "token_sort"],
    "dba-trade-name":           ["name_fuzzy", "name_prefix_match", "dba_lookup"],
    "truncation":               ["name_fuzzy", "name_prefix_match", "slug_prefix"],
    "spacing-compound":         ["name_fuzzy", "name_prefix_match", "compound_split"],
    "parent-subsidiary":        ["name_fuzzy", "corporate_hierarchy"],
    "ampersand-and":            ["name_fuzzy", "name_prefix_match", "symbol_normalize"],
    "the-prefix-drop":          ["name_fuzzy", "name_prefix_match", "prefix_strip"],
    "regional-qualifier-drop":  ["name_fuzzy", "name_prefix_match", "qualifier_strip"],
    "rebrand-alias":            ["alias_table", "manual_override"],
}

PERSON_MATCH_SIGNALS = {
    "nickname":              ["name_fuzzy", "nickname_table"],
    "middle-initial":        ["name_fuzzy", "initial_expansion"],
    "suffix-inconsistent":   ["name_fuzzy", "suffix_strip"],
    "maiden-married":        ["email_domain_match", "manual_override"],
    "first-initial":         ["name_fuzzy", "initial_expansion"],
    "hyphenated-partial":    ["name_fuzzy", "hyphen_split"],
    "name-order":            ["name_fuzzy", "token_sort"],
    "accent-drop":           ["name_fuzzy", "unicode_normalize"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def random_phone():
    area = random.randint(200, 999)
    return f"{area}-555-{random.randint(1000, 9999)}"


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


def dept_rate(department_key: str) -> float:
    dept = DEPARTMENTS[department_key]
    return round(random.uniform(dept["rate_min"], dept["rate_max"]), 2)


def make_project_code(slug: str, sow_num: int) -> str:
    prefix = slug.split("-")[0].upper()[:3]
    suffix = "".join(random.choices(string.ascii_uppercase, k=random.randint(2, 4)))
    return f"{prefix}-{suffix}-SOW{sow_num}"


# ---------------------------------------------------------------------------
# Entity generators — Orgs
# ---------------------------------------------------------------------------

def generate_qb_org(idx: int, org: dict) -> dict:
    slug = org.get("ruddr_slug", org["canonical"].lower().replace(" ", "-"))
    return {
        "id": f"QB-{idx:03d}",
        "source": "quickbooks",
        "entity_category": "organization",
        "display_name": org["qb_name"],
        "type": "Vendor" if org["type"] == "vendor" else "Customer",
        "email": random_email(slug),
        "phone": random_phone(),
        "balance": round(random.uniform(5000, 120000) * (-1 if org["type"] == "vendor" else 1), 2),
        "currency": "USD",
        "address": {
            "line1": f"{random.randint(1, 2000)} {random.choice(['Main', 'Market', 'Congress', 'Phillips', 'Adams', 'Berkeley', 'Francis', 'Elm', 'Oak', 'Walnut'])} St",
            "city": org["city"],
            "state": org["state"],
            "postal_code": f"{random.randint(10000, 99999)}",
        },
        "created_at": random_date(2024, 2024),
        "last_modified": random_date(2025, 2025),
    }


def generate_ruddr_org(idx: int, org: dict) -> dict:
    dept_key = org.get("department", random.choice(list(DEPARTMENTS.keys())))
    project_count = random.randint(1, 3)
    slug = org.get("ruddr_slug", org.get("canonical", "unknown").lower().replace(" ", "-"))
    projects = []
    for j in range(project_count):
        budget = random.choice([300, 400, 500, 600, 750, 800, 900, 1000, 1200, 2000])
        logged = round(random.uniform(50, budget * 0.95), 1)
        projects.append({
            "code": make_project_code(slug, j + 1),
            "name": random.choice(PROJECT_NAMES),
            "status": random.choice(["active", "active", "completed"]),
            "budget_hours": budget,
            "logged_hours": logged,
            "hourly_rate": dept_rate(dept_key),
            "department": DEPARTMENTS[dept_key]["label"],
        })

    return {
        "id": f"RUDDR-{idx:03d}",
        "source": "ruddr",
        "entity_category": "organization",
        "slug": slug,
        "display_name": org.get("ruddr_name", org.get("canonical", "")),
        "type": org["type"],
        "projects": projects,
        "tags": [org.get("industry", "general"), random.choice(["enterprise", "mid-market", "startup", "boutique"])],
        "created_at": random_date(2024, 2024),
        "last_activity": random_date(2025, 2025),
    }


# ---------------------------------------------------------------------------
# Entity generators — Persons
# ---------------------------------------------------------------------------

def generate_qb_person(idx: int, person: dict) -> dict:
    dept_key = person["department"]
    return {
        "id": f"QB-{idx:03d}",
        "source": "quickbooks",
        "entity_category": "person",
        "display_name": person["qb_name"],
        "type": "Employee",
        "email": person["qb_email"],
        "phone": random_phone(),
        "department": DEPARTMENTS[dept_key]["label"],
        "hourly_rate": dept_rate(dept_key),
        "currency": "USD",
        "created_at": random_date(2024, 2024),
        "last_modified": random_date(2025, 2025),
    }


def generate_ruddr_person(idx: int, person: dict) -> dict:
    dept_key = person["department"]
    return {
        "id": f"RUDDR-{idx:03d}",
        "source": "ruddr",
        "entity_category": "person",
        "slug": person["ruddr_name"].lower().replace(" ", "-").replace(".", ""),
        "display_name": person["ruddr_name"],
        "type": "team-member",
        "email": person["ruddr_email"],
        "department": DEPARTMENTS[dept_key]["label"],
        "hourly_rate": dept_rate(dept_key),
        "created_at": random_date(2024, 2024),
        "last_activity": random_date(2025, 2025),
    }


# ---------------------------------------------------------------------------
# QB-only negative generators
# ---------------------------------------------------------------------------

def generate_qb_only(idx: int, neg: dict) -> dict:
    return {
        "id": f"QB-{idx:03d}",
        "source": "quickbooks",
        "entity_category": "organization",
        "display_name": neg["qb_name"],
        "type": "Vendor",
        "email": random_email(neg["qb_name"].lower().split()[0]),
        "phone": random_phone(),
        "balance": round(random.uniform(-50000, -1000), 2),
        "currency": "USD",
        "address": {
            "line1": f"{random.randint(1, 2000)} {random.choice(['Main', 'Market', 'Broad'])} St",
            "city": neg["city"],
            "state": neg["state"],
            "postal_code": f"{random.randint(10000, 99999)}",
        },
        "created_at": random_date(2024, 2024),
        "last_modified": random_date(2025, 2025),
    }


# ---------------------------------------------------------------------------
# Ground truth generator
# ---------------------------------------------------------------------------

def generate_ground_truth(qb_entities, ruddr_entities, org_pairs, person_pairs,
                          qb_only_entities, ruddr_only_entities):
    canonical = []
    idx = 0

    # Org matches
    for i, org in enumerate(org_pairs):
        qb_e = qb_entities[i]
        ruddr_e = ruddr_entities[i]
        signals = ORG_MATCH_SIGNALS.get(org["pattern"], ["name_fuzzy"])
        canonical.append({
            "canonical_id": f"CAN-{idx + 1:03d}",
            "canonical_name": org["canonical"],
            "entity_type": org["type"],
            "entity_category": "organization",
            "pattern": org["pattern"],
            "sources": {
                "quickbooks": {"id": qb_e["id"], "display_name": qb_e["display_name"]},
                "ruddr": {"id": ruddr_e["id"], "slug": ruddr_e["slug"], "display_name": ruddr_e["display_name"]},
            },
            "match_signals": signals,
            "confidence": round(random.uniform(0.82, 0.99), 2),
        })
        idx += 1

    # Person matches
    org_count = len(org_pairs)
    for i, person in enumerate(person_pairs):
        qb_e = qb_entities[org_count + i]
        ruddr_e = ruddr_entities[org_count + i]
        signals = PERSON_MATCH_SIGNALS.get(person["pattern"], ["name_fuzzy"])
        canonical.append({
            "canonical_id": f"CAN-{idx + 1:03d}",
            "canonical_name": person["canonical"],
            "entity_type": "employee",
            "entity_category": "person",
            "pattern": person["pattern"],
            "sources": {
                "quickbooks": {"id": qb_e["id"], "display_name": qb_e["display_name"]},
                "ruddr": {"id": ruddr_e["id"], "slug": ruddr_e["slug"], "display_name": ruddr_e["display_name"]},
            },
            "match_signals": signals,
            "confidence": round(random.uniform(0.78, 0.99), 2),
        })
        idx += 1

    # Unmatched
    unmatched_qb = []
    for qb_neg in qb_only_entities:
        unmatched_qb.append({
            "id": qb_neg["id"],
            "display_name": qb_neg["display_name"],
            "reason": next((n["reason"] for n in QB_ONLY if n["qb_name"] == qb_neg["display_name"]),
                           "No RUDDR match"),
        })

    unmatched_ruddr = []
    for ruddr_neg in ruddr_only_entities:
        unmatched_ruddr.append({
            "id": ruddr_neg["id"],
            "display_name": ruddr_neg["display_name"],
            "reason": next((n["reason"] for n in RUDDR_ONLY if n["ruddr_name"] == ruddr_neg["display_name"]),
                           "No QB match"),
        })

    return {
        "description": "Ground truth entity mappings for cross-category resolution validation.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "total_canonical": len(canonical),
            "org_pairs": len(org_pairs),
            "person_pairs": len(person_pairs),
            "unmatched_qb": len(unmatched_qb),
            "unmatched_ruddr": len(unmatched_ruddr),
            "departments": len(DEPARTMENTS),
            "org_patterns_covered": len(set(o["pattern"] for o in org_pairs)),
            "person_patterns_covered": len(set(p["pattern"] for p in person_pairs)),
        },
        "departments": {k: v["label"] for k, v in DEPARTMENTS.items()},
        "canonical_entities": canonical,
        "unmatched": {
            "quickbooks": unmatched_qb,
            "ruddr": unmatched_ruddr,
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate Nexus Finance test fixtures")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # --- Build matched entity lists (orgs then persons, same order both sides) ---
    qb_entities = []
    ruddr_entities = []

    # Orgs (QB idx 1..19, RUDDR idx 1..19)
    for i, org in enumerate(ORG_PAIRS):
        qb_entities.append(generate_qb_org(i + 1, org))
        ruddr_entities.append(generate_ruddr_org(i + 1, org))

    # Persons (QB idx 20..44, RUDDR idx 20..44)
    person_offset = len(ORG_PAIRS)
    for i, person in enumerate(PERSON_PAIRS):
        qb_entities.append(generate_qb_person(person_offset + i + 1, person))
        ruddr_entities.append(generate_ruddr_person(person_offset + i + 1, person))

    # --- Negatives ---
    neg_offset = len(ORG_PAIRS) + len(PERSON_PAIRS)

    qb_only_entities = []
    for j, neg in enumerate(QB_ONLY):
        ent = generate_qb_only(neg_offset + j + 1, neg)
        qb_entities.append(ent)
        qb_only_entities.append(ent)

    ruddr_only_entities = []
    for j, ro in enumerate(RUDDR_ONLY):
        ent = generate_ruddr_org(neg_offset + len(QB_ONLY) + j + 1, ro)
        ruddr_entities.append(ent)
        ruddr_only_entities.append(ent)

    # --- Ground truth ---
    truth = generate_ground_truth(qb_entities, ruddr_entities, ORG_PAIRS, PERSON_PAIRS,
                                  qb_only_entities, ruddr_only_entities)

    # --- Write ---
    def write_json(filename, data):
        path = args.output_dir / filename
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  Written: {path}")

    print(f"Generating test fixtures (seed={args.seed})...\n")
    write_json("qb_entities.json", qb_entities)
    write_json("ruddr_entities.json", ruddr_entities)
    write_json("canonical_ground_truth.json", truth)

    print(f"\n  {len(qb_entities)} QB entities ({len(ORG_PAIRS)} orgs + {len(PERSON_PAIRS)} persons + {len(QB_ONLY)} QB-only)")
    print(f"  {len(ruddr_entities)} RUDDR entities ({len(ORG_PAIRS)} orgs + {len(PERSON_PAIRS)} persons + {len(RUDDR_ONLY)} RUDDR-only)")
    print(f"  {len(truth['canonical_entities'])} canonical matches")
    print(f"  {len(DEPARTMENTS)} departments, rates ${min(d['rate_min'] for d in DEPARTMENTS.values())}-${max(d['rate_max'] for d in DEPARTMENTS.values())}/hr")
    print(f"  Org patterns covered: {truth['stats']['org_patterns_covered']}/13")
    print(f"  Person patterns covered: {truth['stats']['person_patterns_covered']}/8")
    print("\nDone.")


if __name__ == "__main__":
    main()
