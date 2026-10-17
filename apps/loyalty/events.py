"""Append-only guest lifecycle facts, separated from financial postings."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import GuestEvent, Member


def record(
    *,
    customer,
    kind,
    reference,
    source,
    member=None,
    actor=None,
    ledger=None,
    before=None,
    after=None,
    evidence=None,
    reason="",
    occurred_at=None,
):
    if member is None:
        member = Member.objects.filter(customer=customer).first()
    if member is not None and member.customer_id != customer.pk:
        raise ValidationError("حدث العضوية لا يطابق صاحب الحساب.")
    if ledger is not None and (member is None or ledger.member_id != member.pk):
        raise ValidationError("مرجع النقاط لا يطابق حدث العضوية.")
    values = dict(
        customer=customer,
        member=member,
        actor=actor,
        ledger=ledger,
        kind=kind,
        source=source,
        reason=reason,
        before=before or {},
        after=after or {},
        evidence=evidence or {},
    )
    if occurred_at is not None:
        values["occurred_at"] = occurred_at
    event, _ = GuestEvent.objects.get_or_create(reference=reference, defaults=values)
    return event


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="loyalty_guest_created")
def account_created(sender, instance, created, raw=False, **kwargs):
    if raw or not created or instance.is_staff or instance.is_superuser:
        return
    record(
        customer=instance,
        kind=GuestEvent.Kind.ACCOUNT,
        reference=f"account:{instance.pk}",
        source="system",
        occurred_at=instance.date_joined,
        after={"email": instance.email},
        evidence={"source": "account_creation"},
    )


@receiver(post_save, sender="accounts.VerifiedPhone", dispatch_uid="loyalty_phone_verified")
def phone_verified(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    record(
        customer=instance.user,
        kind=GuestEvent.Kind.PHONE,
        reference=f"phone-proof:{instance.version}",
        source="guest",
        actor=instance.user,
        occurred_at=instance.verified_at,
        after={"phone": instance.phone},
    )


@receiver(post_save, sender="notifications.AuditLog", dispatch_uid="loyalty_identity_event")
def identity_changed(sender, instance, created, raw=False, **kwargs):
    if raw or not created or instance.action != "guest.identity_changed":
        return
    from django.contrib.auth import get_user_model

    customer = get_user_model().objects.get(pk=instance.object_reference)
    data = instance.metadata
    record(
        customer=customer,
        kind=GuestEvent.Kind.IDENTITY,
        reference=f"identity:{instance.pk}",
        source="admin",
        actor=instance.actor_user,
        occurred_at=instance.created_at,
        reason=data.get("reason", ""),
        before={"email": data.get("old_email", ""), "phone": data.get("old_phone", "")},
        after={"email": data.get("new_email", ""), "phone": data.get("new_phone", "")},
        evidence={
            "audit_reference": str(instance.pk),
            "phone_requires_proof": bool(data.get("phone_requires_whatsapp_proof")),
            "email_requires_proof": bool(data.get("email_requires_proof")),
        },
    )
