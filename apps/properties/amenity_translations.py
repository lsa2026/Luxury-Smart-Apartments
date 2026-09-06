"""Load the reviewed amenity copy shipped as repository data.

Every entry here was written by a person, not produced by a translation service:
the site does not machine-translate source content, and an amenity label is read
by a guest deciding whether a home suits them.

The key is the English name exactly as Hostaway returns it, matched case
insensitively. An amenity missing from this map keeps its English name rather
than being guessed at, and the seeding command reports it so the gap is visible.

``category`` groups the list on the property page; ``icon_key`` names the glyph
the template may use. Both are optional and safe to leave empty.
"""

import json
from pathlib import Path
from typing import Final, NamedTuple, cast

from django.utils.translation import gettext_lazy as _


class AmenityCopy(NamedTuple):
    name_ar: str
    name_fr: str
    category: str
    icon_key: str


# Category codes are stable identifiers; the label shown to a guest is
# translated at display time from CATEGORY_LABELS below.
ESSENTIALS: Final = "essentials"
KITCHEN: Final = "kitchen"
COMFORT: Final = "comfort"
LAUNDRY: Final = "laundry"
FAMILY: Final = "family"
SAFETY: Final = "safety"
OUTDOOR: Final = "outdoor"

CATEGORY_LABELS: Final = {
    ESSENTIALS: _("Essentials"),
    KITCHEN: _("Kitchen and dining"),
    COMFORT: _("Comfort and entertainment"),
    LAUNDRY: _("Laundry"),
    FAMILY: _("Family"),
    SAFETY: _("Safety"),
    OUTDOOR: _("Outdoor and parking"),
}

_DATA_FILE: Final = Path(__file__).with_name("data") / "amenity_translations.json"
_VALID_CATEGORIES: Final = frozenset(CATEGORY_LABELS)


def _load_copy() -> dict[str, AmenityCopy]:
    """Read and validate the deliberately hand-written catalogue once at import."""
    payload = cast(dict[str, object], json.loads(_DATA_FILE.read_text(encoding="utf-8")))
    catalogue: dict[str, AmenityCopy] = {}
    for source_name, raw_entry in payload.items():
        if not isinstance(raw_entry, dict):
            raise ValueError(f"Amenity copy for {source_name!r} must be an object")
        required = ("name_ar", "name_fr", "category", "icon_key")
        if any(not isinstance(raw_entry.get(field), str) for field in required):
            raise ValueError(f"Amenity copy for {source_name!r} has an invalid field")
        entry = AmenityCopy(*(str(raw_entry[field]).strip() for field in required))
        if not all(entry) or entry.category not in _VALID_CATEGORIES:
            raise ValueError(f"Amenity copy for {source_name!r} is incomplete")
        normalized_name = " ".join(source_name.split()).casefold()
        if normalized_name in catalogue:
            raise ValueError(f"Duplicate amenity copy key: {normalized_name!r}")
        catalogue[normalized_name] = entry
    return catalogue


AMENITY_COPY: Final = _load_copy()


def copy_for(source_name: str) -> AmenityCopy | None:
    """Look up an amenity by the English name Hostaway returned."""
    return AMENITY_COPY.get(" ".join((source_name or "").split()).casefold())
