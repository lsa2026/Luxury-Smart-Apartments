"""Private fixed-destination adapter. Configuration is absent/disabled by default."""

import re
from hashlib import sha256

import httpx
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

BASE = "https://rsqgwepnowpnpealrzbn.supabase.co/rest/v1/"


class Unavailable(Exception):
    pass


class NotReady(Unavailable):
    pass


def configured():
    return bool(
        getattr(settings, "TELEGRAM_ACTIVATION_ENABLED", False)
        and getattr(settings, "TELEGRAM_ACTIVATION_SERVICE_KEY", "")
    )


class Backend:
    def __init__(self):
        if not configured():
            raise Unavailable
        key = settings.TELEGRAM_ACTIVATION_SERVICE_KEY
        self.ready_checked = False
        self.client = httpx.Client(
            base_url=BASE,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=5,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self):
        self.client.close()

    def health(self):
        if self.ready_checked:
            return
        try:
            response = self.client.post("rpc/tg20_s01_health", json={})
            value = response.json()
            if (
                response.status_code != 200
                or not isinstance(value, dict)
                or value.get("contract") != "tg20_guest_journey_v1"
                or value.get("ready") is not True
                or type(value.get("dispatch_enabled")) is not bool
                or type(value.get("cutover_configured")) is not bool
                or not isinstance(value.get("components"), dict)
                or any(
                    value["components"].get(k) is not True
                    for k in ("s01", "reminders", "reviews", "contacts", "loyalty")
                )
            ):
                raise NotReady
        except (httpx.HTTPError, ValueError) as exc:
            raise NotReady from exc
        self.ready_checked = True

    def binding(self, key, phone):
        self.health()
        try:
            response = self.client.get(
                "tg20_s01_bindings",
                params={
                    "reservation_key": "eq." + key,
                    "select": "user_id,chat_id,phone_hash,revoked",
                    "limit": "1",
                },
            )
            rows = response.json()
            if response.status_code != 200 or not isinstance(rows, list) or len(rows) != 1:
                raise Unavailable
            row = rows[0]
            chat = row.get("chat_id", "")
            if (
                row.get("revoked") is not False
                or not isinstance(chat, str)
                or not re.fullmatch(r"[1-9][0-9]{0,15}", chat)
                or row.get("user_id") != chat
                or row.get("phone_hash") != sha256(phone.lstrip("+").encode()).hexdigest()
            ):
                raise Unavailable
            return chat
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise Unavailable from exc

    def rpc(self, name, data):
        if name not in {"tg20_s01_approve_phone", "tg20_s01_issue_invitation"}:
            raise Unavailable
        self.health()
        try:
            result = self.client.post("rpc/" + name, json=data)
            if result.status_code != 200:
                raise Unavailable
            value = result.json()
            if not isinstance(value, dict) or value.get("action") not in {
                "approved",
                "issued",
                "hold",
                "bound",
            }:
                raise Unavailable
            return value
        except (httpx.HTTPError, ValueError) as exc:
            raise Unavailable from exc

    def status(self, key):
        self.health()
        # Explicit minimal columns; never retrieve snapshots/contact data or token hashes.
        for table, columns in (
            ("bindings", "revoked"),
            ("invitations", "revoked,expires_at"),
        ):
            try:
                response = self.client.get(
                    "tg20_s01_" + table,
                    params={
                        "reservation_key": "eq." + key,
                        "select": columns,
                        "limit": "1",
                        **(
                            {"order": "created_at.desc", "consumed_by": "is.null"}
                            if table == "invitations"
                            else {}
                        ),
                    },
                )
                if response.status_code != 200:
                    raise Unavailable
                rows = response.json()
                if not isinstance(rows, list):
                    raise Unavailable
                if rows:
                    row = rows[0]
                    if not isinstance(row, dict) or type(row.get("revoked")) is not bool:
                        raise Unavailable
                    if table == "bindings" and not row["revoked"]:
                        return "bound"
                    if table == "invitations" and not row["revoked"]:
                        expiry = parse_datetime(str(row.get("expires_at", "")))
                        if expiry and timezone.is_aware(expiry):
                            return "active" if expiry > timezone.now() else "expired"
                        raise Unavailable
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                raise Unavailable from exc
        return "unverified"


def checked_invitation(value):
    url = value.get("url", "")
    try:
        expiry = parse_datetime(str(value.get("expires_at", "")))
    except (ValueError, TypeError) as exc:
        raise Unavailable from exc
    if (
        not isinstance(url, str)
        or not re.fullmatch(r"https://t\.me/LuxurySmartApartmentsBot\?start=b_[a-f0-9]{48}", url)
        or not expiry
        or not timezone.is_aware(expiry)
        or not 0 < (expiry - timezone.now()).total_seconds() <= 7500
    ):
        raise Unavailable
    return url, expiry
