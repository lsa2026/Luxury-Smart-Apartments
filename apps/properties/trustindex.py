"""Approved Trustindex widget routing for each property and interface language."""

import json
import re
from functools import lru_cache
from pathlib import Path

from django.utils import translation

_WIDGET_ID = re.compile(r"^[A-Za-z0-9]{20,32}$")
_MAPPING_PATH = Path(__file__).resolve().parent / "data" / "trustindex_widgets.json"


@lru_cache(maxsize=1)
def widget_mapping() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(_MAPPING_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _valid_widget_id(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _WIDGET_ID.fullmatch(candidate) else ""


def full_review_widget_id(property_obj: object, language: str | None = None) -> str:
    config = widget_mapping().get(str(getattr(property_obj, "hostaway_listing_id", "")), {})
    widgets = config.get("full_review_widgets", {})
    if not isinstance(widgets, dict):
        return ""
    language_code = (language or translation.get_language() or "ar").split("-")[0]
    return _valid_widget_id(widgets.get(language_code))


def metrics_widget_id(property_obj: object) -> str:
    config = widget_mapping().get(str(getattr(property_obj, "hostaway_listing_id", "")), {})
    widgets = config.get("full_review_widgets", {})
    if isinstance(widgets, dict):
        for language_code in ("en", "ar", "fr"):
            if widget_id := _valid_widget_id(widgets.get(language_code)):
                return widget_id
    return _valid_widget_id(getattr(property_obj, "trustindex_widget_id", ""))
