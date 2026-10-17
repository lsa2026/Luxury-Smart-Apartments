"""Shared identity and locale formatting. No transport or guest state."""

BRAND = "Luxury Smart Apartments"


def points_text(value, language):
    from apps.core.templatetags.presentation import localized_number

    text = format(value, ".2f")
    return text.replace(".", ",") if language == "fr" else localized_number(text)
