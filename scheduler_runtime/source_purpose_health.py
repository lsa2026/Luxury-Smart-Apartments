"""Read-only, four-field operational metadata; never an admission decision."""

import json
import logging
import re
from datetime import datetime
from os import environ

FIELDS = (
    "MESSAGE_LOYALTY_SOURCE_V2_ENABLED",
    "MESSAGE_LOYALTY_SOURCE_CUTOVER_AT",
    "MESSAGE_CANCELLATION_SOURCE_V2_ENABLED",
    "MESSAGE_CANCELLATION_SOURCE_CUTOVER_AT",
)
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


def _cutover(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return ""
    if len(value) > 40 or _ISO.fullmatch(value) is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def snapshot(settings=None, environment=None):
    """Django processes read resolved settings; isolated beat reads only these keys."""
    environment = environ if environment is None else environment
    result = {}
    for key in FIELDS:
        enabled = key.endswith("_ENABLED")
        if settings is not None:
            value = getattr(settings, key, False if enabled else "")
            result[key] = (
                (value if isinstance(value, bool) else None) if enabled else _cutover(value)
            )
        else:
            value = environment.get(key, "false" if enabled else "")
            if not enabled:
                result[key] = _cutover(value)
            elif isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "on"}:
                result[key] = True
            elif isinstance(value, str) and value.strip().casefold() in {"0", "false", "no", "off"}:
                result[key] = False
            else:
                result[key] = None
    return result


def log_snapshot(role, settings=None):
    if role not in {"worker", "beat"}:
        raise ValueError("Unsupported source health role")
    logging.getLogger(__name__).info(
        "LSA_SOURCE_PURPOSE_HEALTH %s",
        json.dumps({"role": role, "values": snapshot(settings=settings)}, sort_keys=True),
    )
