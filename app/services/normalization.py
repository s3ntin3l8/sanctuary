"""Text normalization helpers.

`normalize_hm`           — canonicalizes H&M variants in display text.
`normalize_entity_name`  — canonical form for Entity dedup at extraction time.
"""

import re
import unicodedata
from typing import TYPE_CHECKING

from app.models.enums import EntityType

if TYPE_CHECKING:
    from app.models.database import Case

_HM_PATTERN = re.compile(r"\bh\s*&\s*m\b|\bh\s+and\s+m\b", re.IGNORECASE)


def normalize_hm(text: str) -> str:
    """Canonicalize 'H&M' / 'h and m' / 'H & M' → 'H&M'."""
    return _HM_PATTERN.sub("H&M", text)


# Honorifics stripped from PERSON names only. Order-dependent — longer first.
_PERSON_HONORIFICS = (
    "prof. dr.",
    "rechtsanwältin",
    "rechtsanwalt",
    "prof.",
    "dr.",
    "herr",
    "frau",
    "ra ",
    "rain ",
)

# German transliteration fold applied AFTER diacritic strip so
# "Björn"/"Bjoern", "Müller"/"Mueller", "Schröder"/"Schroeder",
# "Köln"/"Koeln" all collapse to the same canonical form.
_GERMAN_FOLD = (
    ("oe", "o"),
    ("ue", "u"),
    ("ae", "a"),
)


def _strip_diacritics(s: str) -> str:
    """ö→o, ü→u, ä→a, ß→ss, é→e, etc."""
    s = s.replace("ß", "ss")
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _german_fold(s: str) -> str:
    """Fold ASCII-form German umlaut digraphs to single vowels."""
    for src, dst in _GERMAN_FOLD:
        s = s.replace(src, dst)
    return s


def _strip_person_honorifics(s: str) -> str:
    lower = s.lower().strip()
    for h in _PERSON_HONORIFICS:
        if lower.startswith(h):
            return s[len(h) :].strip()
    return s


def _comma_reverse(s: str) -> str:
    """'Liu, Yingying' → 'Yingying Liu' when the prefix is a single token."""
    if "," not in s:
        return s
    parts = [p.strip() for p in s.split(",", 1)]
    if len(parts) == 2 and parts[0] and " " not in parts[0]:
        return f"{parts[1]} {parts[0]}"
    return s


def _sub_unit_collapse(s: str, canonical_names: set[str]) -> str:
    """'Landratsamt X, Amt Y' → 'Landratsamt X' when normalized 'Landratsamt X'
    is present in the (already-normalized) canonical_names set.
    """
    if ", " not in s:
        return s
    prefix = s.split(", ", 1)[0]
    prefix_key = _german_fold(_strip_diacritics(prefix).lower())
    if prefix_key in canonical_names:
        return prefix
    return s


def normalize_entity_name(
    name: str,
    entity_type: EntityType,
    canonical_names: set[str] | None = None,
) -> str:
    """Return the canonical dedup key for an entity name.

    The original `name` should still be stored as-is on the Entity row; this
    function only produces the lookup key. Conservative — no Levenshtein or
    other fuzzy matching, only deterministic German-transliteration folds.
    """
    if not name or not name.strip():
        return ""

    s = name.strip()

    if entity_type == EntityType.PERSON:
        s = _strip_person_honorifics(s)
        s = _comma_reverse(s)
    elif entity_type in (
        EntityType.ORGANIZATION,
        EntityType.COURT,
        EntityType.LAW_FIRM,
    ):
        if canonical_names:
            s = _sub_unit_collapse(s, canonical_names)

    s = _strip_diacritics(s)
    s = s.lower()
    s = _german_fold(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_party_canonical_map(case: Case | None) -> dict[str, str]:
    """Snap-to-canonical lookup for known case parties.

    Returns a dict mapping `normalize_entity_name(variant, PERSON|ORG)` to the
    canonical party name in `Case.parties`. The entity-extractor often emits
    PERSON variants like "Liu", "Yingying Liu", "J. Liu, Yingying" that share
    a dedup key with "Liu Yingying" once normalized — but some variants don't
    quite normalize identically (East-Asian vs Western name ordering doesn't
    collapse, initials, comma-reverse edge cases). Anchoring on the case's
    `parties` list pins variants of both orderings to one row.
    """
    if not case or not case.parties:
        return {}
    mapping: dict[str, str] = {}
    for party in case.parties:
        if not isinstance(party, dict):
            continue
        canonical = (party.get("name") or "").strip()
        if not canonical:
            continue
        # Map all entity-type normalisations so humans ("Liu Yingying") and
        # orgs ("Haidl Funk Rechtsanwälte") and courts ("Amtsgericht
        # Ingolstadt") all snap uniformly.
        for et in (
            EntityType.PERSON,
            EntityType.ORGANIZATION,
            EntityType.LAW_FIRM,
            EntityType.COURT,
        ):
            key = normalize_entity_name(canonical, et)
            if key:
                mapping[key] = canonical
        # For two-token PERSON names, expand the keyset so all common
        # variants of the same person map back to the canonical spelling:
        #
        #  - swap order ("Liu Yingying" ↔ "Yingying Liu") for East-Asian
        #    vs Western name ordering (existing).
        #  - initial-form ("Y. Liu", "Liu, Y.", "L. Yingying") for the
        #    abbreviated references the AI sometimes emits (Round 7 — fixes
        #    the post-R6 doc 25 "Liu, Y." duplicate).
        #
        # Single-token bare-surname references ("Liu", "Frau Liu" stripped
        # of honorific) are deliberately NOT mapped — a bare surname is
        # ambiguous between "Liu Yingying" and "Liu Jun" (or any other Liu
        # on the case) and snapping would falsely merge them. The
        # honorific-stripped "Frau Liu" → key "liu" case is documented as
        # accepted ambiguity; user can merge manually if needed.
        tokens = canonical.split()
        if len(tokens) == 2:
            first, last = tokens
            variants = [
                f"{last} {first}",  # swapped order
                f"{first[0]}. {last}",  # "Y. Liu"
                f"{last}, {first[0]}.",  # "Liu, Y."
                f"{last[0]}. {first}",  # "L. Yingying" (swapped + initial)
            ]
            for variant in variants:
                key = normalize_entity_name(variant, EntityType.PERSON)
                if key and key not in mapping:
                    mapping[key] = canonical
    return mapping
