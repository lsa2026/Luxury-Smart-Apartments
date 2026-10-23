import hashlib
from decimal import Decimal
from io import BytesIO
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from PIL import Image

from apps.accounts.models import VerifiedPhone
from apps.loyalty import message_previews
from apps.loyalty import whatsapp_notices as notices
from apps.loyalty.models import GuestEvent, Ledger, Member, MovementNotice
from apps.loyalty.services import withdraw
from apps.notifications.models import AuditLog
from tests import test_loyalty_whatsapp_notices as existing_notices
from tests.test_loyalty_stage8 import join, user

notice_clock = existing_notices.notice_clock
rig = existing_notices.rig

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_all_previews_are_synthetic_single_language_and_have_no_database_writes(
    settings, language, django_assert_num_queries
):
    settings.SITE_BASE_URL = "https://example.invalid"
    settings.LOYALTY_PILOT_ONLY = False
    with django_assert_num_queries(0):
        packets = message_previews.catalogue(language)
    assert len(packets) == 20 and len({key for key, _, _ in packets}) == 20
    for key, caption, content in packets:
        assert len(caption.encode("utf-16-le")) // 2 <= 1024
        assert (
            "example.invalid" in caption
            or key.startswith("auth-")
            or key in {"payment-receipt", "invoice"}
        )
        if key == "invoice":
            assert content.startswith(b"%PDF")
        else:
            assert content.startswith(b"\x89PNG")
            if key not in {"invitation", GuestEvent.Kind.JOIN}:
                assert content == notices.phone_logo()
        if key.startswith("auth-"):
            assert caption.count("[LSA-AUTH]") == 1 and caption.count("000000") == 1
        if language == "fr" and key == "stay":
            assert "28,71" in caption and "25,71" in caption
    assert not Ledger.objects.exists() and not AuditLog.objects.exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_correction_explains_reason_and_negative_balance_without_exposing_booking(rig, language):
    row = Ledger.objects.first()
    row.event_type, row.debit, row.credit = "adjust", Decimal("4.50"), Decimal("0")
    row.member_balance_after = Decimal("-2.00")
    row.reservation_reference = "PRIVATE-BOOKING"
    caption = notices.movement_caption(row, language)
    expected = {
        "ar": ("تعديل الحجز أو إلغائه", "الرصيد السالب", "بطاقتك"),
        "en": ("booking change or cancellation", "negative balance", "card"),
        "fr": ("modification ou annulation", "solde négatif", "carte"),
    }
    assert all(value in caption for value in expected[language])
    assert "PRIVATE-BOOKING" not in caption
    for other in expected.keys() - {language}:
        assert expected[other][0] not in caption


def test_approved_light_horizontal_artwork_is_packaged_unchanged():
    assert hashlib.sha256(notices.phone_logo()).hexdigest() == (
        "eb4d7a6eb5d8bff20c8a187798f6c6e3354db34e1e6cc5869a47a4134d66c7e7"
    )
    with Image.open(BytesIO(notices.phone_logo())) as artwork:
        assert artwork.size == (1920, 450) and artwork.format == "PNG"
        assert artwork.convert("RGB").getpixel((0, 0)) == (255, 255, 255)


def test_new_join_notifies_once_without_adding_points(rig):
    u = user("new-notice")
    VerifiedPhone.objects.create(user=u, phone="+12025550100")
    member = join(u)
    notice = MovementNotice.objects.get(event__member=member)
    assert notice.ledger_id is None
    notices.deliver_movement_notice(notice.pk)
    notices.deliver_movement_notice(notice.pk)
    assert rig.provider.send_image.call_count == 1
    assert "مرحبًا بك" in rig.provider.send_image.call_args.kwargs["caption"]
    assert not Ledger.objects.filter(member=member).exists()


def test_zero_balance_closure_notifies_without_fake_posting(rig):
    withdraw(rig.b, "close-empty")
    notice = MovementNotice.objects.get(event__kind=GuestEvent.Kind.CLOSE)
    notices.deliver_movement_notice(notice.pk)
    assert "تأكيد إلغاء" in rig.provider.send_image.call_args.kwargs["caption"]
    assert not Ledger.objects.filter(member=rig.bm).exists()


def test_welcome_waiting_for_new_phone_resumes_only_its_existing_notice(rig):
    u = user("new-unbound-notice")
    m = join(u)
    notice = MovementNotice.objects.get(event__member=m)
    assert notice.status == "skipped"
    VerifiedPhone.objects.create(user=u, phone="+12025550101")
    notice.refresh_from_db()
    assert notice.status == "queued"
    notices.deliver_movement_notice(notice.pk)
    assert rig.provider.send_image.call_count == 1
    assert MovementNotice.objects.count() == 1


def test_ready_notice_checks_current_eligibility_before_sending(rig, monkeypatch):
    from apps.loyalty.events import record

    record(
        customer=rig.a,
        member=rig.am,
        kind=GuestEvent.Kind.READY,
        reference="synthetic-ready",
        source="system",
    )
    notice = MovementNotice.objects.get(event__kind=GuestEvent.Kind.READY)
    monkeypatch.setattr("apps.loyalty.services.invitation_eligible", lambda member: False)
    notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    assert notice.status == "skipped" and not rig.provider.send_image.called


def test_event_notice_queue_failure_does_not_cancel_join_or_phone_proof(rig, monkeypatch):
    monkeypatch.setattr(notices, "queue_lifecycle", Mock(side_effect=RuntimeError("failure")))
    u = user("resilient-notice")
    m = join(u)
    assert m.status == "active"
    monkeypatch.setattr(notices, "resume_welcome", Mock(side_effect=RuntimeError("failure")))
    binding = VerifiedPhone.objects.create(user=u, phone="+12025550102")
    assert VerifiedPhone.objects.filter(pk=binding.pk).exists()


def test_lifecycle_does_not_notify_history_or_repeat_positive_closure(rig, notice_clock):
    event = GuestEvent.objects.get(member=rig.am, kind=GuestEvent.Kind.JOIN)
    event.source = "history"
    assert notices.queue_lifecycle(event) is None
    notice_clock[0] = notice_clock[0].replace(year=2027)
    withdraw(rig.a, "close-positive")
    assert MovementNotice.objects.count() == 1
    assert MovementNotice.objects.get().ledger.event_type == "withdraw"


def test_join_rollback_has_no_notification(rig):
    count = Member.objects.count()
    with pytest.raises(RuntimeError), transaction.atomic():
        join(user("rolled-back-notice"))
        raise RuntimeError("abort")
    assert Member.objects.count() == count and not MovementNotice.objects.exists()


def test_lifecycle_changed_phone_or_closed_membership_is_not_sent(rig):
    event = GuestEvent.objects.get(member=rig.bm, kind=GuestEvent.Kind.JOIN)
    notice = notices.queue_lifecycle(event)
    rig.bm.status = "withdrawn"
    rig.bm.save(update_fields=["status"])
    notices.deliver_movement_notice(notice.pk)
    notice.refresh_from_db()
    assert notice.status == "skipped" and not rig.provider.send_image.called


def test_notice_requires_exactly_one_business_source():
    with pytest.raises(IntegrityError), transaction.atomic():
        MovementNotice.objects.create(language="ar")


def test_preview_guard_and_once_only_audit_without_financial_changes(rig, monkeypatch):
    monkeypatch.setattr(
        message_previews, "OWNER_PREVIEW_PHONE_SHA256", hashlib.sha256(b"+16465557246").hexdigest()
    )
    rig.bindings[0].phone = "+16465557246"
    rig.bindings[0].save(update_fields=["phone"])
    rig.am.number = "LSA-M000001"
    rig.am.save(update_fields=["number"])
    fake = Mock()
    fake.send_image.return_value = {"id": "fictional-preview"}
    client = Mock()
    client.__enter__ = Mock(return_value=fake)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(message_previews, "UltraMsgClient", Mock(return_value=client))
    before = Ledger.objects.count()
    first = message_previews.send_catalogue(batch="synthetic-review")
    second = message_previews.send_catalogue(batch="synthetic-review")
    assert len(first) == 20 and {v for _, v in first} == {"accepted"}
    assert {v for _, v in second} == {"already_attempted"}
    assert fake.send_image.call_count == 19 and fake.send_document.call_count == 1
    assert Ledger.objects.count() == before
    assert "000000" not in str(list(AuditLog.objects.values_list("metadata", flat=True)))
    # Same suffix is NOT the same destination.
    rig.bindings[0].phone = "+12025557246"
    rig.bindings[0].save(update_fields=["phone"])
    with pytest.raises(ValidationError):
        message_previews.send_catalogue(batch="wrong-destination")


def test_preview_unknown_outcome_stops_and_never_retries(rig, monkeypatch):
    monkeypatch.setattr(
        message_previews, "OWNER_PREVIEW_PHONE_SHA256", hashlib.sha256(b"+16465557246").hexdigest()
    )
    rig.bindings[0].phone = "+16465557246"
    rig.bindings[0].save(update_fields=["phone"])
    rig.am.number = "LSA-M000001"
    rig.am.save(update_fields=["number"])
    client = Mock()
    provider = Mock()
    provider.send_image.side_effect = TimeoutError()
    client.__enter__ = Mock(return_value=provider)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(message_previews, "UltraMsgClient", Mock(return_value=client))
    result = message_previews.send_catalogue(batch="uncertain-review")
    assert result == [("auth-bind", "uncertain")]
    assert AuditLog.objects.filter(action="whatsapp.preview.uncertain").count() == 1


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_booking_invitation_sample_sends_one_figma_card_only_to_pinned_owner(
    rig, monkeypatch, language
):
    from apps.loyalty.booking_invitations import invitation_caption
    from apps.loyalty.invitation_design import invitation_image

    monkeypatch.setattr(
        message_previews, "OWNER_PREVIEW_PHONE_SHA256", hashlib.sha256(b"+16465557246").hexdigest()
    )
    rig.bindings[0].phone = "+16465557246"
    rig.bindings[0].save(update_fields=["phone"])
    rig.am.number = "LSA-M000001"
    rig.am.save(update_fields=["number"])
    provider = Mock()
    provider.send_image.return_value = {"sent": True, "id": "synthetic-only"}
    provider.__enter__ = Mock(return_value=provider)
    provider.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(message_previews, "UltraMsgClient", Mock(return_value=provider))
    before = (Ledger.objects.count(), Member.objects.count())
    assert message_previews.send_catalogue(
        batch="booking-sample", language=language, kind="booking-invitation"
    ) == [("booking-invitation", "accepted")]
    assert message_previews.send_catalogue(
        batch="booking-sample", language=language, kind="booking-invitation"
    ) == [("booking-invitation", "already_attempted")]
    packet = provider.send_image.call_args.kwargs
    assert packet["recipient"] == "+16465557246"
    assert packet["content"] == invitation_image(language)
    assert packet["caption"].endswith(
        invitation_caption(language, "https://example.invalid/my-bookings/rewards/")
    )
    assert len(packet["caption"].encode("utf-16-le")) // 2 <= 1024
    provider.send_image.assert_called_once()
    assert not provider.send_document.called
    assert before == (Ledger.objects.count(), Member.objects.count())
