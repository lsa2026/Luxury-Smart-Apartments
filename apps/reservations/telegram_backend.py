"""Fixed action-scoped bridge; no service-role or reusable outbound credential."""

import json
import re
from hashlib import sha256

import httpx
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from . import telegram_authority as authority

BASE = "https://lsa25.app.n8n.cloud/webhook/tg39-private-activation-v1"


class Unavailable(Exception):
    pass


class NotReady(Unavailable):
    pass


def configured():
    from .telegram_workgroup import enabled

    return bool(getattr(settings, "TELEGRAM_ACTIVATION_ENABLED", False) and not enabled())


class Backend:
    def __init__(self):
        from .telegram_workgroup import enabled

        if not configured() and not enabled():
            raise Unavailable
        self.ready_checked = False
        self.authority_context = None
        self.client = httpx.Client(timeout=20, follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    @sensitive_variables()
    def call(self, action, key=None):
        try:
            scope = self.authority_context
            envelope = authority.mint(scope, action, key or (scope or {}).get("key"))
            # Private render context includes both canonical JSON strings and
            # their parsed values. Ordinary activation results remain capped8KiB.
            limit = {
                "render_source": 131072,
                "review_policy": 32768,
                "website_handoff": 524288,
            }.get(action, 8192)
            with self.client.stream("POST", BASE, json=envelope) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise Unavailable
                if response.status_code != 200:
                    raise Unavailable
                value = json.loads(body, object_pairs_hook=authority.duplicate_safe)
            if (
                not isinstance(value, dict)
                or set(value) != {"contract", "request_id", "action", "result"}
                or value["contract"] != "tg39_activation_bridge_result_v1"
                or value["request_id"] != envelope["request_id"]
                or value["action"] != action
                or not isinstance(value["result"], dict)
            ):
                raise Unavailable
            return value["result"]
        except (httpx.HTTPError, ValueError, TypeError, authority.Denied) as exc:
            raise Unavailable from exc

    def health(self):
        if self.ready_checked:
            return
        try:
            value = self.call("health")
        except Unavailable as exc:
            raise NotReady from exc
        if (
            value.get("contract") != "tg20_guest_journey_v1"
            or value.get("ready") is not True
            or value.get("dispatch_enabled") is not True
            or value.get("cutover_configured") is not True
            or not isinstance(value.get("components"), dict)
            or any(
                value["components"].get(k) is not True
                for k in ("s01", "reminders", "reviews", "contacts", "loyalty")
            )
        ):
            raise NotReady
        self.ready_checked = True

    def prepare(self, key):
        self.health()
        value = self.call("prepare", key)
        if (
            value.get("booking_available") is not True
            or value.get("source_fresh") is not True
            or value.get("bound") is not False
            or value.get("invitation_state") not in {"none", "expired"}
        ):
            raise NotReady

    def binding(self, key, phone):
        self.health()
        row = self.call("binding", key)
        chat = row.get("chat_id", "")
        if (
            row.get("available") is not True
            or row.get("revoked") is not False
            or not isinstance(chat, str)
            or not re.fullmatch(r"[1-9][0-9]{0,15}", chat)
            or row.get("user_id") != chat
            or row.get("phone_hash") != sha256(phone.lstrip("+").encode()).hexdigest()
        ):
            raise Unavailable
        return chat

    def rpc(self, name, data):
        actions = {
            "tg20_s01_approve_phone": "approve_phone",
            "tg20_s01_issue_invitation": "issue_invitation",
        }
        if name not in actions or not isinstance(data, dict):
            raise Unavailable
        self.health()
        scope, key = self.authority_context or {}, data.get("p_key")
        if key != scope.get("key"):
            raise Unavailable
        if actions[name] == "approve_phone" and (
            data.get("p_phone") != scope.get("phone", "").lstrip("+")
            or data.get("p_evidence_ref") != scope.get("evidence_ref")
        ):
            raise Unavailable
        value = self.call(actions[name], key)
        if value.get("action") not in {"approved", "issued", "hold", "bound"}:
            raise Unavailable
        if value.get("action") == "approved" and value.get("reservation_key") != key:
            raise Unavailable
        return value

    def status(self, key):
        self.health()
        value = self.call("status", key)
        if type(value.get("bound")) is not bool:
            raise Unavailable
        if value["bound"]:
            return "bound"
        if value.get("booking_available") is not True or value.get("source_fresh") is not True:
            raise NotReady
        state = value.get("invitation_state")
        if state not in {"none", "active", "expired"}:
            raise Unavailable
        return "unverified" if state == "none" else state


def checked_invitation(value, *, prefixes="bg"):
    if prefixes not in {"bg", "h", "d"}:
        raise Unavailable
    url = value.get("url", "")
    try:
        expiry = parse_datetime(str(value.get("expires_at", "")))
    except (ValueError, TypeError) as exc:
        raise Unavailable from exc
    if (
        not isinstance(url, str)
        or not re.fullmatch(
            r"https://t\.me/LuxurySmartApartmentsBot\?start=[" + prefixes + r"]_[a-f0-9]{48}", url
        )
        or not expiry
        or not timezone.is_aware(expiry)
        or not 0 < (expiry - timezone.now()).total_seconds() <= 7500
    ):
        raise Unavailable
    return url, expiry
