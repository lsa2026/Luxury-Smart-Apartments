"""Source-owned template/media selection before first immutable intent.

Pure selection. Inputs must come from source01 READ/source20 private RPC, never
from an HTTP caller. No provider POST, source claim, model, URLs fetch or logs.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .access_guide_pdf_v2 import FIELDS, render_access_guide
from .website_template_contract_v2 import source_timestamp as stamp

PACK_SHA = "97e5ce1947a9bf86476841ea76da9f53e4385fa7a51448d0dbc1877c3c13dcb7"
SELECTION_REF = "yc19-template-render-v2:" + PACK_SHA
HEX = re.compile(r"[0-9a-f]{64}")
ATTACH_KEYS = {
    "kind",
    "asset_ref",
    "sha256",
    "media_type",
    "size_bytes",
    "filename",
    "expires_at",
    "binding_sha256",
}
IDENTITY = (
    "scenario",
    "source_family",
    "job_sha256",
    "reservation_key",
    "reservation_revision",
    "source_event_key",
    "source_message_type",
    "purpose",
)
ZONE_LABELS = {
    "Asia/Riyadh": {"ar": "توقيت الرياض", "en": "Riyadh time", "fr": "heure de Riyad"},
    "Africa/Casablanca": {"ar": "توقيت مراكش", "en": "Marrakech time", "fr": "heure de Marrakech"},
}


class V2Hold(ValueError):
    pass


def canonical2(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest2(value):
    return hashlib.sha256(canonical2(value).encode()).hexdigest()


def content_digest2(language, message):
    return digest2([language, normalize_message2(message, language)])


def normalize_message2(message, language):
    if (
        language not in {"ar", "en", "fr"}
        or not isinstance(message, dict)
        or set(message) != {"mode", "template_ref", "variables", "components_sha256", "attachment"}
        or message.get("mode") != "template"
        or not isinstance(message.get("template_ref"), str)
        or not re.fullmatch(r"[a-z0-9_]{1,180}", message["template_ref"])
        or not isinstance(message.get("components_sha256"), str)
        or not HEX.fullmatch(message["components_sha256"])
    ):
        raise V2Hold("invalid_template_media_message")
    variables = message["variables"]
    if (
        not isinstance(variables, list)
        or len(variables) > 30
        or any(
            not isinstance(v, str)
            or not v.strip()
            or len(v) > 1024
            or any(ord(c) < 32 and c not in "\n\r\t" for c in v)
            for v in variables
        )
    ):
        raise V2Hold("source_variable_missing_or_invalid")
    a = message["attachment"]
    if a is not None:
        if not isinstance(a, dict) or set(a) != ATTACH_KEYS:
            raise V2Hold("invalid_private_attachment")
        if a.get("kind") not in {"image", "document"}:
            raise V2Hold("invalid_private_attachment")
        for k in ("sha256", "binding_sha256"):
            if not isinstance(a.get(k), str) or not HEX.fullmatch(a[k]):
                raise V2Hold("invalid_private_attachment")
        kind = a["kind"]
        ext, mime, limit = (
            ("png", "image/png", 5_000_000)
            if kind == "image"
            else ("pdf", "application/pdf", 2_000_000)
        )
        if (
            a["asset_ref"] != kind + ":" + a["sha256"]
            or a["media_type"] != mime
            or type(a["size_bytes"]) is not int
            or not 1 <= a["size_bytes"] <= limit
            or not isinstance(a["filename"], str)
            or not re.fullmatch(r"[a-z0-9_-]{1,100}[.]" + ext, a["filename"])
        ):
            raise V2Hold("invalid_private_attachment")
        try:
            stamp(a["expires_at"])
        except (ValueError, TypeError):
            raise V2Hold("invalid_private_attachment") from None
    return {
        "mode": "template",
        "template_ref": message["template_ref"],
        "variables": list(variables),
        "components_sha256": message["components_sha256"],
        "attachment": a,
    }


def match_context(facts, context):
    if (
        not isinstance(facts, dict)
        or not isinstance(context, dict)
        or any(context.get(k) != facts.get(k) for k in IDENTITY)
        or not isinstance(facts.get("reservation_revision"), str)
    ):
        raise V2Hold("template_source_identity_changed")
    website = facts.get("source_owner_family") == "website" or facts.get(
        "source_family", ""
    ).startswith(("lsa07", "lsa08"))
    if context.get("recipient_ref") != facts.get("recipient_ref") and not (
        website and context.get("recipient_ref") is None
    ):
        raise V2Hold("template_source_recipient_changed")
    try:
        recipient = facts["recipient_e164"]
        expected = hashlib.sha256((facts["reservation_key"] + ":" + recipient).encode()).hexdigest()
        if context.get("recipient_binding_sha256") != expected:
            raise V2Hold("template_source_recipient_changed")
        if any(stamp(context[k]) != stamp(facts[k]) for k in ("not_before", "expires_at")):
            raise V2Hold("template_source_schedule_changed")
    except (KeyError, TypeError, ValueError) as e:
        if isinstance(e, V2Hold):
            raise
        raise V2Hold("template_source_context_incomplete") from None
    if context.get("language") not in {"ar", "en", "fr"} or facts.get("language") not in (
        None,
        context["language"],
    ):
        raise V2Hold("template_source_language_changed")
    if (
        not isinstance(context.get("source_context_ref"), str)
        or not 1 <= len(context["source_context_ref"]) <= 256
    ):
        raise V2Hold("template_source_mapping_unknown")
    return {**context, "recipient_ref": facts["recipient_ref"]}


def manifests():
    root = Path(__file__).with_name("template_v2_assets")
    pins = {
        "template-source-bindings-v2.json": (
            "deac7e67f8fcccdbb01118087cbf8e0bdb08b585ef79a3273472942d4b25bc5c"
        ),
        "approved-template-provider-v2.json": (
            "19c7a1ef6dfd4dd02c22d7bc4934974296a71c3cff18777698786f1961457fe4"
        ),
    }
    values = []
    for filename, expected in pins.items():
        raw = (root / filename).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise V2Hold("source_registry_integrity_failed")
        values.append(json.loads(raw))
    bindings, provider = values
    if (
        bindings["source_pack_sha256"] != PACK_SHA
        or provider["source_pack_sha256"] != PACK_SHA
        or provider["waba_id"] != bindings["waba_id"]
    ):
        raise V2Hold("template_registry_version_mismatch")
    return bindings, provider


def choose_binding(facts, context, bindings):
    purpose = facts.get("purpose")
    lang = context["language"]
    scenario = facts["scenario"]
    raw = facts["source_message_type"]
    matches = [
        r
        for r in bindings["templates"]
        if r["purpose"] == purpose
        and r["language"] == lang
        and scenario in r["scenarios"]
        and (
            r["source_message_type"] == raw
            or purpose == "booking_extension_confirmation"
            and raw == "extension_confirmation"
        )
    ]
    if purpose in {"review_request", "review_reminder_2", "review_reminder_3"}:
        step = {"review_request": 1, "review_reminder_2": 2, "review_reminder_3": 3}[purpose]
        dest = context.get("review_destination_kind")
        ref = context.get("verified_destination_ref")
        if (
            type(context.get("review_campaign_step")) is not int
            or context["review_campaign_step"] != step
            or dest not in {"google", "ota"}
            or not isinstance(ref, str)
            or not ref
        ):
            raise V2Hold("review_source_step_or_destination_unknown")
        matches = [
            r
            for r in matches
            if r["name"].startswith(
                "lsa_google_review_" if dest == "google" else "lsa_channel_review_"
            )
        ]
    if len(matches) != 1:
        raise V2Hold("template_source_scope_ambiguous")
    return matches[0]


@dataclass(frozen=True, repr=False)
class SelectedMessage:
    message: dict
    content_digest: str
    selection_ref: str
    source_values_sha256: str
    context: dict = field(repr=False)
    attachment_bytes: bytes | None = field(default=None, repr=False)


def select_source_message(
    facts, context, *, bindings=None, provider=None, live_snapshot=None, asset_reader=None
):
    context = match_context(facts, context)
    if bindings is None or provider is None:
        bindings, provider = manifests()
    binding = choose_binding(facts, context, bindings)
    pins = [
        r
        for r in provider["snapshots"]
        if r["name"] == binding["name"] and r["language"] == binding["language"]
    ]
    if len(pins) != 1:
        raise V2Hold("template_provider_pin_missing")
    pin = pins[0]
    snapshot = live_snapshot or pin
    if (
        snapshot.get("waba_id") != bindings["waba_id"]
        or snapshot.get("name") != binding["name"]
        or snapshot.get("language") != binding["language"]
        or snapshot.get("status") != "APPROVED"
        or snapshot.get("category") not in {"UTILITY", "MARKETING"}
        or snapshot.get("category") != pin.get("category")
        or snapshot.get("components_sha256") != pin.get("components_sha256")
        or snapshot.get("components_sha256") != digest2(snapshot.get("components"))
    ):
        raise V2Hold("template_provider_approval_changed")
    components = snapshot["components"]
    bodies = [c for c in components if c.get("type") == "BODY"]
    headers = [c for c in components if c.get("type") == "HEADER"]
    if (
        len(bodies) != 1
        or len(headers) > 1
        or any(c.get("type") not in {"BODY", "HEADER", "FOOTER"} for c in components)
    ):
        raise V2Hold("template_components_unsupported")
    if hashlib.sha256(bodies[0].get("text", "").encode()).hexdigest() != binding["body_sha256"]:
        raise V2Hold("template_body_changed")
    keys = binding["ordered_source_keys"]
    indexes = sorted({int(v) for v in re.findall(r"\{\{([1-9][0-9]*)\}\}", bodies[0]["text"])})
    if indexes != list(range(1, len(keys) + 1)) or len(set(keys)) != len(keys):
        raise V2Hold("template_source_order_changed")
    values = dict(context.get("values") or {})
    zone = values.get("timezone")
    if "timezone_display" in keys:
        if values.get("timezone_display") != zone:
            raise V2Hold("template_timezone_source_unknown")
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(zone)
        except Exception:
            raise V2Hold("template_timezone_source_unknown") from None
        values["timezone_display"] = ZONE_LABELS.get(zone, {}).get(context["language"], zone)
    required = set(keys) | (
        set(FIELDS)
        if binding.get("asset", {}) and binding["asset"]["kind"] == "document"
        else set()
    )
    curated = {k: values.get(k) for k in sorted(required)}
    if any(not isinstance(v, str) or not v.strip() for v in curated.values()):
        raise V2Hold("template_source_variable_missing")
    params = [curated[k] for k in keys]
    asset = binding.get("asset")
    attachment = None
    raw = None
    if asset:
        kind = asset["kind"]
        fmt = "IMAGE" if kind == "image" else "DOCUMENT"
        if len(headers) != 1 or headers[0].get("format") != fmt:
            raise V2Hold("template_media_header_changed")
        if kind == "image":
            raw = (
                asset_reader(asset["sha256"])
                if asset_reader
                else (
                    Path(__file__).with_name("template_v2_assets") / (asset["sha256"] + ".png")
                ).read_bytes()
            )
            if (
                not isinstance(raw, bytes)
                or len(raw) != asset["size_bytes"]
                or hashlib.sha256(raw).hexdigest() != asset["sha256"]
                or not raw.startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise V2Hold("template_private_image_not_verified")
        else:
            raw = render_access_guide(curated, context["language"])
        h = hashlib.sha256(raw).hexdigest()
        value_sha = digest2(curated)
        bound = digest2(
            [
                "yc19-attachment-binding-2",
                *[
                    facts[k]
                    for k in (
                        "source_family",
                        "scenario",
                        "job_sha256",
                        "reservation_key",
                        "reservation_revision",
                        "recipient_ref",
                        "purpose",
                    )
                ],
                context["language"],
                facts["source_event_key"],
                value_sha,
                h,
            ]
        )
        attachment = {
            "kind": kind,
            "asset_ref": kind + ":" + h,
            "sha256": h,
            "media_type": "image/png" if kind == "image" else "application/pdf",
            "size_bytes": len(raw),
            "filename": ("stay-image-" if kind == "image" else "arrival-guide-")
            + context["language"]
            + (".png" if kind == "image" else ".pdf"),
            "expires_at": datetime.fromtimestamp(stamp(facts["expires_at"]), UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "binding_sha256": bound,
        }
    elif headers:
        raise V2Hold("unexpected_template_media_header")
    message = normalize_message2(
        {
            "mode": "template",
            "template_ref": binding["name"],
            "variables": params,
            "components_sha256": pin["components_sha256"],
            "attachment": attachment,
        },
        context["language"],
    )
    return SelectedMessage(
        message,
        content_digest2(context["language"], message),
        SELECTION_REF,
        digest2(curated),
        context,
        raw,
    )
