"""Bearer-authenticated access to the existing unpaid manual-booking workflow."""

import hashlib
import json
import re
import uuid
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt

from apps.notifications.services.audit import record_audit
from apps.notifications.services.ultramsg import send_manual_payment_link_request
from apps.properties.models import Property

from .manual_bookings import (
    create_manual_booking_draft,
    create_manual_booking_in_hostaway,
    finalize_manual_booking_draft,
)
from .models import ManualBookingDraft, Reservation
from .operations_forms import ManualBookingAvailabilityForm, ManualBookingFinalizeForm
from .services.sama_guest_context import canonical_phone, current_guest_context

ACTOR = "sama-booking-agent"


def response(code, status=200, **fields):
    result = JsonResponse({"code": code, **fields}, status=status)
    result["Cache-Control"] = "no-store"
    return result


def authenticated(method):
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def secured(request, *args, **kwargs):
            if not settings.SAMA_BOOKING_ENABLED:
                return response("disabled", 503)
            if not request.is_secure():
                return response("https_required", 403)
            digest = settings.SAMA_BOOKING_KEY_SHA256
            authorization = request.headers.get("Authorization", "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(
                r"Bearer [0-9a-f]{64}", authorization
            ):
                return response("unauthorized", 401)
            supplied = hashlib.sha256(authorization[7:].encode()).hexdigest()
            if not constant_time_compare(digest, supplied):
                return response("unauthorized", 401)
            if request.method != method:
                return response("method_not_allowed", 405)
            if method == "POST" and (
                request.content_type != "application/json" or len(request.body) > 8192
            ):
                return response("invalid_request", 400)
            try:
                return view(request, *args, **kwargs)
            except Exception:
                # Never echo exception URLs, secrets, submitted guest data or provider bodies.
                return response("review_required", 503)

        return secured

    return decorate


def payload(request, fields):
    try:
        value = json.loads(request.body)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) and set(value) == set(fields) else None


def actor():
    user, created = get_user_model().objects.get_or_create(username=ACTOR)
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    if not user.is_active or user.is_staff or user.is_superuser or user.has_usable_password():
        raise ValueError("Unexpected service identity configuration.")
    return user


def draft_for(request_id):
    return (
        ManualBookingDraft.objects.select_related("property", "quote", "created_by")
        .filter(sama_request_id=request_id, created_by__username=ACTOR)
        .first()
    )


def snapshot(draft):
    reservation = Reservation.objects.filter(booking_intent__quote=draft.quote).first()
    return {
        "request_id": str(draft.sama_request_id),
        "reference": draft.public_reference,
        "property_slug": draft.property.slug,
        "check_in": draft.check_in.isoformat(),
        "check_out": draft.check_out.isoformat(),
        "guests": draft.guests,
        "total": format(draft.system_total_price.quantize(Decimal("0.01")), ".2f"),
        "currency": draft.currency,
        "expires_at": draft.expires_at.isoformat(),
        "status": draft.status,
        "confirmation_started": draft.sama_confirmation_started_at is not None,
        "hostaway_created": bool(reservation and reservation.hostaway_reservation_id),
        "accounting_result": draft.sama_accounting_result,
        "reservation_reference": reservation.public_reference if reservation else "",
    }


@authenticated("GET")
def health(request):
    return response(
        "ready",
        actor=ACTOR,
        capabilities=[
            "prepare",
            "confirm_unpaid",
            "request_accounting_payment",
            "status",
            "guest_context",
        ],
    )


@authenticated("POST")
def guest_context(request):
    # Read-only POST keeps the guest number out of URLs and access logs.
    data = payload(request, ("phone",))
    if (
        data is None
        or not isinstance(data["phone"], str)
        or not re.fullmatch(r"\+[1-9][0-9]{7,14}", data["phone"])
        or canonical_phone(data["phone"]) != data["phone"]
    ):
        return response("invalid_request", 400)
    return response(**current_guest_context(data["phone"]))


@authenticated("POST")
def prepare(request):
    data = payload(request, ("request_id", "property_slug", "check_in", "check_out", "guests"))
    if data is None or type(data["guests"]) is not int or not 1 <= data["guests"] <= 20:
        return response("invalid_request", 400)
    try:
        request_id = uuid.UUID(data["request_id"])
    except (ValueError, TypeError, AttributeError):
        return response("invalid_request", 400)
    fingerprint = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = draft_for(request_id)
    if existing:
        if existing.sama_request_fingerprint != fingerprint:
            return response("idempotency_conflict", 409)
        return response("prepared", booking=snapshot(existing))
    property_obj = Property.objects.filter(
        slug=data["property_slug"], is_visible=True, hostaway_is_active=True
    ).first()
    if property_obj is None:
        return response("invalid_property", 400)
    form = ManualBookingAvailabilityForm({**data, "property": property_obj.pk})
    if not form.is_valid():
        return response("invalid_stay", 400)
    if (form.cleaned_data["check_out"] - form.cleaned_data["check_in"]).days > 366:
        return response("invalid_stay", 400)
    request.user = actor()
    try:
        with transaction.atomic():
            result = create_manual_booking_draft(
                property_obj=property_obj,
                check_in=form.cleaned_data["check_in"],
                check_out=form.cleaned_data["check_out"],
                guests=form.cleaned_data["guests"],
                actor=request.user,
            )
            if result.draft is None:
                return response(result.code, 409)
            draft = result.draft
            draft.sama_request_id = request_id
            draft.sama_request_fingerprint = fingerprint
            draft.save(update_fields=["sama_request_id", "sama_request_fingerprint", "updated_at"])
            record_audit(
                request=request,
                action="sama.booking_prepared",
                object_type="ManualBookingDraft",
                object_reference=draft.public_reference,
                summary="Sama prepared a live quote; inventory remains unreserved.",
                metadata={"source": "sama", "status": draft.status},
            )
    except IntegrityError:
        existing = draft_for(request_id)
        if existing and existing.sama_request_fingerprint == fingerprint:
            return response("prepared", booking=snapshot(existing))
        return response("idempotency_conflict", 409)
    return response("prepared", booking=snapshot(draft))


@authenticated("GET")
def status(request, request_id):
    draft = draft_for(request_id)
    return response("status", booking=snapshot(draft)) if draft else response("not_found", 404)


@authenticated("POST")
def confirm(request, request_id):
    data = payload(
        request,
        (
            "guest_first_name",
            "guest_last_name",
            "guest_email",
            "guest_phone",
            "accepted_total",
            "currency",
            "terms_accepted",
        ),
    )
    if (
        data is None
        or data["terms_accepted"] is not True
        or not isinstance(data["accepted_total"], str)
    ):
        return response("guest_confirmation_required", 400)
    with transaction.atomic():
        draft = (
            ManualBookingDraft.objects.select_for_update()
            .select_related("quote")
            .filter(sama_request_id=request_id, created_by__username=ACTOR)
            .first()
        )
        if draft is None:
            return response("not_found", 404)
        if draft.status == ManualBookingDraft.Status.BOOKED_AWAITING_PAYMENT:
            return response("booked_awaiting_payment", booking=snapshot(draft))
        if draft.sama_confirmation_started_at is not None:
            return response("review_required", 409, booking=snapshot(draft))
        total = format(draft.system_total_price.quantize(Decimal("0.01")), ".2f")
        if data["currency"] != draft.currency or data["accepted_total"] != total:
            return response("price_confirmation_required", 409, booking=snapshot(draft))
        form = ManualBookingFinalizeForm({**data, "final_total_price": total}, draft=draft)
        if not form.is_valid():
            return response("invalid_guest_details", 400)
        result = finalize_manual_booking_draft(
            draft_id=draft.pk, guest_data=form.cleaned_data, final_total_price=Decimal(total)
        )
        if result.code != "ready_for_payment":
            return response(result.code, 409)
        draft.sama_confirmation_started_at = timezone.now()
        draft.save(update_fields=["sama_confirmation_started_at", "updated_at"])
    # Persist before the external write. Reconcile uncertain results; never replay them.
    created = create_manual_booking_in_hostaway(draft_id=draft.pk)
    if created.code not in {"created", "already_created"} or created.reservation is None:
        return response("review_required", 409)
    try:
        payment = send_manual_payment_link_request(reservation_id=created.reservation.pk)
        payment_code = payment.code
    except Exception:
        payment_code = "review_required"
    draft.refresh_from_db()
    draft.sama_accounting_result = payment_code
    draft.save(update_fields=["sama_accounting_result", "updated_at"])
    request.user = draft.created_by
    record_audit(
        request=request,
        action="sama.booking_created",
        object_type="ManualBookingDraft",
        object_reference=draft.public_reference,
        summary="Sama created an unpaid stay and requested the accounting payment link.",
        metadata={"source": "sama", "status": payment_code},
    )
    return response("booked_awaiting_payment", booking=snapshot(draft))
