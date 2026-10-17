"""Guest identity changes are owner-authorised, atomic and auditable."""

import uuid
from contextlib import contextmanager

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.html import strip_tags

from apps.core.phone_numbers import normalize_phone_number
from apps.notifications.models import AuditLog

from .access import require_operations_owner
from .models import ApprovedPhoneChange, CustomerProfile, OperationsManager, VerifiedPhone


@contextmanager
def identity_transaction():
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(190009, 9)")
        yield


def identity_locked(user):
    from apps.loyalty.models import Member

    return bool(
        Member.objects.filter(customer=user).exists()
        or VerifiedPhone.objects.filter(user=user).exists()
        or ApprovedPhoneChange.objects.filter(user=user).exists()
    )


def account_verified(user):
    return bool(
        user.is_active
        and (
            VerifiedPhone.objects.filter(user=user).exists()
            or CustomerProfile.objects.filter(user=user, email_verified_at__isnull=False).exists()
        )
    )


def booking_identity_matches(user, email, phone):
    """A matching email never overrides another member's verified mobile."""
    if not user.is_authenticated or not user.is_active:
        return False
    from apps.core.phone_numbers import InvalidPhoneNumber

    try:
        number = normalize_phone_number(phone)
    except InvalidPhoneNumber:
        number = ""
    owner = VerifiedPhone.objects.filter(phone=number).first() if number else None
    if owner:
        return owner.user_id == user.pk
    if VerifiedPhone.objects.filter(user=user).exists():
        return False
    return bool(user.email and user.email.strip().casefold() == email.strip().casefold())


def can_bind_phone(user, phone):
    if ApprovedPhoneChange.objects.filter(Q(user=user) | Q(phone=phone)).exists():
        return False  # Approved replacements finish through the normal mobile sign-in screen.
    contact = CustomerProfile.objects.filter(user=user).values_list("phone", flat=True).first()
    try:
        return not identity_locked(user) or not contact or normalize_phone_number(contact) == phone
    except ValueError:
        return False


def attach_paid_phone_account(booking):
    """New paid website stays can earn without an email/login detour.

    The existing verified mobile identifies the recipient; this grants no
    browser session or access token. Old bookings keep their recovery policy.
    Called under loyalty's posting lock; identity changes share the inner lock.
    """
    from decimal import Decimal

    from django.db.models import Sum

    from apps.loyalty.launch import participant_allowed, pilot_only, pilot_references
    from apps.loyalty.services import launch_at
    from apps.payments.models import PaymentAttempt
    from apps.reservations.models import BookingIntent, Reservation

    intent = booking.booking_intent
    if (
        not intent
        or intent.customer_id is not None
        or booking.source_type != Reservation.SourceType.DIRECT_WEBSITE
        or booking.normalized_status not in Reservation.ACTIVE_STATUSES
        or booking.created_at < launch_at()
        or (pilot_only() and booking.public_reference not in pilot_references())
    ):
        return False
    try:
        phone = normalize_phone_number(intent.guest_phone)
    except ValueError:
        return False
    with identity_transaction():
        binding = (
            VerifiedPhone.objects.select_related("user")
            .filter(
                phone=phone,
                verified_at__lte=booking.created_at,
                user__is_active=True,
                user__member__status="active",
            )
            .first()
        )
        if not binding or not participant_allowed(binding.user):
            return False
        paid = PaymentAttempt.objects.filter(
            booking_intent=intent,
            status=PaymentAttempt.Status.SUCCEEDED,
            verified_at__lte=timezone.now(),
            currency="SAR",
            amount__gt=0,
        ).exclude(provider_payment_id="").aggregate(total=Sum("amount"))["total"] or Decimal(0)
        if intent.payment_amount_sar <= 0 or paid < intent.payment_amount_sar:
            return False
        changed = BookingIntent.objects.filter(pk=intent.pk, customer__isnull=True).update(
            customer=binding.user
        )
        intent.refresh_from_db(fields=["customer"])
        if changed:
            booking.loyalty_state["identity_match"] = {
                "method": "existing_verified_mobile",
                "customer": binding.user_id,
                "binding": binding.pk,
                "binding_version": str(binding.version),
                "at": timezone.now().isoformat(),
            }
        return bool(changed)


def update_identity(request, user_id, *, email, phone, reason, expected):
    """Never accepts a balance, membership number, inviter or verification claim."""
    require_operations_owner(request.user)
    email = email.strip().casefold()
    validate_email(email)
    phone = normalize_phone_number(phone) if phone.strip() else ""
    if not 5 <= len(reason.strip()) <= 500:
        raise ValidationError("اكتب سبب التعديل وكيف تحققت من هوية صاحب الحساب.")
    with identity_transaction():
        user = get_user_model().objects.select_for_update().get(pk=user_id)
        if (
            user.is_staff
            or user.is_superuser
            or user.email.casefold() == settings.OPERATIONS_OWNER_EMAIL.casefold()
            or OperationsManager.objects.filter(email__iexact=user.email, is_enabled=True).exists()
        ):
            raise ValidationError("هذه الأداة لحسابات الضيوف فقط، وليس حسابات الإدارة.")
        profile, _ = CustomerProfile.objects.select_for_update().get_or_create(user=user)
        binding = VerifiedPhone.objects.filter(user=user).first()
        pending = ApprovedPhoneChange.objects.filter(user=user).first()
        current_phone = pending.phone if pending else binding.phone if binding else profile.phone
        current = [
            user.email,
            current_phone,
            str(binding.version) if binding else "",
            str(pending.version) if pending else "",
        ]
        if current != expected:
            raise ValidationError("تغيرت بيانات الحساب؛ أعد فتح الصفحة ومراجعة التعديل.")
        if (
            email == settings.OPERATIONS_OWNER_EMAIL.casefold()
            or OperationsManager.objects.filter(email__iexact=email, is_enabled=True).exists()
        ):
            raise ValidationError("لا يمكن استخدام بريد مخصص للإدارة في حساب ضيف.")
        if (
            get_user_model()
            .objects.exclude(pk=user.pk)
            .filter(Q(email__iexact=email) | Q(username__iexact=email))
            .exists()
        ):
            raise ValidationError("البريد مرتبط بحساب آخر؛ لا ندمج الحسابات تلقائيًا.")
        from allauth.account.models import EmailAddress

        if EmailAddress.objects.exclude(user=user).filter(email__iexact=email).exists():
            raise ValidationError("البريد مرتبط بحساب آخر.")
        if (
            VerifiedPhone.objects.exclude(user=user).filter(phone=phone).exists()
            or ApprovedPhoneChange.objects.exclude(user=user).filter(phone=phone).exists()
        ):
            raise ValidationError("الجوال مرتبط بحساب آخر أو محجوز لتوثيقه؛ لا يمكن نقله.")
        old_email, old_phone = user.email, current_phone
        if current_phone and not phone:
            raise ValidationError("لا يمكن حذف جوال العضوية؛ أدخل الرقم البديل.")
        email_changed, phone_changed = old_email.casefold() != email, current_phone != phone
        if not email_changed and not phone_changed:
            raise ValidationError("لا يوجد تغيير في الجوال أو البريد.")
        if email_changed:
            user.email = email
            # Reserve the canonical address using Django's unique username too.
            # This closes a race with ordinary email-based account creation.
            user.username = email[:150]
            user.save(update_fields=["email", "username"])
            # No old email token or alternate allauth address remains a login alias.
            profile.email_verified_at = None
            profile.verification_sent_at = None
            profile.verification_sent_for = ""
            EmailAddress.objects.filter(user=user).delete()
            EmailAddress.objects.create(user=user, email=email, verified=False, primary=True)
        if phone_changed:
            VerifiedPhone.objects.filter(user=user).delete()
            ApprovedPhoneChange.objects.filter(user=user).delete()
            ApprovedPhoneChange.objects.create(user=user, phone=phone, approved_by=request.user)
            profile.phone = phone
        profile.save()
        profile.auth_version = uuid.uuid4()
        profile.identity_changed_at = timezone.now()
        profile.save(update_fields=["auth_version", "identity_changed_at", "updated_at"])
        from .models import PhoneChallenge

        PhoneChallenge.objects.filter(user=user, status__in=["pending", "sending", "sent"]).update(
            status="superseded", code_digest=""
        )
        # Dedicated private identity audit, not the general redacted event logger.
        AuditLog.objects.create(
            actor_user=request.user,
            action="guest.identity_changed",
            object_type="guest_account",
            object_reference=str(user.pk),
            summary="عدلت الإدارة بيانات اتصال حساب الضيف",
            metadata={
                "reason": strip_tags(reason.strip()),
                "old_email": old_email,
                "new_email": email,
                "old_phone": old_phone,
                "new_phone": phone,
                "phone_requires_whatsapp_proof": phone_changed,
                "email_requires_proof": email_changed,
            },
        )
        return user
