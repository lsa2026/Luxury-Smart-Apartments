"""Independent text override. Pure selection; caller messages confer no retention authority."""

import hashlib
import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TEXT_NAMES = frozenset({"lsa_checkin_reminder_v1", "lsa_sameday_checkin_guide_v1"})
DOCUMENT_NAMES = frozenset({"lsa_checkin_arrival_document_v2", "lsa_stay_arrival_document_v2"})


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def registry():
    raw = (
        (Path(__file__).with_name("template_v2_assets") / "text-access-profile-v1.json")
        .read_bytes()
        .replace(b"\r\n", b"\n")
    )
    if (
        hashlib.sha256(raw).hexdigest()
        != "f8d885fb3868b283acbf110a4236bd341c38cda12f7c50e7f5d1037577bf7b1f"
    ):
        raise ValueError("text_access_registry_changed")
    value = json.loads(raw)
    content = {k: v for k, v in value.items() if k != "override_sha256"}
    if (
        value.get("contract_version") != "yc19-text-access-profile-1"
        or value.get("waba_id") != "2075336595945655"
        or hashlib.sha256(canonical(content).encode()).hexdigest() != value.get("override_sha256")
    ):
        raise ValueError("text_access_registry_changed")
    pairs = {(x["name"], x["language"]) for x in value["snapshots"]}
    if (
        pairs != {(name, language) for name in TEXT_NAMES for language in ("ar", "en", "fr")}
        or len(value["snapshots"]) != 6
    ):
        raise ValueError("text_access_registry_scope")
    for pin in value["snapshots"]:
        parts = pin.get("components")
        body = parts[0] if isinstance(parts, list) and len(parts) == 1 else {}
        if (
            pin.get("waba_id") != value["waba_id"]
            or pin.get("status") != "APPROVED"
            or pin.get("category") != "UTILITY"
            or body.get("type") != "BODY"
            or not isinstance(body.get("text"), str)
            or pin.get("components_sha256") != hashlib.sha256(canonical(parts).encode()).hexdigest()
            or pin.get("body_sha256") != hashlib.sha256(body["text"].encode()).hexdigest()
            or sorted({int(v) for v in re.findall(r"\{\{([1-9][0-9]*)\}\}", body["text"])})
            != list(range(1, 9))
        ):
            raise ValueError("text_access_provider_pin_changed")
    return value


def selection_identity(message):
    if isinstance(message, dict) and message.get("template_ref") in TEXT_NAMES:
        sha = registry()["override_sha256"]
        return "yc19-text-access-render-1:" + sha, sha
    from .website_template_selector_v2 import PACK_SHA, SELECTION_REF

    return SELECTION_REF, PACK_SHA


def local_day(epoch, zone):
    instant = datetime.fromtimestamp(epoch, UTC)
    # tzdb2026c/africa: permanent GMT from20Sep2026 02:00 local(+01).
    # Keep the IANA identity while matching source20/site01 on older host tzdata.
    if zone == "Africa/Casablanca" and instant >= datetime(2026, 9, 20, 1, tzinfo=UTC):
        return instant.date()
    if zone not in {"Asia/Riyadh", "Africa/Casablanca"}:
        raise ValueError("unverified_access_timezone")
    return instant.astimezone(ZoneInfo(zone)).date()


def reminder_matches(values, now):
    try:
        raw = values.get("arrival_date")
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return False
        return values.get("check_in_time") == "15:00" and date.fromisoformat(raw) == local_day(
            now, values.get("timezone")
        ) + timedelta(days=1)
    except (ValueError, TypeError, OverflowError, OSError):
        return False


def override(base, facts, context, *, enabled=False, retained_message=None, now=None):
    """Retained message must be from matching durable prepared intent, never caller body."""
    old = retained_message.get("template_ref") if isinstance(retained_message, dict) else None
    if base.get("name") not in DOCUMENT_NAMES or old in DOCUMENT_NAMES:
        return None
    if not enabled and old not in TEXT_NAMES:
        return None
    purpose = facts.get("purpose")
    raw = facts.get("source_message_type")
    if (purpose, raw) not in {
        ("checkin_reminder", "reminder_checkin"),
        ("checkin_guide", "checkin_guide"),
    }:
        return None
    if purpose == "checkin_reminder" and facts.get("scenario") in {"07", "08"}:
        raise ValueError("website_reminder_not_supported")
    name = (
        "lsa_checkin_reminder_v1"
        if purpose == "checkin_reminder"
        and reminder_matches(context.get("values") or {}, time.time() if now is None else now)
        else "lsa_sameday_checkin_guide_v1"
    )
    doc = registry()
    lang = context["language"]
    pin = next(p for p in doc["snapshots"] if p["name"] == name and p["language"] == lang)
    binding = {
        **base,
        "name": name,
        "body": pin["components"][0]["text"],
        "body_sha256": pin["body_sha256"],
        "ordered_source_keys": doc["ordered_source_keys"][name],
        "asset": None,
    }
    return binding, pin, "yc19-text-access-render-1:" + doc["override_sha256"]


def requested_profile(selection_ref, pack_sha):
    """Recognized prepared profile selects a renderer, never source authority."""
    if not isinstance(selection_ref, str) or not selection_ref.startswith(
        "yc19-text-access-render-1:"
    ):
        return False
    sha = registry()["override_sha256"]
    if selection_ref != "yc19-text-access-render-1:" + sha or pack_sha != sha:
        raise ValueError("text_access_profile_mismatch")
    return True
