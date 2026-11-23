"""Exact closed v2 content contract. Validation does not authorize source ACK."""

import hashlib
import json
import re
from datetime import datetime

MESSAGE_KEYS = {"mode", "template_ref", "variables", "components_sha256", "attachment"}
ATTACHMENT_KEYS = {
    "kind",
    "asset_ref",
    "sha256",
    "media_type",
    "size_bytes",
    "filename",
    "expires_at",
    "binding_sha256",
}
HEX = re.compile(r"[a-f0-9]{64}\Z")


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_message(language, message):
    if (
        language not in {"ar", "en", "fr"}
        or not isinstance(message, dict)
        or set(message) != MESSAGE_KEYS
        or message["mode"] != "template"
        or not isinstance(message["template_ref"], str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,511}", message["template_ref"])
        or not isinstance(message["components_sha256"], str)
        or not HEX.fullmatch(message["components_sha256"])
        or not isinstance(message["variables"], list)
        or not 1 <= len(message["variables"]) <= 40
        or any(
            not isinstance(v, str)
            or not v.strip()
            or len(v) > 1024
            or re.search(r"[\x00-\x1f]|\{\{|\}\}", v)
            for v in message["variables"]
        )
    ):
        raise ValueError("invalid_template_message")
    attachment = message["attachment"]
    if attachment is None:
        return
    if not isinstance(attachment, dict) or set(attachment) != ATTACHMENT_KEYS:
        raise ValueError("invalid_attachment")
    kind = attachment["kind"]
    if kind not in {"image", "document"}:
        raise ValueError("invalid_attachment_kind")
    for key in ("sha256", "binding_sha256"):
        if not isinstance(attachment[key], str) or not HEX.fullmatch(attachment[key]):
            raise ValueError("invalid_attachment_hash")
    maximum = 5000000 if kind == "image" else 2000000
    if (
        attachment["asset_ref"] != kind + ":" + attachment["sha256"]
        or attachment["media_type"] != ("image/png" if kind == "image" else "application/pdf")
        or type(attachment["size_bytes"]) is not int
        or not 1 <= attachment["size_bytes"] <= maximum
        or not isinstance(attachment["filename"], str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}\.(png|pdf)", attachment["filename"])
        or not attachment["filename"].endswith(".png" if kind == "image" else ".pdf")
        or (kind == "document" and attachment["filename"] != f"arrival-guide-{language}.pdf")
        or not isinstance(attachment["expires_at"], str)
    ):
        raise ValueError("invalid_attachment_metadata")
    expiry = datetime.fromisoformat(attachment["expires_at"].replace("Z", "+00:00"))
    if expiry.utcoffset() is None:
        raise ValueError("invalid_attachment_expiry")


def content_digest(language, message):
    validate_message(language, message)
    return sha(canonical([language, message]))


def attachment_binding(facts, values, asset_sha256):
    fields = (
        "source_family",
        "scenario",
        "job_sha256",
        "reservation_key",
        "reservation_revision",
        "recipient_ref",
        "purpose",
        "language",
        "source_event_key",
    )
    if any(not isinstance(facts.get(k), str) or not facts[k] for k in fields):
        raise ValueError("attachment_source_identity_missing")
    if not isinstance(values, dict) or not HEX.fullmatch(asset_sha256):
        raise ValueError("attachment_source_values_missing")
    return sha(
        canonical(
            [
                "yc19-attachment-binding-2",
                *[facts[k] for k in fields],
                sha(canonical(values)),
                asset_sha256,
            ]
        )
    )
