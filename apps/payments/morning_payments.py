"""Independent morning collection. This module never writes to Hostaway."""

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
from apps.reservations.models import MorningStay
from apps.reservations.morning_stays import (
    MorningStayError,
    active_stays,
    local_inflight_conflict,
    verify_window,
)

from .hyperpay.exceptions import HyperPayCheckoutError, HyperPayError
from .hyperpay.result_codes import HyperPayStatus, map_result_code
from .hyperpay.service import HyperPayService, build_checkout_payload, merchant_transaction_id
from .models import MorningPaymentAttempt, PaymentAttempt

SALT = "payments.morning-stay.v1"
UNRESOLVED = ("created", "pending", "unknown", "review")


def public_url(stay):
    token = signing.Signer(salt=SALT).sign_object({"stay": str(stay.pk)})
    return settings.SITE_BASE_URL.rstrip("/") + reverse("payments:morning_payment", args=[token])


def resolve_token(token):
    try:
        data = signing.Signer(salt=SALT).unsign_object(token)
        if not isinstance(data, dict) or set(data) != {"stay"}:
            return None
        return MorningStay.objects.select_related("property").filter(pk=data["stay"]).first()
    except (signing.BadSignature, ValidationError, ValueError, TypeError):
        return None


def require_payable(stay):
    if not settings.HYPERPAY_ENABLED or stay.environment != settings.HYPERPAY_ENVIRONMENT:
        raise HyperPayCheckoutError("morning_payment_unavailable")
    if stay.status not in {"open", "processing"} or (
        stay.status == "open" and stay.expires_at <= timezone.now()
    ):
        raise HyperPayCheckoutError("morning_payment_closed")
    if stay.starts_at <= timezone.now():
        raise HyperPayCheckoutError("morning_arrival_started")


def create_checkout(stay_id, *, billing_data, acceptance, client=None, hostaway=None):
    stay = MorningStay.objects.select_related("property").get(pk=stay_id)
    require_payable(stay)
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
    )
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=stay.property_id)
        stay = MorningStay.objects.select_for_update().get(pk=stay_id)
        require_payable(stay)
        if local_inflight_conflict(stay.property, stay.starts_at, stay.ends_at):
            raise MorningStayError("بدأ طلب حجز آخر متداخل؛ يلزم مراجعة الإدارة.")
        if active_stays(
            stay.property, stay.starts_at, stay.ends_at + timedelta(hours=3), exclude=stay.pk
        ).exists():
            raise MorningStayError("تعارض الطلب الصباحي مع طلب آخر؛ يلزم مراجعة الإدارة.")
        previous = stay.attempts.filter(status__in=(*UNRESOLVED, "succeeded")).first()
        if previous:
            if (
                previous.status == "pending"
                and previous.provider_checkout_id
                and previous.widget_integrity
            ):
                return previous
            raise HyperPayCheckoutError("previous_morning_collection_unresolved")
        first, _, last = stay.guest_name.partition(" ")
        # Do not fabricate names or billing fields required by 3DS.
        if not last.strip():
            raise HyperPayCheckoutError("guest_full_name_required")
        payer = SimpleNamespace(
            guest_first_name=first,
            guest_last_name=last,
            guest_email=stay.guest_email,
            payment_amount_sar=stay.amount_sar,
            **billing_data,
        )
        merchant_id = merchant_transaction_id()
        payload = build_checkout_payload(payer, merchant_id, amount=stay.amount_sar)
        attempt = MorningPaymentAttempt.objects.create(
            stay=stay,
            merchant_transaction_id=merchant_id,
            amount=stay.amount_sar,
        )
        stay.status = "processing"
        stay.legal_acceptance = acceptance
        stay.availability_evidence = evidence
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
        MorningPaymentAttempt.objects.filter(pk=attempt.pk).update(
            status="unknown", failure_code="checkout_outcome_uncertain"
        )
        MorningStay.objects.filter(pk=stay.pk).update(status="review")
        raise HyperPayCheckoutError("checkout_outcome_uncertain") from exc
    return attempt


def verify_payment(attempt_id, *, client=None):
    attempt = MorningPaymentAttempt.objects.select_related("stay").get(pk=attempt_id)
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
        stay = MorningStay.objects.select_for_update().get(pk=attempt.stay_id)
        attempt = MorningPaymentAttempt.objects.select_for_update().get(pk=attempt_id)
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
                if (
                    MorningPaymentAttempt.objects.filter(provider_payment_id=identifier)
                    .exclude(pk=attempt.pk)
                    .exists()
                    or PaymentAttempt.objects.filter(provider_payment_id=identifier).exists()
                ):
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
        if attempt.status == "succeeded":
            # A late payment stays recorded, but does not silently promise admission.
            stay.paid_at = attempt.verified_at
            stay.status = (
                "paid"
                if stay.status == "processing" and stay.starts_at > timezone.now()
                else "review"
            )
        elif attempt.status in {"review", "unknown"}:
            stay.status = "review"
        elif attempt.status in {"failed", "cancelled"}:
            stay.status = "open" if stay.expires_at > timezone.now() else "expired"
        stay.save(update_fields=["status", "paid_at", "updated_at"])
        if attempt.status == "succeeded":
            from .team_notices import queue_notice

            queue_notice(attempt, morning=True)
    return attempt


def reconcile_mornings(limit=3):
    if not settings.HYPERPAY_ENABLED:
        return
    attempts = MorningPaymentAttempt.objects.filter(
        stay__environment=settings.HYPERPAY_ENVIRONMENT,
        status__in=["created", "pending", "unknown"],
        provider_checkout_id__isnull=False,
    ).order_by("updated_at")[:limit]
    for attempt in attempts:
        try:
            verify_payment(attempt.pk)
        except HyperPayError:
            continue
