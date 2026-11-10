"""Persist only completed provider reads and the five requested history fields."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime
from django.utils.html import strip_tags

from apps.reservations.models import (
    AdminGuestAnchor,
    AdminGuestFile,
    DayUseHostawayOwnership,
    GuestDirectorySnapshot,
    MorningStay,
    Reservation,
)


def identity_key(email, phone):
    return salted_hmac("admin-guest-file.v1", f"{email}:{phone}", algorithm="sha256").hexdigest()


def project_history(row, identity):
    def provider_date(name):
        value = row.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    start, end = provider_date("arrivalDate"), provider_date("departureDate")
    special = str(row.get("source") or "").casefold() in {"lsa_dayuse", "lsa_morningstay"}
    if special or not start or not end or end <= start:
        start = end = None
    total = None
    try:
        if isinstance(row.get("totalPrice"), bool):
            raise InvalidOperation
        amount = Decimal(str(row.get("totalPrice")))
        if amount.is_finite() and 0 <= amount < 10**10:
            total = str(amount.quantize(Decimal("0.0001")))
    except (InvalidOperation, ValueError):
        pass
    currency = row.get("currency") or row.get("currencyCode") or ""
    if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
        currency = ""
    channel = row.get("channelName") or row.get("source") or ""
    channel = strip_tags(channel).strip().casefold()[:100] if isinstance(channel, str) else ""
    channel_id = row.get("channelId")
    if "airbnb" in channel or channel_id == 2018:
        channel = "airbnb"
    elif "bookingcom" in channel or channel in {"booking.com", "booking"} or channel_id == 2005:
        channel = "booking"
    elif "homeaway" in channel or "vrbo" in channel or channel_id in {2002, 2009, 2010}:
        channel = "vrbo"
    elif "expedia" in channel or channel_id == 2007:
        channel = "expedia"
    elif channel in {"direct", "reservation"} or channel_id == 2000:
        channel = "direct"
    else:
        channel = "other"
    return {
        "id": row["id"],
        "email_key": identity[0],
        "phone_key": identity[1],
        "check_in": start.isoformat() if start else None,
        "check_out": end.isoformat() if end else None,
        "nights": (end - start).days if start else None,
        "total": total,
        "currency": currency,
        "channel": channel,
    }


def matched_rows(rows, email, phone):
    return [
        row
        for row in rows
        if (email and row["email_key"] == email) or (phone and row["phone_key"] == phone)
    ]


@transaction.atomic
def save_completed(snapshot):
    from .guest_classification import classify_reference

    checked = parse_datetime(snapshot["completed_at"])
    # Publish the directory, guest files and anchors together. A failed or
    # truncated scan cannot overwrite any previously verified profile.
    GuestDirectorySnapshot.objects.update_or_create(
        pk=1, defaults={"data": snapshot, "completed_at": checked}
    )
    grouped = {}
    for reference, (email, phone) in snapshot["identities"].items():
        if email or phone:
            grouped.setdefault(
                identity_key(email, phone), {"identity": (email, phone), "references": []}
            )["references"].append(int(reference))
    # Existing files remain usable even if their anchor is no longer active.
    for saved in AdminGuestFile.objects.all():
        grouped.setdefault(
            saved.identity_key,
            {
                "identity": (saved.email_key, saved.phone_key),
                "references": list(saved.anchors.values_list("hostaway_reservation_id", flat=True)),
            },
        )
    for key, group in grouped.items():
        email, phone = group["identity"]
        rows = matched_rows(snapshot.get("history", []), email, phone)
        rows.sort(key=lambda row: (row["check_in"] or "", row["id"]), reverse=True)
        history = [
            {k: v for k, v in row.items() if k not in {"email_key", "phone_key"}} for row in rows
        ]
        results = [
            classify_reference(ref, snapshot)
            for ref in group["references"]
            if tuple(snapshot["identities"].get(str(ref), ())) == (email, phone)
        ]
        result = next(
            (value for value in results if value.code == "returning"),
            results[0] if results else None,
        )
        if result is None:
            continue
        defaults = {
            "email_key": email,
            "phone_key": phone,
            "history": history,
            "checked_at": checked,
        }
        if result is not None:
            defaults.update(classification=result.code, basis=result.basis)
        file, _ = AdminGuestFile.objects.update_or_create(identity_key=key, defaults=defaults)
        for reference in group["references"]:
            if snapshot["identities"].get(str(reference)) == [email, phone] or snapshot[
                "identities"
            ].get(str(reference)) == (email, phone):
                AdminGuestAnchor.objects.update_or_create(
                    hostaway_reservation_id=reference, defaults={"guest_file": file}
                )


def load_completed():
    record = GuestDirectorySnapshot.objects.filter(pk=1).only("data").first()
    return record.data if record else None


def history_for_display(file):
    rows = [dict(row) for row in file.history]
    references = [row["id"] for row in rows]
    local = {
        booking.hostaway_reservation_id: booking
        for booking in Reservation.objects.filter(
            hostaway_reservation_id__in=references, is_test=False, booking_intent__isnull=False
        ).select_related("booking_intent")
    }
    day_use = {
        owner.provider_reservation_id: owner.stay
        for owner in DayUseHostawayOwnership.objects.filter(
            provider_reservation_id__in=references
        ).select_related("stay")
    }
    mornings = {
        stay.availability_evidence["hostaway_morning"]["reservation_id"]: stay
        for stay in MorningStay.objects.filter(
            availability_evidence__hostaway_morning__reservation_id__in=references
        )
    }
    for row in rows:
        booking = local.get(row["id"])
        channel = row["channel"].casefold()
        if booking:
            row["manage_id"] = booking.pk
            row["manage_url"] = reverse("notifications:booking_detail", args=[booking.pk])
        row["channel_label"] = (
            "بوكنق"
            if "booking" in channel
            else "إير بي إن بي"
            if "airbnb" in channel
            else "موقعنا"
            if booking and booking.source_type == Reservation.SourceType.DIRECT_WEBSITE
            else "حجز مباشر عبر هوستاوي"
            if channel in {"direct", "reservation"}
            else {"vrbo": "فيربو", "expedia": "إكسبيديا"}.get(channel, "قناة أخرى")
        )
        if row["check_in"]:
            row["check_in"] = date.fromisoformat(row["check_in"])
            row["check_out"] = date.fromisoformat(row["check_out"])
        if row["total"] is not None and row["currency"]:
            row["amount"] = Decimal(row["total"])
        special = day_use.get(row["id"]) or mornings.get(row["id"])
        if special:
            from zoneinfo import ZoneInfo

            row.update(
                check_in=special.starts_at.astimezone(ZoneInfo(special.time_zone)).date(),
                check_out=special.ends_at.astimezone(ZoneInfo(special.time_zone)).date(),
                nights=0,
                channel_label="موقعنا — إقامة بالساعات",
                manage_url=reverse(
                    "notifications:day_use_detail"
                    if row["id"] in day_use
                    else "notifications:morning_detail",
                    args=[special.pk],
                ),
            )
    return rows
