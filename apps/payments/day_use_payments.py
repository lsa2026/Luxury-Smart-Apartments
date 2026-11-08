"""Independent day_use collection. This module never writes to Hostaway."""

from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.core.guest_documents import documents_digest
from apps.properties.models import Property
from apps.reservations.day_use_stays import (
    DayUseStayError,
    active_stays,
    local_inflight_conflict,
    verify_window,
)
from apps.reservations.models import DayUseStay

from .day_use_validation import customer_values, validate_value
from .hyperpay.exceptions import HyperPayCheckoutError, HyperPayError
from .hyperpay.result_codes import HyperPayStatus, map_result_code
from .hyperpay.service import HyperPayService, build_checkout_payload, merchant_transaction_id
from .models import DayUsePaymentAttempt

SALT = "payments.day_use-stay.v1"
UNRESOLVED = ("created", "pending", "unknown", "review")


def public_url(stay):
    token = signing.Signer(salt=SALT).sign_object({"stay": str(stay.pk)})
    return settings.SITE_BASE_URL.rstrip("/") + reverse("payments:day_use_payment", args=[token])


def resolve_token(token):
    try:
        data = signing.Signer(salt=SALT).unsign_object(token)
        if not isinstance(data, dict) or set(data) != {"stay"}:
            return None
        return DayUseStay.objects.select_related("property").filter(pk=data["stay"]).first()
    except (signing.BadSignature, ValidationError, ValueError, TypeError):
        return None


def require_payable(stay):
    from apps.reservations.day_use_hostaway import booking_state, hostaway_first
    from apps.reservations.day_use_loyalty import core_delivered

    if hostaway_first(stay) and booking_state(stay).get("state") != "confirmed":
        raise HyperPayCheckoutError("day_use_booking_not_confirmed")
    if not settings.DAY_USE08_ENABLED or stay.ends_at <= timezone.now():
        raise HyperPayCheckoutError("day_use_payment_closed")
    if not stay.payment_link_deliveries.filter(
        state__in=["queued", "sending", "accepted", "uncertain", "delivered", "read"]
    ).exists():
        raise HyperPayCheckoutError("day_use_payment_not_released")
    try:
        delivered = core_delivered(stay, stay.communication_revision)
    except ValueError:
        delivered = False
    if not delivered:
        raise HyperPayCheckoutError("day_use_core_not_delivered")
    if not settings.HYPERPAY_ENABLED or stay.environment != settings.HYPERPAY_ENVIRONMENT:
        raise HyperPayCheckoutError("day_use_payment_unavailable")
    # The signed link has no clock expiry. The short allocation hold is separate.
    if stay.status not in {"open", "expired", "processing"} or stay.paid_at:
        raise HyperPayCheckoutError("day_use_payment_closed")


def create_checkout(
    stay_id, *, billing_data, acceptance, customer_data=None, client=None, hostaway=None
):
    stay = DayUseStay.objects.select_related("property").get(pk=stay_id)
    require_payable(stay)

    # Validate even direct service callers before availability IO or a payment claim.
    def validated(stay):
        values = dict(customer_data if customer_data is not None else customer_values(stay))
        values.update(
            {
                k: v
                for k, v in (stay.billing_address or billing_data or {}).items()
                if k.startswith("billing_")
            }
        )
        try:
            return {
                name: validate_value(name, values.get(name))
                for name in (
                    "guest_first_name",
                    "guest_last_name",
                    "guest_email",
                    "billing_street1",
                    "billing_city",
                    "billing_state",
                    "billing_postcode",
                    "billing_country",
                )
            }
        except ValidationError as exc:
            raise HyperPayCheckoutError("day_use_customer_data_invalid") from exc

    validated(stay)
    if acceptance.get("documents_digest") != documents_digest() or acceptance.get("consents") != {
        "terms": True,
        "house_rules": True,
    }:
        raise HyperPayCheckoutError("guest_acceptance_required")
    evidence = verify_window(
        stay.property,
        stay.starts_at,
        stay.ends_at,
        guests=stay.guests,
        client=hostaway,
        allow_started=True,
        allow_elapsed=False,
        linked_stay=stay if (stay.availability_evidence or {}).get("hostaway_day_use") else None,
    )
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=stay.property_id)
        stay = DayUseStay.objects.select_for_update().get(pk=stay_id)
        require_payable(stay)
        if local_inflight_conflict(stay.property, stay.starts_at, stay.ends_at):
            raise DayUseStayError("بدأ طلب حجز آخر متداخل؛ يلزم مراجعة الإدارة.")
        if active_stays(
            stay.property, stay.starts_at, stay.ends_at + timedelta(hours=3), exclude=stay.pk
        ).exists():
            raise DayUseStayError("تعارض الطلب النهاري مع طلب آخر؛ يلزم مراجعة الإدارة.")
        previous = stay.attempts.filter(status__in=(*UNRESOLVED, "succeeded")).first()
        if previous:
            if (
                previous.status == "pending"
                and previous.provider_checkout_id
                and previous.widget_integrity
            ):
                return previous
            raise HyperPayCheckoutError("previous_day_use_collection_unresolved")
        values = validated(stay)
        payer = SimpleNamespace(
            payment_amount_sar=stay.amount_sar,
            **values,
        )
        merchant_id = merchant_transaction_id()
        payload = build_checkout_payload(payer, merchant_id, amount=stay.amount_sar)
        # DayUse fields already match the wire contract; preserve the entered region.
        payload["billing.state"] = values["billing_state"]
        attempt = DayUsePaymentAttempt.objects.create(
            stay=stay,
            merchant_transaction_id=merchant_id,
            amount=stay.amount_sar,
        )
        stay.status = "processing"
        stay.legal_acceptance = acceptance
        stay.availability_evidence = {**(stay.availability_evidence or {}), **evidence}
        stay.availability_checked_at = timezone.now()
        stay.save(
            update_fields=[
                "status",
                "legal_acceptance",
                "availability_evidence",
                "availability_checked_at",
                "updated_at",
            ]
        )
    # The claim commits before a provider POST. A timeout cannot create another charge.
    try:
        with HyperPayService(client=client) as service:
            result = service.client.create_checkout(payload)
            service._validate_checkout_response(result, attempt)
    except Exception as exc:
        DayUsePaymentAttempt.objects.filter(pk=attempt.pk).update(
            status="unknown", failure_code="checkout_outcome_uncertain"
        )
        DayUseStay.objects.filter(pk=stay.pk).update(status="review")
        raise HyperPayCheckoutError("checkout_outcome_uncertain") from exc
    return attempt


def verify_payment(attempt_id, *, client=None):
    attempt = DayUsePaymentAttempt.objects.select_related("stay").get(pk=attempt_id)
    if attempt.stay.environment != settings.HYPERPAY_ENVIRONMENT:
        raise HyperPayCheckoutError("payment_environment_mismatch")
    if attempt.status == "succeeded":
        return attempt
    if not attempt.provider_checkout_id:
        raise HyperPayCheckoutError("checkout_reference_unavailable")
    with HyperPayService(client=client) as service:
        document = service.client.get_checkout_payment(attempt.provider_checkout_id)
    result = document.get("result")
    code = result.get("code", "") if isinstance(result, dict) else ""
    mapped = map_result_code(code)
    mismatch = ""
    if mapped is not HyperPayStatus.PENDING:
        mismatch = HyperPayService._verification_mismatch(attempt, document)
        if mapped is HyperPayStatus.SUCCESS and not document.get("id"):
            mismatch = "payment_id_missing"
    with transaction.atomic():
        stay = DayUseStay.objects.select_for_update().get(pk=attempt.stay_id)
        attempt = DayUsePaymentAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status == "succeeded":
            return attempt
        if mismatch:
            mapped = HyperPayStatus.REVIEW
        attempt.status = {
            HyperPayStatus.SUCCESS: "succeeded",
            HyperPayStatus.PENDING: "pending",
            HyperPayStatus.FAILED: "failed",
            HyperPayStatus.CANCELLED: "cancelled",
            HyperPayStatus.REVIEW: "review",
            HyperPayStatus.UNKNOWN: "unknown",
        }[mapped]
        attempt.failure_code = mismatch
        attempt.provider_result_code = str(code)[:100]
        if mapped is HyperPayStatus.SUCCESS:
            identifier = document["id"]
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 255:
                attempt.status, attempt.failure_code = "review", "payment_id_invalid"
            else:
                from .collection_identity import payment_id_conflict

                if payment_id_conflict(identifier, attempt):
                    attempt.status, attempt.failure_code = "review", "payment_id_already_recorded"
                else:
                    attempt.provider_payment_id = identifier
        attempt.verified_at = timezone.now()
        try:
            with transaction.atomic():
                attempt.save()
        except IntegrityError:
            attempt.provider_payment_id = None
            attempt.status, attempt.failure_code = "review", "payment_reference_conflict"
            attempt.save()
        if hasattr(stay, "offline_receipt"):
            # Historical failed attempts can still receive a late provider response.
            # Preserve manual money; flag a second capture for review, never reopen.
            if attempt.status == "succeeded":
                stay.status = "review"
        elif attempt.status == "succeeded":
            # Payment after checkout stays recorded for review, without promising admission.
            stay.paid_at = attempt.verified_at
            stay.status = (
                "paid"
                if stay.status == "processing" and stay.ends_at > timezone.now()
                else "review"
            )
        elif attempt.status in {"review", "unknown"}:
            stay.status = "review"
        elif attempt.status in {"failed", "cancelled"} and stay.status == "processing":
            stay.status = "open"
        stay.save(update_fields=["status", "paid_at", "updated_at"])
    return attempt


def reconcile_day_uses(limit=3):
    if not settings.HYPERPAY_ENABLED:
        return
    attempts = DayUsePaymentAttempt.objects.filter(
        stay__environment=settings.HYPERPAY_ENVIRONMENT,
        status__in=["created", "pending", "unknown"],
        provider_checkout_id__isnull=False,
    ).order_by("updated_at")[:limit]
    for attempt in attempts:
        try:
            verify_payment(attempt.pk)
        except HyperPayError:
            continue
