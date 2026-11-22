"""Deterministic source HTML with escaped variables and exact context digests."""

import hashlib
import html
import json
import re

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from .telegram_authority import Denied, duplicate_safe

OPERATIONAL = {
    "address_ar",
    "address_en",
    "address_fr",
    "map_url",
    "wifi_net",
    "wifi_pass",
    "gate_code",
    "video_url",
    "timezone",
}
METADATA = (
    "source_owner",
    "source_key",
    "hostaway_reservation_id",
    "scenario",
    "stay_key",
    "event_id",
    "source_revision",
    "job_key",
    "reference_id",
    "purpose",
    "sequence",
    "listing_map_id",
    "language",
    "phone_hash",
)
CANONICAL = METADATA + (
    "source_template_name",
    "source_template_sha256",
    "template_version",
    "variables_sha256",
)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def checked_json(raw, digest):
    if not isinstance(raw, str) or len(raw.encode()) > 32768 or sha(raw) != digest:
        raise Denied
    value = json.loads(raw, object_pairs_hook=duplicate_safe)
    if not isinstance(value, dict):
        raise Denied
    return value


@sensitive_variables()
def verified_context(value, expected):
    if (
        not isinstance(value, dict)
        or value.get("contract") != "tg39_stay_render_source_v1"
        or value.get("available") is not True
        or value.get("dispatch_allowed") is not False
        or any(
            type(value.get(key)) is not type(expected[key]) or value[key] != expected[key]
            for key in METADATA
        )
    ):
        raise Denied
    template = value.get("source_template_text")
    if (
        not isinstance(template, str)
        or len(template.encode()) > 16384
        or sha(template) != value.get("source_template_sha256")
        or value.get("source_template_name") != expected["purpose"]
        or value.get("template_version") != "sha256:" + sha(template)
    ):
        raise Denied
    variables = checked_json(value.get("variables_canonical_json"), value.get("variables_sha256"))
    context = checked_json(value.get("context_canonical_json"), value.get("context_sha256"))
    if (
        variables != value.get("variables")
        or not set(variables) <= OPERATIONAL
        or any(v is not None and not isinstance(v, str) for v in variables.values())
        or any(
            type(context.get(k)) is not type(value[k]) or context[k] != value[k] for k in CANONICAL
        )
    ):
        raise Denied
    at = parse_datetime(value.get("authoritative_at", ""))
    expiry = parse_datetime(value.get("expires_at", ""))
    now = timezone.now()
    if (
        at is None
        or expiry is None
        or not timezone.is_aware(at)
        or not timezone.is_aware(expiry)
        or not 0 < (expiry - at).total_seconds() <= 30
        or not at <= now < expiry
    ):
        raise Denied
    return template, variables


@sensitive_variables()
def render_html(template, variables, language):
    if language not in {"ar", "en", "fr"} or not isinstance(template, str):
        raise Denied
    output, depth = [], 0
    # Parse the trusted template before inserting guest values. A guest's tags,
    # entities, stars or underscores can never become markup or attributes.
    for part in re.split(r"(<[^>]*>)", template):
        if part.startswith("<") and part.endswith(">"):
            if re.fullmatch(r"<br\s*/?>", part, re.I):
                output.append("\n")
            elif part.lower() == "<b>":
                depth += 1
                output.append("<b>")
            elif part.lower() == "</b>" and depth:
                depth -= 1
                output.append("</b>")
            else:
                raise Denied
            continue
        end = 0
        for match in re.finditer(r"\{\{\s*(\w+)\s*\}\}", part, re.ASCII):
            output.append(html.escape(html.unescape(part[end : match.start()]), quote=False))
            key = match[1]
            if key not in variables or not (
                variables[key] is None or isinstance(variables[key], str)
            ):
                raise Denied
            output.append(html.escape(variables[key] or "", quote=False))
            end = match.end()
        output.append(html.escape(html.unescape(part[end:]), quote=False))
    if depth:
        raise Denied
    result = "".join(output)
    if language == "ar":
        result = "\u200f" + result.replace("\n", "\n\u200f")
    return result


def message(language, rendered, asset=None):
    if language not in {"ar", "en", "fr"} or not isinstance(rendered, str) or not rendered:
        raise Denied
    plain = html.unescape(re.sub(r"</?b>", "", rendered))
    if len(plain.encode("utf-16-le")) // 2 > (1024 if asset else 4096):
        raise Denied("telegram_message_oversize")
    if asset and (
        set(asset) != {"asset_ref", "asset_sha256", "asset_mime"}
        or asset["asset_mime"] != "image/png"
        or not re.fullmatch(r"[a-f0-9]{64}", asset["asset_sha256"])
    ):
        raise Denied
    method = "photo" if asset else "text"
    result = {
        "method": method,
        "parse_mode": "HTML",
        "content_sha256": sha(
            json.dumps(
                [language, "HTML", method, rendered, asset["asset_sha256"] if asset else ""],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ),
        "caption_html" if asset else "body_html": rendered,
    }
    if asset:
        result.update(asset)
    return result
