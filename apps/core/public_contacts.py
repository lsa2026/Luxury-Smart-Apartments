"""Public business aliases, separate from owner login and SMTP credentials."""

GENERAL_EMAIL = "info@luxurysmartapartments.com"
RESERVATIONS_EMAIL = "reservations@luxurysmartapartments.com"
LEGACY_OWNER_EMAIL = "saeed@luxurysmartapartments.com"


def public_contact_email(value: str, *, reservations: bool = False) -> str:
    """Replace our retired public address; preserve explicitly configured alternatives."""
    if value.strip().casefold() in {LEGACY_OWNER_EMAIL, GENERAL_EMAIL, RESERVATIONS_EMAIL}:
        return RESERVATIONS_EMAIL if reservations else GENERAL_EMAIL
    return value
