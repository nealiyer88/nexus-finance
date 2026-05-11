"""Pipeline Stage 0: Entity normalization.

This module is preprocessing — not matching. It transforms raw connector
records (QuickBooks, RUDDR) into a canonical surface form (`normalized_name`)
that downstream stages (Stage 1 deterministic match, Stage 2 blocking, Stage
3 pairwise scoring, etc.) can compare reliably. The matcher cannot function
without it.

Rule order (applied EXACTLY in this sequence):
    1.  NFD unicode decomposition
    2.  Strip combining marks (diacritics)
    3.  NFC recomposition
    4.  Case fold (lowercase)
    5.  Strip leading "the" prefix
    6.  Strip honorifics / suffixes (Jr.|Sr.|III|Mr.|Ms.|Dr.)
    7.  Strip legal suffixes (LLC|L.L.C.|Inc.|Inc|Corp.|Ltd.|LLP|
        "Limited Liability Company")
    8.  Strip parenthesized qualifiers (e.g. "(Northeast)")
    9.  "&" -> "and"
    10. Strip remaining punctuation
    11. Collapse runs of whitespace
    12. Trim leading / trailing whitespace

Person-name inversion is detected BEFORE the comma is stripped: a raw name
containing exactly one comma (and matching "Last, First" shape) is rewritten
to "First Last" prior to step 1.

Source -> category map:
    quickbooks -> accounting
    ruddr      -> psa

Empty / null name input raises NormalizationError.

This module is a pure function. No I/O, no DB writes, no API calls.
No matching libraries (RapidFuzz / fastText / etc.) imported here — those
belong to Stage 1+.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class NormalizationError(ValueError):
    """Raised when an entity cannot be normalized (e.g. missing name)."""

    def __init__(self, source_id: Optional[str], reason: str) -> None:
        self.source_id = source_id
        self.reason = reason
        super().__init__(f"NormalizationError(source_id={source_id!r}): {reason}")


@dataclass
class NormalizedEntity:
    raw_name: str
    normalized_name: str
    entity_category: str  # "organization" | "person"
    source: str           # "quickbooks" | "ruddr"
    category: str         # "accounting" | "psa"
    source_id: str
    email: Optional[str]
    email_is_person: bool
    raw_record: Dict[str, Any]
    rules_applied: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module-level compiled regexes (compile-once at import time)
# ---------------------------------------------------------------------------

_SOURCE_TO_CATEGORY: Dict[str, str] = {
    "quickbooks": "accounting",
    "ruddr": "psa",
}

# Person-inversion: exactly one comma, "Last, First" shape (alphabetic-ish
# tokens on each side; allows letters, hyphens, apostrophes, periods, spaces,
# and unicode letters).
_PERSON_INVERSION_RE = re.compile(
    r"^\s*([^,]+?)\s*,\s*([^,]+?)\s*$"
)

# "the" leading prefix
_LEADING_THE_RE = re.compile(r"^the\s+", re.IGNORECASE)

# Honorifics / personal suffixes — applied as standalone tokens.
# Order matters in the alternation only insofar as longer tokens come first
# when they share a prefix; here all tokens are distinct.
_HONORIFIC_RE = re.compile(
    r"(?:^|\s)(?:jr\.?|sr\.?|iii|mr\.?|ms\.?|dr\.?)(?=\s|$|,|\.)",
    re.IGNORECASE,
)

# Legal suffixes — match as whole-token sequences. We accept optional
# preceding comma + whitespace to absorb "Cenlar, LLC".
# "Limited Liability Company" (multi-word) listed first.
_LEGAL_SUFFIX_RE = re.compile(
    r"(?:,\s*|\s+)"
    r"(?:limited\s+liability\s+company|l\.l\.c\.?|llc\.?|inc\.?|corp\.?|ltd\.?|llp\.?)"
    r"(?=\s|$|\.|,)",
    re.IGNORECASE,
)

# Parenthesized qualifier: "(...)" anywhere in the string.
_PAREN_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)")

# Standalone middle initial like " J. " or " J ".
_MIDDLE_INITIAL_RE = re.compile(r"\s+[a-z]\.?(?=\s)", re.IGNORECASE)

# Remaining punctuation to strip after structural rules ran. We deliberately
# keep hyphens (multi-word surnames like "ruiz-fernandez" preserve them).
_PUNCT_RE = re.compile(r"[.,;:!?\"'`]")

# Whitespace collapse.
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_person_inversion(name: str) -> Optional[str]:
    """Return inverted "First Last" if `name` is "Last, First" with exactly
    one comma; otherwise None."""
    if name.count(",") != 1:
        return None
    m = _PERSON_INVERSION_RE.match(name)
    if not m:
        return None
    last, first = m.group(1), m.group(2)
    # Heuristic: the first portion (last name) shouldn't contain spaces if
    # the second portion is a single given name. We allow the inversion as
    # long as both halves are non-empty alphabetic-ish.
    if not last.strip() or not first.strip():
        return None
    return f"{first} {last}"


def _strip_diacritics(s: str) -> str:
    """NFD -> drop combining marks -> NFC."""
    decomposed = unicodedata.normalize("NFD", s)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", no_marks)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_entity(raw: Dict[str, Any]) -> NormalizedEntity:
    """Normalize a raw connector record into a NormalizedEntity.

    `raw` is treated as opaque except for these top-level keys:
        - id              (source_id)
        - source          ("quickbooks" | "ruddr")
        - entity_category ("organization" | "person")
        - display_name    (primary input string)
        - email           (optional)
    The full `raw` dict is passed through unchanged in `raw_record`.
    """
    source_id = raw.get("id")
    source = raw.get("source", "")
    entity_category = raw.get("entity_category", "")
    display_name = raw.get("display_name")

    if not isinstance(display_name, str) or not display_name.strip():
        raise NormalizationError(source_id, "empty or missing display_name")

    raw_name = display_name
    rules_applied: List[str] = []

    name = raw_name

    # Step 0: person-name inversion BEFORE comma stripping / lowercasing.
    if entity_category == "person":
        inverted = _detect_person_inversion(name)
        if inverted is not None:
            name = inverted
            rules_applied.append("person_inversion")

    # Steps 1-3: NFD -> strip combining marks -> NFC.
    pre_unicode = name
    name = _strip_diacritics(name)
    if name != pre_unicode:
        rules_applied.append("strip_diacritics")

    # Step 4: case fold (lowercase).
    name = name.lower()
    rules_applied.append("lowercase")

    # Step 5: strip "the" prefix.
    new_name, n = _LEADING_THE_RE.subn("", name)
    if n:
        rules_applied.append("strip_the")
    name = new_name

    # Step 6: honorifics / personal suffixes.
    new_name, n = _HONORIFIC_RE.subn(" ", name)
    if n:
        rules_applied.append("strip_honorific")
        # repeat once in case adjacent honorifics
        new_name, _ = _HONORIFIC_RE.subn(" ", new_name)
    name = new_name

    # Person middle-initial stripping ("sarah j. martinez" -> "sarah martinez")
    if entity_category == "person":
        new_name, n = _MIDDLE_INITIAL_RE.subn(" ", name)
        if n:
            rules_applied.append("strip_middle_initial")
        name = new_name

    # Step 7: legal suffixes (organizations).
    new_name, n = _LEGAL_SUFFIX_RE.subn("", name)
    if n:
        rules_applied.append("strip_legal_suffix")
        # repeat once for stacked suffixes (e.g. "Foo LLC Inc")
        new_name, _ = _LEGAL_SUFFIX_RE.subn("", new_name)
    name = new_name

    # Step 8: parenthesized qualifiers.
    new_name, n = _PAREN_QUALIFIER_RE.subn("", name)
    if n:
        rules_applied.append("strip_paren_qualifier")
    name = new_name

    # Step 9: ampersand normalization.
    if "&" in name:
        name = name.replace("&", " and ")
        rules_applied.append("ampersand_to_and")

    # Step 10: strip remaining punctuation.
    new_name = _PUNCT_RE.sub(" ", name)
    if new_name != name:
        rules_applied.append("strip_punctuation")
    name = new_name

    # Steps 11-12: collapse whitespace + trim.
    name = _WS_RE.sub(" ", name).strip()
    rules_applied.append("collapse_whitespace")

    if not name:
        raise NormalizationError(
            source_id, "name became empty after normalization"
        )

    # Email extraction.
    email = raw.get("email")
    if email is not None and not isinstance(email, str):
        email = None
    email_is_person = entity_category == "person"

    category = _SOURCE_TO_CATEGORY.get(source, "")

    return NormalizedEntity(
        raw_name=raw_name,
        normalized_name=name,
        entity_category=entity_category,
        source=source,
        category=category,
        source_id=str(source_id) if source_id is not None else "",
        email=email,
        email_is_person=email_is_person,
        raw_record=raw,
        rules_applied=rules_applied,
    )


# Inline anchor self-tests; run `python -m core.ingestion.normalizer` to check.
_ANCHOR_CASES = [
    ("Cenlar, LLC.", "organization", "cenlar"),
    ("Chen, Michael", "person", "michael chen"),
    ("André Dubois", "person", "andre dubois"),
    ("The Briarwood Group, LLC", "organization", "briarwood group"),
    ("Pinnacle Engineering (Northeast)", "organization", "pinnacle engineering"),
    ("Beck & Howell Consulting Group", "organization", "beck and howell consulting group"),
    ("Marcus Williams Jr.", "person", "marcus williams"),
    ("Sarah J. Martinez", "person", "sarah martinez"),
    ("GreenField Analytics, LLC", "organization", "greenfield analytics"),
]

if __name__ == "__main__":  # pragma: no cover
    fails = 0
    for raw_name, cat, expected in _ANCHOR_CASES:
        out = normalize_entity({"id": "TEST", "source": "quickbooks",
                                "entity_category": cat, "display_name": raw_name})
        ok = out.normalized_name == expected
        print(f"{'OK ' if ok else 'FAIL'}  {raw_name!r:45s} -> {out.normalized_name!r}  (expected {expected!r})")
        if not ok:
            fails += 1
    print(f"\n{len(_ANCHOR_CASES) - fails}/{len(_ANCHOR_CASES)} anchors passed")
