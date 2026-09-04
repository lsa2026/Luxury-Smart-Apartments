"""The administrative regions of Saudi Arabia, for the billing address.

A free-text "state or province" field asks a Saudi guest to invent a value the
gateway then receives inconsistently. The thirteen regions are a closed, stable
list, so they are typed here rather than synced or translated at runtime.

Every other country keeps a free-text field: no list is maintained for them, and
guessing one would be worse than letting the guest write their own.
"""

from typing import Final

from django.utils.translation import gettext_lazy as _

SAUDI_COUNTRY_CODE: Final = "SA"

# Stored value is the English name, which is what the gateway and the channel
# manager both read; the label is what the guest sees in the active language.
SAUDI_REGIONS: Final = (
    ("Riyadh", _("Riyadh Region")),
    ("Makkah", _("Makkah Region")),
    ("Madinah", _("Madinah Region")),
    ("Qassim", _("Qassim Region")),
    ("Eastern Province", _("Eastern Province")),
    ("Asir", _("Asir Region")),
    ("Tabuk", _("Tabuk Region")),
    ("Hail", _("Hail Region")),
    ("Northern Borders", _("Northern Borders Region")),
    ("Jazan", _("Jazan Region")),
    ("Najran", _("Najran Region")),
    ("Al Bahah", _("Al Bahah Region")),
    ("Al Jawf", _("Al Jawf Region")),
)

SAUDI_REGION_VALUES: Final = frozenset(value for value, _label in SAUDI_REGIONS)


def is_saudi_region(value: str) -> bool:
    return (value or "").strip() in SAUDI_REGION_VALUES


def region_choices() -> list[tuple[str, object]]:
    """Choices for the select, with an empty prompt in first position."""
    return [("", _("Choose your region")), *SAUDI_REGIONS]
