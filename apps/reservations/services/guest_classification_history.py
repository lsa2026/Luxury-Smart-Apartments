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

PREFIX = "lsa:guest-classification-hostaway:v3"
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
        snapshot = _provider_snapshot()
        if snapshot is not None:
            return snapshot
    except Exception:
        pass
    from .guest_files import load_completed

    snapshot = load_completed()
    if (
        isinstance(snapshot, dict)
        and snapshot.get("key_version") == salted_hmac(PREFIX, "contact-key-version").hexdigest()
    ):
        return snapshot
    return None


def _provider_state():
    snapshot = provider_snapshot()
    state = cache.get(STATE) or {}
    if state.get("status") in {"queued", "running"} or state.get("failure_code"):
        return state
    if snapshot is not None and not cache.get(ACTIVE):
        return {"status": "complete", "completed_at": snapshot["completed_at"]}
    state = cache.get(STATE) or {"status": "unavailable"}
    if state.get("status") == "complete":
        return {"status": "unavailable"}
    return {key: state[key] for key in ("status", "completed_at", "failure_code") if key in state}


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
        snapshot = provider_snapshot()
        if not force and snapshot is not None:
            checked = parse_datetime(snapshot["completed_at"])
            if (
                not Reservation.objects.filter(
                    is_test=False,
                    normalized_status__in=Reservation.ACTIVE_STATUSES,
                    hostaway_reservation_id__gt=0,
                    created_at__gt=checked,
                )
                .exclude(hostaway_reservation_id__in=[int(ref) for ref in snapshot["identities"]])
                .exists()
            ):
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
                "identities": {},
                "targets": list(
                    Reservation.objects.filter(
                        is_test=False,
                        normalized_status__in=Reservation.ACTIVE_STATUSES,
                        hostaway_reservation_id__gt=0,
                    ).values_list("hostaway_reservation_id", flat=True)
                ),
                "email": {},
                "phone": {},
                "history": [],
                "updated_at": timezone.now().isoformat(),
            },
            timeout=JOB_SECONDS,
        )
        cache.set(STATE, {"status": "queued"}, timeout=JOB_SECONDS)
        try:
            dispatch(generation, None)
        except Exception:
            fail(generation, "queue_unavailable")
        return provider_state()


def ensure_refresh(*, force=False):
    try:
        return _ensure_refresh(force=force)
    except Exception:
        return {"status": "unavailable"}


def fail(generation, code="source_unavailable"):
    if cache.get(ACTIVE) == generation:
        cache.delete(ACTIVE)
        cache.set(
            STATE,
            {
                "status": "unavailable",
                "failed_at": timezone.now().isoformat(),
                "failure_code": code,
            },
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
            targets = set(job["targets"])
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
                identity = contact_keys(row.get("guestEmail"), row.get("phone"))
                from .guest_files import project_history

                job["history"].append(project_history(row, identity))
                if row["id"] in targets:
                    job["identities"][str(row["id"])] = identity
                for kind, digest in zip(("email", "phone"), identity, strict=True):
                    if digest:
                        references = job[kind].setdefault(digest, [])
                        if row["id"] not in references and len(references) < 2:
                            references.append(row["id"])
            if not rows:
                completed = timezone.now().isoformat()
                snapshot = {
                    "state": "complete",
                    "email": job["email"],
                    "phone": job["phone"],
                    "identities": job["identities"],
                    "history": job["history"],
                    "completed_at": completed,
                    "pages": job["pages"],
                    "key_version": salted_hmac(PREFIX, "contact-key-version").hexdigest(),
                }
                from .guest_files import save_completed

                save_completed(snapshot)
                cache.set(
                    SNAPSHOT,
                    snapshot,
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
        except Exception as exc:
            # Do not log provider responses, exception URLs or contact data.
            allowed = {
                "invalid_page",
                "invalid_cursor",
                "invalid_status",
                "unrecognized_status",
                "invalid_contact",
                "partial_history",
            }
            code = (
                str(exc)
                if type(exc) is ValueError and str(exc) in allowed
                else "source_unavailable"
            )
            fail(generation, code)
            return "unavailable"
