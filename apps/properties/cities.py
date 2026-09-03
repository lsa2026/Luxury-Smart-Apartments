"""Canonical public cities supported by the booking website."""

from typing import Final

RIYADH: Final = "Riyadh"
MARRAKECH: Final = "Marrakesh"

SUPPORTED_CITY_ROWS: Final = (
    {
        "city": RIYADH,
        "city_ar": "الرياض",
        "city_en": "Riyadh",
        "city_fr": "Riyad",
    },
    {
        "city": MARRAKECH,
        "city_ar": "مراكش",
        "city_en": "Marrakech",
        "city_fr": "Marrakech",
    },
)

_CITY_ALIASES: Final = {
    # Riyadh and known Hostaway neighborhood values.
    "riyadh": RIYADH,
    "riyad": RIYADH,
    "الرياض": RIYADH,
    "manea al mreidi": RIYADH,
    "irqah": RIYADH,
    "arqah": RIYADH,
    "irqah, riyad": RIYADH,
    "عرقة": RIYADH,
    "عرقة, الرياض": RIYADH,
    # Marrakech spelling variants.
    "marrakesh": MARRAKECH,
    "marrakech": MARRAKECH,
    "مراكش": MARRAKECH,
}


def canonical_city(value: object) -> str:
    """Return a supported city code for a source city or known neighborhood."""
    normalized = " ".join(str(value or "").strip().replace("،", ",").casefold().split())
    return _CITY_ALIASES.get(normalized, "")


def supported_city_rows() -> list[dict[str, str]]:
    """Return fresh rows suitable for localized templates."""
    return [dict(row) for row in SUPPORTED_CITY_ROWS]


def supported_city_choices(language: str) -> list[tuple[str, str]]:
    """Return stable form choices localized for the active language."""
    language = language.split("-")[0]
    label_field = {
        "ar": "city_ar",
        "en": "city_en",
        "fr": "city_fr",
    }.get(language, "city_ar")
    return [(row["city"], row[label_field]) for row in SUPPORTED_CITY_ROWS]


def supported_city_labels(language: str) -> tuple[str, ...]:
    return tuple(label for _, label in supported_city_choices(language))
