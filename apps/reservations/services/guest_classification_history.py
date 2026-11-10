"""Finite, admin-triggered, read-only all-channel Hostaway scan; no raw contacts stored."""

import secrets
import uuid
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from apps.integrations.hostaway.client import HostawayClient
from apps.integrations.hostaway.reservation_validators import normalize_hostaway_reservation_status
from apps.reservations.models import Reservation

from .guest_classification import contact_keys

PREFIX = "lsa:guest-classification-hostaway:v1"
SNAPSHOT = PREFIX + ":complete"
ACTIVE = PREFIX + ":active"
STATE = PREFIX + ":state"
FRESH_SECONDS = 15 * 60
JOB_SECONDS = 30 * 60
MAX_PAGES = 1000


def shared_cache_available():
    return settings.CACHES["default"]["BACKEND"].endswith("RedisCache")


@contextmanager
def control_lock():
    token, key = secrets.token_hex(16), PREFIX + ":lock"
    acquired = cache.add(key, token, timeout=45)
    try:
        yield acquired
    finally:
        if acquired and cache.get(key) == token:
            cache.delete(key)


def _provider_snapshot():
    if not shared_cache_available():
        return None
    snapshot = cache.get(SNAPSHOT)
    if not isinstance(snapshot, dict) or snapshot.get("state") != "complete":
        return None
    if snapshot.get("key_version") != salted_hmac(PREFIX, "contact-key-version").hexdigest():
        return None
    checked = parse_datetime(snapshot.get("completed_at", ""))
    if (
        not checked
        or timezone.is_naive(checked)
        or not timedelta(0) <= timezone.now() - checked < timedelta(seconds=FRESH_SECONDS)
    ):
        return None
    return snapshot


def provider_snapshot():
    try:
        return _provider_snapshot()
    except Exception:
        return None


def _provider_state():
    snapshot = provider_snapshot()
    if snapshot is not None and not cache.get(ACTIVE):
        return {"status": "complete", "completed_at": snapshot["completed_at"]}
    state = cache.get(STATE) or {"status": "unavailable"}
    if state.get("status") == "complete":
        return {"status": "unavailable"}
    return {key: state[key] for key in ("status", "completed_at") if key in state}


def provider_state():
    try:
        return _provider_state()
    except Exception:
        return {"status": "unavailable"}


def dispatch(generation, cursor):
    from apps.reservations.tasks import refresh_guest_classification_history

    refresh_guest_classification_history.apply_async(args=[generation, cursor], countdown=1)


def _ensure_refresh(*, force=False):
    if not shared_cache_available():
        return {"status": "unavailable"}
    with control_lock() as acquired:
        if not acquired:
            return provider_state()
        if not force and provider_snapshot() is not None:
            return provider_state()
        active = cache.get(ACTIVE)
        job = cache.get(f"{PREFIX}:job:{active}") if active else None
        updated = parse_datetime(job.get("updated_at", "")) if isinstance(job, dict) else None
        if (
            updated
            and timezone.is_aware(updated)
            and timezone.now() - updated < timedelta(seconds=90)
        ):
            return provider_state()
        state = cache.get(STATE) or {}
        failed = parse_datetime(state.get("failed_at", ""))
        if (
            not force
            and failed
            and timezone.is_aware(failed)
            and timezone.now() - failed < timedelta(seconds=60)
        ):
            return provider_state()
        generation = uuid.uuid4().hex
        cache.delete(SNAPSHOT)
        cache.set(ACTIVE, generation, timeout=JOB_SECONDS)
        cache.set(
            f"{PREFIX}:job:{generation}",
            {
                "cursor": None,
                "pages": 0,
                "seen": [],
                "email": {},
                "phone": {},
                "updated_at": timezone.now().isoformat(),
            },
            timeout=JOB_SECONDS,
        )
        cache.set(STATE, {"status": "queued"}, timeout=JOB_SECONDS)
        try:
            dispatch(generation, None)
        except Exception:
            fail(generation)
        return provider_state()


def ensure_refresh(*, force=False):
    try:
        return _ensure_refresh(force=force)
    except Exception:
        return {"status": "unavailable"}


def fail(generation):
    if cache.get(ACTIVE) == generation:
        cache.delete(ACTIVE)
        cache.set(
            STATE,
            {"status": "unavailable", "failed_at": timezone.now().isoformat()},
            timeout=JOB_SECONDS,
        )
        cache.delete(f"{PREFIX}:job:{generation}")


def refresh_page(generation, expected_cursor, *, client=None):
    if not shared_cache_available():
        return "unavailable"
    with control_lock() as acquired:
        if not acquired:
            return "busy"
        key = f"{PREFIX}:job:{generation}"
        job = cache.get(key)
        if (
            cache.get(ACTIVE) != generation
            or not isinstance(job, dict)
            or job["cursor"] != expected_cursor
        ):
            return "obsolete"
        try:
            # Existing vetted all-reservation cursor: no channel, date, name,
            # email or phone filter; includes Booking.com and other channels.
            if client is None:
                with HostawayClient(max_get_attempts=1, timeout=5) as remote:
                    rows = remote.invoice_search_page(after_id=expected_cursor)
            else:
                rows = client.invoice_search_page(after_id=expected_cursor)
            if not isinstance(rows, list) or len(rows) > 50:
                raise ValueError("invalid_page")
            ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
            if (
                any(type(value) is not int or value <= 0 for value in ids)
                or len(set(ids)) != len(ids)
                or set(ids) & set(job["seen"])
            ):
                raise ValueError("invalid_cursor")
            tests = set(
                Reservation.objects.filter(is_test=True).values_list(
                    "hostaway_reservation_id", flat=True
                )
            )
            for row in rows:
                status = row.get("status")
                if not isinstance(status, str):
                    raise ValueError("invalid_status")
                normalized = normalize_hostaway_reservation_status(status)
                if normalized == "unknown":
                    raise ValueError("unrecognized_status")
                if row["id"] in tests or normalized not in Reservation.ACTIVE_STATUSES:
                    continue
                if not all(
                    row.get(name) is None or isinstance(row.get(name), str)
                    for name in ("guestEmail", "phone")
                ):
                    raise ValueError("invalid_contact")
                for kind, digest in zip(
                    ("email", "phone"),
                    contact_keys(row.get("guestEmail"), row.get("phone")),
                    strict=True,
                ):
                    if digest:
                        references = job[kind].setdefault(digest, [])
                        if row["id"] not in references and len(references) < 2:
                            references.append(row["id"])
            if not rows:
                completed = timezone.now().isoformat()
                cache.set(
                    SNAPSHOT,
                    {
                        "state": "complete",
                        "email": job["email"],
                        "phone": job["phone"],
                        "completed_at": completed,
                        "pages": job["pages"],
                        "key_version": salted_hmac(PREFIX, "contact-key-version").hexdigest(),
                    },
                    timeout=60 * 60,
                )
                cache.set(
                    STATE, {"status": "complete", "completed_at": completed}, timeout=JOB_SECONDS
                )
                cache.delete(ACTIVE)
                cache.delete(key)
                return "complete"
            job["pages"] += 1
            if job["pages"] >= MAX_PAGES:
                raise ValueError("partial_history")
            job.update(
                cursor=ids[-1], seen=job["seen"] + ids, updated_at=timezone.now().isoformat()
            )
            cache.set(key, job, timeout=JOB_SECONDS)
            cache.set(ACTIVE, generation, timeout=JOB_SECONDS)
            cache.set(STATE, {"status": "running"}, timeout=JOB_SECONDS)
            dispatch(generation, ids[-1])
            return "queued"
        except Exception:
            # Do not log provider responses, exception URLs or contact data.
            fail(generation)
            return "unavailable"
