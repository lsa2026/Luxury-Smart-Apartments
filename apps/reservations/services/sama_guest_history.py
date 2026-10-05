"""Exact-phone history projection; no names, contacts, prices or access secrets."""

import time
import uuid
from datetime import datetime

from django.core.cache import cache
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.exceptions import HostawayResponseError
from apps.properties.models import Property

from .sama_guest_context import phone_key, project_stay


def guest_history(phone, *, client_factory=HostawayClient):
    now = timezone.now()
    cutoff = now.date()
    key = f"sma-guest-history-v1:{cutoff}"
    document = cache.get(key) or {"stays": [], "seen": [], "cursor": None, "complete": False}
    if not document["complete"] and not document.get("bounded"):
        lock_key, lock_owner = key + ":lock", uuid.uuid4().hex
        if cache.add(lock_key, lock_owner, timeout=30):
            try:
                document = _populate_history(document, key, cutoff, client_factory)
            finally:
                if cache.get(lock_key) == lock_owner:
                    cache.delete(lock_key)
        else:
            document = cache.get(key) or {
                "stays": [],
                "complete": False,
                "checked_at": now.isoformat(),
            }
    matches = []
    for row in document["stays"]:
        if row["phone_key"] == phone_key(phone) and datetime.fromisoformat(row["ends_at"]) <= now:
            matches.append({k: v for k, v in row.items() if k != "phone_key"})
    matches.sort(key=lambda s: (s["check_out"], s["check_in"], s["property_slug"]), reverse=True)
    return {
        "code": "guest_history",
        "source": "hostaway_read_only",
        "match_key": phone_key(phone),
        "checked_at": document["checked_at"],
        "coverage": "complete" if document["complete"] else "partial",
        "stays": matches[:5],
    }


def _populate_history(document, key, cutoff, client_factory):
    properties = {}
    for p in Property.objects.filter(is_visible=True, hostaway_is_active=True):
        identifier = p.hostaway_listing_map_id
        if identifier:
            if identifier in properties:
                raise HostawayResponseError("Ambiguous history property mapping.")
            properties[identifier] = p
    if not properties:
        raise HostawayResponseError("No history property mapping.")
    seen = set(document["seen"])
    deadline = time.monotonic() + 18
    with client_factory(max_get_attempts=1, timeout=4) as client:
        for _ in range(20):
            if time.monotonic() > deadline:
                break
            rows = client.guest_history_page(cutoff, after_id=document["cursor"])
            if time.monotonic() > deadline:
                break
            if not isinstance(rows, list) or len(rows) > 100:
                raise HostawayResponseError("Unbounded history page.")
            for row in rows:
                if len(seen) >= 10000:
                    document["bounded"] = True
                    break
                identifier = row.get("id")
                if type(identifier) is not int or identifier <= 0 or identifier in seen:
                    cache.delete(key)
                    raise HostawayResponseError("Unverified history pagination.")
                seen.add(identifier)
                stay = project_stay(row, properties)
                if stay:
                    guests = row.get("numberOfGuests")
                    if type(guests) is not int or not 1 <= guests <= 20:
                        guests = None
                    document["stays"].append({**stay, "guests": guests})
            if not rows:
                document["complete"] = True
                break
            document["cursor"] = rows[-1]["id"]
            if len(seen) >= 10000:
                document["bounded"] = True
                break
            if time.monotonic() > deadline:
                break
    document["seen"] = list(seen)
    document["checked_at"] = timezone.now().isoformat()
    cache.set(key, document, timeout=60)
    return document
