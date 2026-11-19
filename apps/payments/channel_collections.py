"""Private Booking VCC orders. One checkout, no guest/booking/finance side effects."""

import hashlib
import re
import secrets
from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.integrations.hostaway.client import HostawayClient
from apps.properties.models import Property
from apps.reservations.models import Reservation

from .channel_forms import ChannelCollectionForm
from .collection_identity import payment_id_conflict
from .hyperpay.client import HyperPayClient
from .hyperpay.exceptions import HyperPayCheckoutError
from .hyperpay.result_codes import HyperPayStatus, map_result_code
from .hyperpay.service import HyperPayService, build_checkout_payload, merchant_transaction_id
from .models import ChannelCardCollection, PaymentAttempt


def activation_ready():
    return bool(
        settings.BOOKING_VCC_COLLECTION_ENABLED
        and settings.BOOKING_VCC_APPROVAL_REFERENCE
        and settings.BOOKING_VCC_APPROVED_ENVIRONMENT == settings.HYPERPAY_ENVIRONMENT
        and settings.HYPERPAY_ENVIRONMENT in {"test", "production"}
        and re.fullmatch(r"[a-f0-9]{64}", settings.BOOKING_VCC_APPROVED_ENTITY_FINGERPRINT)
        and secrets.compare_digest(
            settings.BOOKING_VCC_APPROVED_ENTITY_FINGERPRINT, _entity_fingerprint()
        )
        and settings.HYPERPAY_ENABLED
        and settings.HYPERPAY_CURRENCY == "SAR"
        and settings.HYPERPAY_PAYMENT_TYPE == "DB"
    )


def _entity_fingerprint():
    return hashlib.sha256(settings.HYPERPAY_ENTITY_ID.encode()).hexdigest()


def _booking(hostaway_id, *, client=None):
    def read(remote):
        data = remote.get_reservation(int(hostaway_id))
        if str(data.reservation_id) != str(hostaway_id):
            raise ValidationError("لم يطابق هوستاوي رقم الحجز المطلوب.")
        if data.channel_id == 2000 or data.source.strip().casefold() not in {
            "booking",
            "booking.com",
            "bookingcom",
        }:
            raise ValidationError("هذا المسار لحجز بوكينج خارجي مثبت؛ لا تغير مصدر الحجز لتجاوزه.")
        if data.status.casefold() not in {"new", "confirmed", "modified"}:
            raise ValidationError("حالة الحجز الخارجي غير مؤهلة؛ يلزم مراجعتها قبل التحصيل.")
        places = list(
            Property.objects.filter(
                Q(hostaway_listing_map_id=data.listing_map_id)
                | Q(hostaway_listing_id=data.listing_map_id)
            )[:2]
        )
        if len(places) != 1:
            raise ValidationError("الشقة غير مرتبطة بشكل فريد؛ راجع ربط هوستاوي.")
        snapshot = {
            "channel": "booking.com",
            "listing": str(data.listing_map_id),
            "check_in": data.check_in.isoformat(),
            "check_out": data.check_out.isoformat(),
            "currency": data.currency,
            "total": str(data.total_price),
        }
        return places[0], snapshot

    if client is not None:
        return read(client)
    with HostawayClient(max_get_attempts=1, timeout=20) as remote:
        return read(remote)


def _local_money(hostaway_id):
    rows = list(
        Reservation.objects.select_for_update().filter(hostaway_reservation_id=int(hostaway_id))[:2]
    )
    if len(rows) > 1:
        raise ValidationError("مرجع الحجز المحلي غير فريد؛ يلزم مراجعته.")
    local = rows[0] if rows else None
    if local and (
        local.booking_intent_id
        or local.source_type
        in {Reservation.SourceType.DIRECT_WEBSITE, Reservation.SourceType.HOSTAWAY_MANUAL}
        or hasattr(local, "offline_payment_receipt")
        or PaymentAttempt.objects.filter(
            Q(booking_intent__reservation=local) | Q(modification_request__reservation=local),
            status__in=["created", "pending", "unknown", "review", "succeeded"],
        ).exists()
    ):
        raise ValidationError("يوجد مسار حجز أو قبض محلي؛ يلزم مصالحة السجل قبل تحصيل قناة آخر.")
    if local and local.payment_status.casefold() in {"paid", "fully_paid", "partially_paid"}:
        # A synced paid label might mean guest->Booking, not Booking->merchant.
        # It is an unresolved conflict requiring evidence, never permission to debit.
        raise ValidationError(
            "حالة السداد المحلية تحتاج مصالحة وإثبات جهة القبض؛ "
            "لا تفترض أنها قبضنا أو تسمح بتحصيل إضافي."
        )
    return local


def _audit(order, actor, action):
    from apps.notifications.services.audit import record_audit

    record_audit(
        action="channel_collection." + action,
        object_type="channel_collection",
        object_reference=str(order.pk),
        summary="إجراء إداري على تحصيل قناة خارجي",
        metadata={"status": order.status},
        request=SimpleNamespace(user=actor, META={}),
    )


def create_collection(data, *, actor, hostaway=None):
    form = ChannelCollectionForm(data)
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    values = form.cleaned_data
    place, snapshot = _booking(values["hostaway_id"], client=hostaway)
    payer = {name: values[name] for name in form._billing_rule_names}
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=place.pk)
        local = _local_money(values["hostaway_id"])
        existing = (
            ChannelCardCollection.objects.select_for_update()
            .filter(hostaway_reservation_id=values["hostaway_id"])
            .first()
        )
        if existing:
            if (
                existing.amount != values["amount"]
                or existing.currency != values["currency"]
                or existing.payer != payer
                or existing.booking_snapshot != snapshot
                or existing.available_on != values["available_on"]
                or existing.charge_before != values["charge_before"]
            ):
                raise ValidationError(
                    "يوجد أمر لهذا الحجز ببيانات مختلفة؛ افتحه للمراجعة، ولا تنشئ تحصيلًا ثانيًا."
                )
            return existing
        order = ChannelCardCollection.objects.create(
            hostaway_reservation_id=values["hostaway_id"],
            property=place,
            reservation=local,
            booking_snapshot=snapshot,
            amount=values["amount"],
            currency=values["currency"],
            available_on=values["available_on"],
            charge_before=values["charge_before"],
            balance_checked_at=timezone.now(),
            payer=payer,
            created_by=actor,
            environment=settings.HYPERPAY_ENVIRONMENT,
            merchant_transaction_id=merchant_transaction_id(),
        )
        _audit(order, actor, "created")
        return order


def refresh_balance(order_id, *, actor, balance_confirmed=False, hostaway=None):
    if balance_confirmed is not True:
        raise ValidationError("يلزم تأكيد المدير الحالي لرصيد البطاقة وعدم تحصيله.")
    initial = ChannelCardCollection.objects.get(pk=order_id)
    place, snapshot = _booking(initial.hostaway_reservation_id, client=hostaway)
    with transaction.atomic():
        order = ChannelCardCollection.objects.select_for_update().get(pk=order_id)
        if order.status != "draft" or order.provider_checkout_id:
            raise ValidationError("بدأت محاولة بوابة؛ استخدم التحقق من نتيجتها بدل تحديث الرصيد.")
        if place.pk != order.property_id or snapshot != order.booking_snapshot:
            raise ValidationError("تغير الحجز الخارجي؛ يلزم مراجعته.")
        _local_money(order.hostaway_reservation_id)
        order.balance_checked_at = timezone.now()
        order.save(update_fields=["balance_checked_at", "updated_at"])
        _audit(order, actor, "balance_reconfirmed")
        return order


def _validate_start(order):
    if not activation_ready():
        raise ValidationError(
            "التحصيل غير مفعّل: ينتظر إثبات قبول حساب التاجر والمصادقة والفوترة من مشروع الدفع."
        )
    if order.environment != settings.HYPERPAY_ENVIRONMENT:
        raise ValidationError("بيئة أمر التحصيل لا تطابق البوابة الحالية.")
    if order.status != "draft" or order.provider_checkout_id:
        raise ValidationError("توجد محاولة سابقة؛ تحقق من نتيجتها ولا تعد الخصم.")
    if not order.available_on <= timezone.localdate() < order.charge_before:
        raise ValidationError("البطاقة خارج نافذة الإتاحة الموثقة.")
    if not timezone.now() - timedelta(minutes=10) <= order.balance_checked_at <= timezone.now():
        raise ValidationError("أعد التحقق من رصيد بوكينج وعدم تحصيله قبل فتح البوابة.")
    if order.currency != "SAR" or order.amount <= 0:
        raise ValidationError("عملة التحصيل أو مبلغه غير مدعومين.")


def start_checkout(order_id, *, actor, client=None, hostaway=None):
    initial = ChannelCardCollection.objects.get(pk=order_id)
    _validate_start(initial)
    place, snapshot = _booking(initial.hostaway_reservation_id, client=hostaway)
    with transaction.atomic():
        Property.objects.select_for_update().get(pk=place.pk)
        order = ChannelCardCollection.objects.select_for_update().get(pk=order_id)
        _validate_start(order)
        if place.pk != order.property_id or snapshot != order.booking_snapshot:
            raise ValidationError("تغير الحجز الخارجي؛ توقف التحصيل للمراجعة.")
        _local_money(order.hostaway_reservation_id)
        # Validate only allowlisted billing fields, never guest defaults or card data.
        form = ChannelCollectionForm(
            {
                **order.payer,
                "hostaway_id": order.hostaway_reservation_id,
                "amount": order.amount,
                "currency": order.currency,
                "available_on": order.available_on,
                "charge_before": order.charge_before,
                "balance_confirmed": True,
                "payer_confirmed": True,
            }
        )
        if not form.is_valid():
            raise ValidationError("راجع بيانات الفوترة المعتمدة لهذه البطاقة.")
        payer = SimpleNamespace(**{k: form.cleaned_data[k] for k in form._billing_rule_names})
        payload = build_checkout_payload(
            payer, order.merchant_transaction_id, amount=order.amount, currency=order.currency
        )
        order.status = "created"
        order.entity_fingerprint = _entity_fingerprint()
        order.approval_reference = settings.BOOKING_VCC_APPROVAL_REFERENCE[:100]
        order.save(
            update_fields=["status", "entity_fingerprint", "approval_reference", "updated_at"]
        )
        _audit(order, actor, "checkout_claimed")
    # The claim commits before the only gateway POST, even on network ambiguity.
    try:
        with HyperPayService(client=client) as service:
            result = service.client.create_checkout(payload)
        checkout_id = result.get("id")
        integrity = result.get("integrity")
        code = (result.get("result") or {}).get("code")
        if (
            not isinstance(checkout_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{8,255}", checkout_id)
            or not isinstance(integrity, str)
            or not re.fullmatch(r"sha(?:256|384|512)-[A-Za-z0-9+/=]+", integrity)
            or map_result_code(code) is not HyperPayStatus.PENDING
        ):
            raise HyperPayCheckoutError("checkout_response_invalid")
        with transaction.atomic():
            order = ChannelCardCollection.objects.select_for_update().get(pk=order.pk)
            order.provider_checkout_id = checkout_id
            order.widget_integrity = integrity
            order.provider_result_code = code
            order.status = "pending"
            order.save(
                update_fields=[
                    "provider_checkout_id",
                    "widget_integrity",
                    "provider_result_code",
                    "status",
                    "updated_at",
                ]
            )
    except Exception as exc:
        ChannelCardCollection.objects.filter(pk=order.pk).update(
            status="unknown", failure_code="checkout_outcome_uncertain"
        )
        raise HyperPayCheckoutError("checkout_outcome_uncertain") from exc
    _audit(order, actor, "checkout_prepared")
    return order


def reconcile(order_id, *, actor, client=None):
    order = ChannelCardCollection.objects.get(pk=order_id)
    if order.status == "succeeded":
        return order
    if (
        order.environment != settings.HYPERPAY_ENVIRONMENT
        or order.entity_fingerprint != _entity_fingerprint()
    ):
        raise ValidationError("تغير حساب أو بيئة البوابة؛ يلزم مصالحة الحساب الأصلي.")
    if not order.provider_checkout_id:
        raise ValidationError(
            "مرجع البوابة غير محسوم؛ لا تنشئ محاولة أخرى. راجع مشروع الدفع بالمرجع الإداري نفسه."
        )
    if client is None:
        with HyperPayClient() as remote:
            document = remote.get_checkout_payment(order.provider_checkout_id)
    else:
        document = client.get_checkout_payment(order.provider_checkout_id)
    result = document.get("result")
    code = result.get("code", "") if isinstance(result, dict) else ""
    mapped = map_result_code(code)
    mismatch = ""
    if mapped is not HyperPayStatus.PENDING:
        mismatch = HyperPayService._verification_mismatch(order, document)
        if document.get("paymentBrand") not in {"VISA", "MASTER"}:
            mismatch = "channel_card_brand_mismatch"
        if mapped is HyperPayStatus.SUCCESS and not re.fullmatch(
            r"[A-Za-z0-9._-]{1,255}", str(document.get("id") or "")
        ):
            mismatch = "payment_id_invalid"
        if document.get("testMode") and order.environment == "production":
            mismatch = "test_payment_in_production"
    with transaction.atomic():
        order = ChannelCardCollection.objects.select_for_update().get(pk=order_id)
        if order.status == "succeeded":
            return order
        order.status = {
            HyperPayStatus.SUCCESS: "succeeded",
            HyperPayStatus.PENDING: "pending",
            HyperPayStatus.FAILED: "failed",
            HyperPayStatus.CANCELLED: "cancelled",
            HyperPayStatus.REVIEW: "review",
            HyperPayStatus.UNKNOWN: "unknown",
        }[mapped]
        order.failure_code = mismatch
        order.provider_result_code = str(code)[:100]
        if mismatch:
            order.status = "review"
        if order.status == "succeeded":
            if payment_id_conflict(document["id"], order):
                order.status, order.failure_code = "review", "payment_id_already_recorded"
            else:
                order.provider_payment_id = document["id"]
        order.verified_at = timezone.now()
        try:
            with transaction.atomic():
                order.save()
        except IntegrityError:
            order.provider_payment_id = None
            order.status, order.failure_code = "review", "payment_reference_conflict"
            order.save()
        _audit(order, actor, "verified")
        return order
