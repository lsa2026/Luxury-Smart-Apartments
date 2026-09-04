"""Synthetic tests for notifications, email, audit, reports, health, and retention."""

from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.admin import SiteSettingAdmin
from apps.core.models import ContactMessage, SiteSetting
from apps.notifications.admin import AuditLogAdmin, EmailDeliveryAdmin
from apps.notifications.checks import email_configuration_check
from apps.notifications.exports import csv_safe
from apps.notifications.models import AuditLog, EmailDelivery, Notification
from apps.notifications.reports import operations_report
from apps.notifications.services.audit import hash_ip, record_audit, sanitize_metadata
from apps.notifications.services.email import (
    MESSAGES,
    SUBJECTS,
    TEMPLATE_GROUPS,
    DjangoEmailProvider,
    EmailMessageRequest,
    EmailProviderError,
    EmailSendResult,
    queue_email,
    recipient_hmac,
    send_queued_email,
)
from apps.notifications.services.events import dispatch_event, handle_contact_created
from apps.notifications.tasks import (
    cleanup_expired_notifications_task,
    process_email_queue_task,
    send_daily_operations_summary_task,
)
from apps.payments.models import PaymentAttempt
from apps.reservations.models import Reservation
from apps.reviews.admin import ReviewAdmin
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


def make_contact(**overrides: object) -> ContactMessage:
    values = {
        "name": "Synthetic Guest",
        "email": "guest@example.invalid",
        "phone": "+966500000000",
        "subject": "Synthetic question",
        "message": "This is synthetic test content only.",
        "language": "ar",
    }
    values.update(overrides)
    return ContactMessage.objects.create(**values)


def make_notification(**overrides: object) -> Notification:
    values = {
        "notification_type": Notification.Type.SYSTEM_WARNING,
        "audience_type": Notification.Audience.ADMIN,
        "title_ar": "اختبار",
        "title_en": "Test",
        "message_ar": "إشعار اصطناعي",
        "message_en": "Synthetic notification",
        "severity": Notification.Severity.INFO,
        "idempotency_key": f"test:{timezone.now().timestamp()}",
    }
    values.update(overrides)
    return Notification.objects.create(**values)


@pytest.fixture
def superuser():
    return get_user_model().objects.create_superuser(
        username="phase9-admin",
        email="admin@example.invalid",
        password="synthetic-pass",
    )


def test_notification_creation() -> None:
    notification = make_notification()
    assert notification.is_read is False
    assert notification.status == Notification.Status.ACTIVE


def test_notification_event_is_idempotent() -> None:
    first = dispatch_event(
        "hostaway_sync.failed",
        event_key="sync:synthetic",
        related_object_type="IntegrationSyncRun",
        related_object_reference="synthetic",
    )
    second = dispatch_event(
        "hostaway_sync.failed",
        event_key="sync:synthetic",
        related_object_type="IntegrationSyncRun",
        related_object_reference="synthetic",
    )
    assert first == second
    assert Notification.objects.count() == 1


def test_unknown_event_is_ignored() -> None:
    assert (
        dispatch_event(
            "unknown.event",
            event_key="unknown",
            related_object_type="Unknown",
            related_object_reference="unknown",
        )
        is None
    )


@pytest.mark.parametrize(
    "event_name",
    [
        "booking_intent.created",
        "booking_intent.price_changed",
        "modification_request.created",
        "reservation.create_unknown",
    ],
)
def test_business_events_create_expected_notification(event_name: str) -> None:
    notification = dispatch_event(
        event_name,
        event_key=f"synthetic:{event_name}",
        related_object_type="Synthetic",
        related_object_reference="public",
    )
    assert notification is not None


def test_secondary_event_failure_does_not_escape() -> None:
    with patch.object(Notification.objects, "get_or_create", side_effect=ValueError):
        result = dispatch_event(
            "hostaway_sync.failed",
            event_key="safe-failure",
            related_object_type="IntegrationSyncRun",
            related_object_reference="safe",
        )
    assert result is None


def test_notification_rejects_external_action_url() -> None:
    notification = make_notification(action_url="https://example.invalid/path")
    with pytest.raises(ValidationError):
        notification.full_clean()


def test_notification_accepts_internal_action_url() -> None:
    notification = make_notification(action_url="/admin/core/contactmessage/1/change/")
    notification.full_clean()


def test_user_notification_requires_recipient() -> None:
    notification = Notification(
        notification_type=Notification.Type.SYSTEM_WARNING,
        audience_type=Notification.Audience.USER,
        title_ar="اختبار",
        title_en="Test",
        message_ar="اختبار",
        message_en="Test",
        idempotency_key="user-without-recipient",
    )
    with pytest.raises(ValidationError):
        notification.full_clean()


def test_admin_notification_center_permissions() -> None:
    response = Client().get(reverse("notifications:center"))
    assert response.status_code == 302


def test_admin_notification_center_http_200(superuser) -> None:
    client = Client()
    client.force_login(superuser)
    make_notification()
    response = client.get(reverse("notifications:center"))
    assert response.status_code == 200
    assert "إشعار اصطناعي" in response.content.decode()


def test_mark_notification_read_is_post_only(superuser) -> None:
    client = Client()
    client.force_login(superuser)
    notification = make_notification()
    response = client.get(reverse("notifications:mark_read", args=[notification.pk]))
    assert response.status_code == 405


def test_mark_notification_read(superuser) -> None:
    client = Client()
    client.force_login(superuser)
    notification = make_notification()
    response = client.post(reverse("notifications:mark_read", args=[notification.pk]))
    notification.refresh_from_db()
    assert response.status_code == 302
    assert notification.is_read is True
    assert notification.read_at is not None


def test_mark_all_notifications_read(superuser) -> None:
    client = Client()
    client.force_login(superuser)
    make_notification(idempotency_key="one")
    make_notification(idempotency_key="two")
    client.post(reverse("notifications:mark_all_read"))
    assert Notification.objects.filter(is_read=False).count() == 0


def test_user_notification_cannot_be_read_by_another_user(superuser) -> None:
    other = get_user_model().objects.create_user(username="notification-owner")
    notification = make_notification(
        audience_type=Notification.Audience.USER,
        recipient_user=other,
    )
    client = Client()
    client.force_login(superuser)
    response = client.post(reverse("notifications:mark_read", args=[notification.pk]))
    assert response.status_code == 404
    notification.refresh_from_db()
    assert notification.is_read is False


def test_notification_mark_read_csrf(superuser) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(superuser)
    notification = make_notification()
    response = client.post(reverse("notifications:mark_read", args=[notification.pk]))
    assert response.status_code == 403


def test_contact_event_contains_no_pii() -> None:
    contact = make_contact()
    handle_contact_created(contact.pk, email=contact.email, language="ar")
    notification = Notification.objects.get()
    combined = f"{notification.title_ar} {notification.message_ar}"
    assert contact.email not in combined
    assert contact.phone not in combined
    assert contact.message not in combined


@override_settings(
    CONTACT_NOTIFICATION_EMAIL_ENABLED=True,
    ADMIN_NOTIFICATION_EMAIL_ENABLED=True,
    OPERATIONS_EMAIL="operations@example.invalid",
    EMAIL_DELIVERY_ENABLED=False,
)
def test_contact_event_queues_customer_and_admin_templates() -> None:
    contact = make_contact()
    handle_contact_created(contact.pk, email=contact.email, language="en")
    assert set(EmailDelivery.objects.values_list("message_type", flat=True)) == {
        "contact_confirmation",
        "contact_admin_alert",
    }
    assert EmailDelivery.objects.filter(status=EmailDelivery.Status.DISABLED).count() == 2


@override_settings(
    EMAIL_DELIVERY_ENABLED=False,
    CONTACT_NOTIFICATION_EMAIL_ENABLED=True,
)
def test_email_disabled_records_no_external_send() -> None:
    contact = make_contact()
    delivery = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="disabled-email",
    )
    assert delivery.status == EmailDelivery.Status.DISABLED
    assert send_queued_email(delivery.pk).code == "email_delivery_disabled"


def test_email_delivery_idempotency() -> None:
    contact = make_contact()
    first = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="contact-email-one",
    )
    second = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="contact-email-one",
    )
    assert first == second
    assert EmailDelivery.objects.count() == 1


@override_settings(EMAIL_DELIVERY_ENABLED=True)
def test_new_email_is_dispatched_immediately_after_commit() -> None:
    contact = make_contact()
    with (
        patch("apps.notifications.services.email.transaction.on_commit") as on_commit,
        patch("apps.notifications.tasks.send_email_delivery_task.delay") as delay,
    ):
        on_commit.side_effect = lambda callback, **_kwargs: callback()
        delivery = queue_email(
            message_type="contact_confirmation",
            recipient=contact.email,
            recipient_source="contact",
            recipient_reference=str(contact.pk),
            language="ar",
            idempotency_key="immediate-email",
        )

    on_commit.assert_called_once()
    delay.assert_called_once_with(str(delivery.pk))


def test_recipient_is_masked_and_hmaced() -> None:
    contact = make_contact()
    delivery = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="masked-email",
    )
    assert delivery.recipient_masked == "g***@example.invalid"
    assert delivery.recipient_hash == recipient_hmac(contact.email)
    assert contact.email not in delivery.recipient_hash


def test_email_delivery_does_not_store_body_or_headers() -> None:
    field_names = {field.name for field in EmailDelivery._meta.fields}
    assert "body" not in field_names
    assert "headers" not in field_names
    assert "recipient" not in field_names


class CountingProvider:
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, request: EmailMessageRequest) -> EmailSendResult:
        self.calls += 1
        assert request.recipient.endswith("@example.invalid")
        return EmailSendResult(True, "sent", "synthetic-provider-id")


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
)
def test_sent_delivery_is_not_resent() -> None:
    contact = make_contact()
    delivery = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="send-once",
    )
    provider = CountingProvider()
    assert send_queued_email(delivery.pk, provider=provider).sent is True
    assert send_queued_email(delivery.pk, provider=provider).code == "already_sent"
    assert provider.calls == 1


class FailingProvider:
    name = "failing"

    def __init__(self, *, permanent: bool) -> None:
        self.permanent = permanent

    def send(self, request: EmailMessageRequest) -> EmailSendResult:
        del request
        raise EmailProviderError("synthetic_failure", permanent=self.permanent)


@pytest.mark.parametrize(
    ("permanent", "prefix"),
    [(True, "permanent_"), (False, "transient_")],
)
@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
)
def test_email_failure_classification(permanent: bool, prefix: str) -> None:
    contact = make_contact()
    delivery = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key=f"failure:{permanent}",
    )
    result = send_queued_email(delivery.pk, provider=FailingProvider(permanent=permanent))
    delivery.refresh_from_db()
    assert result.sent is False
    assert delivery.status == EmailDelivery.Status.FAILED
    assert delivery.last_error_code.startswith(prefix)


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
)
def test_email_timeout_is_transient() -> None:
    contact = make_contact()
    delivery = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="timeout",
    )

    class TimeoutProvider:
        def send(self, request: EmailMessageRequest) -> EmailSendResult:
            del request
            raise EmailProviderError("timeout")

    send_queued_email(delivery.pk, provider=TimeoutProvider())
    delivery.refresh_from_db()
    assert delivery.last_error_code == "transient_timeout"


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
)
def test_already_sending_is_not_claimed_twice() -> None:
    contact = make_contact()
    delivery = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="already-sending",
    )
    delivery.status = EmailDelivery.Status.SENDING
    delivery.save(update_fields=["status"])
    assert send_queued_email(delivery.pk).code == "already_sending"


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
    EMAIL_MAX_RETRIES=1,
)
def test_email_retry_limit() -> None:
    contact = make_contact()
    delivery = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="retry-limit",
    )
    delivery.attempt_count = 1
    delivery.save(update_fields=["attempt_count"])
    assert send_queued_email(delivery.pk).code == "retry_limit_reached"


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
)
def test_django_provider_html_escapes_and_has_plain_text() -> None:
    provider = DjangoEmailProvider()
    provider.send(
        EmailMessageRequest(
            recipient="guest@example.invalid",
            subject="Synthetic",
            template_name="contact",
            language="en",
            context={
                "heading": "Synthetic",
                "message": "<script>alert(1)</script>",
                "brand_name": "Luxury Smart Apartments",
                "site_base_url": "https://example.invalid",
            },
        )
    )
    message = mail.outbox[0]
    assert "<script>" not in message.alternatives[0].content
    assert "&lt;script&gt;" in message.alternatives[0].content
    assert "Synthetic" in message.body


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
)
def test_console_backend_is_supported(capsys) -> None:
    result = DjangoEmailProvider().send(
        EmailMessageRequest(
            recipient="guest@example.invalid",
            subject="Console synthetic",
            template_name="contact",
            language="en",
            context={"heading": "Console", "message": "Synthetic only"},
        )
    )
    assert result.sent is True
    assert "Console synthetic" in capsys.readouterr().out


@pytest.mark.parametrize("message_type", sorted(SUBJECTS))
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_all_trilingual_email_messages_are_defined(message_type: str, language: str) -> None:
    assert SUBJECTS[message_type][language]
    assert MESSAGES[message_type][language]
    assert TEMPLATE_GROUPS[message_type] in {
        "contact",
        "booking",
        "modification",
        "reservation",
        "operations",
        "account",
    }


@override_settings(EMAIL_DELIVERY_ENABLED=False)
def test_email_queue_task_disabled() -> None:
    assert process_email_queue_task()["status"] == "disabled"


@override_settings(
    EMAIL_DELIVERY_ENABLED=True,
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
)
def test_celery_email_task_requests_limited_retry() -> None:
    from apps.notifications.tasks import send_email_delivery_task

    contact = make_contact()
    delivery = queue_email(
        message_type="contact_confirmation",
        recipient=contact.email,
        recipient_source="contact",
        recipient_reference=str(contact.pk),
        language="ar",
        idempotency_key="celery-retry",
    )
    delivery.status = EmailDelivery.Status.FAILED
    delivery.last_error_code = "transient_timeout"
    delivery.save(update_fields=["status", "last_error_code"])
    with (
        patch(
            "apps.notifications.tasks.send_queued_email",
            return_value=EmailSendResult(False, "timeout"),
        ),
        patch.object(
            send_email_delivery_task,
            "retry",
            side_effect=RuntimeError("retry-requested"),
        ),
        pytest.raises(RuntimeError, match="retry-requested"),
    ):
        send_email_delivery_task.run(str(delivery.pk))


@override_settings(
    ADMIN_NOTIFICATION_EMAIL_ENABLED=False,
    OPERATIONS_EMAIL="",
)
def test_daily_summary_disabled() -> None:
    assert send_daily_operations_summary_task()["status"] == "disabled"


def test_cleanup_expired_notifications() -> None:
    notification = make_notification(expires_at=timezone.now() - timedelta(seconds=1))
    result = cleanup_expired_notifications_task()
    notification.refresh_from_db()
    assert result["archived"] == 1
    assert notification.status == Notification.Status.ARCHIVED


def test_email_check_rejects_tls_ssl_combination() -> None:
    with override_settings(EMAIL_USE_TLS=True, EMAIL_USE_SSL=True):
        ids = {error.id for error in email_configuration_check()}
    assert "notifications.E001" in ids


def test_email_check_requires_sender_when_enabled() -> None:
    with override_settings(EMAIL_DELIVERY_ENABLED=True, DEFAULT_FROM_EMAIL=""):
        ids = {error.id for error in email_configuration_check()}
    assert "notifications.E002" in ids


def test_email_check_requires_smtp_host() -> None:
    with override_settings(
        EMAIL_DELIVERY_ENABLED=True,
        DEFAULT_FROM_EMAIL="noreply@example.invalid",
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="",
    ):
        ids = {error.id for error in email_configuration_check()}
    assert "notifications.E003" in ids


def test_audit_log_creation_and_ip_hash() -> None:
    request = RequestFactory().post("/admin/")
    request.user = SimpleNamespace(is_authenticated=False)
    request.META["REMOTE_ADDR"] = "192.0.2.10"
    audit = record_audit(
        request=request,
        action="test.created",
        object_type="Synthetic",
        object_reference="public",
        summary="<b>Synthetic</b>",
        metadata={"count": 1, "email": "must-not-store@example.invalid"},
    )
    assert audit.summary == "Synthetic"
    assert audit.ip_hash == hash_ip("192.0.2.10")
    assert audit.metadata == {"count": 1}
    assert "192.0.2.10" not in audit.ip_hash


def test_audit_metadata_allowlist() -> None:
    assert sanitize_metadata({"count": 2, "fields": ["is_visible"], "password": "forbidden"}) == {
        "count": 2,
        "fields": ["is_visible"],
    }


def test_audit_log_is_immutable() -> None:
    audit = AuditLog.objects.create(
        action="test",
        object_type="Synthetic",
        object_reference="public",
        summary="Synthetic",
    )
    audit.summary = "Changed"
    with pytest.raises(ValidationError):
        audit.save()
    with pytest.raises(ValidationError):
        audit.delete()


def test_audit_admin_is_read_only(superuser) -> None:
    model_admin = AuditLogAdmin(AuditLog, admin.site)
    request = RequestFactory().get("/admin/")
    request.user = superuser
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_admin_site_setting_change_creates_audit(superuser) -> None:
    setting = SiteSetting.objects.create(site_name="Luxury Smart Apartments")
    request = RequestFactory().post("/admin/core/sitesetting/")
    request.user = superuser
    form = SimpleNamespace(changed_data=["footer_text_ar"])
    SiteSettingAdmin(SiteSetting, admin.site).save_model(request, setting, form, True)
    assert AuditLog.objects.filter(action="site_settings.changed").exists()


def test_review_visibility_action_creates_audit(superuser) -> None:
    request = RequestFactory().post("/admin/reviews/review/")
    request.user = superuser
    queryset = Mock()
    queryset.update.return_value = 2
    model_admin = ReviewAdmin(Review, admin.site)
    model_admin.message_user = Mock()
    model_admin.make_hidden(request, queryset)
    audit = AuditLog.objects.get(action="review.visibility_changed")
    assert audit.metadata == {"count": 2, "status": "hidden"}


def test_email_admin_is_read_only(superuser) -> None:
    model_admin = EmailDeliveryAdmin(EmailDelivery, admin.site)
    request = RequestFactory().get("/admin/")
    request.user = superuser
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_dashboard_permissions() -> None:
    response = Client().get(reverse("notifications:dashboard"))
    assert response.status_code == 302


def test_dashboard_http_200_and_uses_request_value_label(superuser) -> None:
    client = Client()
    client.force_login(superuser)
    with patch(
        "apps.integrations.hostaway.client.HostawayClient.__init__",
        side_effect=AssertionError("Hostaway must not be called"),
    ):
        response = client.get(reverse("notifications:dashboard"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "قيمة طلبات الحجز" in content
    assert "ليست إيرادات" in content


def test_dashboard_aggregate_values() -> None:
    cache.clear()
    make_contact()
    make_notification()
    today = timezone.localdate()
    report = operations_report(today, today)
    assert report["new_contacts"] == 1
    assert report["notification_unread"] == 1
    assert report["properties_total"] == 0


@pytest.mark.parametrize(
    "query",
    ["?period=today", "?period=7", "?period=30", "?period=custom&start=2026-01-01&end=2026-01-31"],
)
def test_dashboard_date_filters(superuser, query: str) -> None:
    client = Client()
    client.force_login(superuser)
    assert client.get(reverse("notifications:dashboard") + query).status_code == 200


def test_csv_injection_protection() -> None:
    for prefix in "=+-@":
        assert csv_safe(f"{prefix}formula").startswith("'")
    assert csv_safe("normal") == "normal"


def test_csv_export_utf8_bom_and_audit(superuser) -> None:
    client = Client()
    client.force_login(superuser)
    response = client.post(reverse("notifications:export", args=["properties"]))
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert AuditLog.objects.filter(action="report.exported").exists()


def test_contact_csv_hides_pii(superuser) -> None:
    contact = make_contact(
        email="private@example.invalid",
        phone="+966511111111",
        message="Private synthetic text",
    )
    client = Client()
    client.force_login(superuser)
    content = client.post(reverse("notifications:export", args=["contacts"])).content.decode(
        "utf-8-sig"
    )
    assert contact.email not in content
    assert contact.phone not in content
    assert contact.message not in content


def test_csv_export_csrf(superuser) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(superuser)
    response = client.post(reverse("notifications:export", args=["properties"]))
    assert response.status_code == 403


def test_health_live() -> None:
    response = Client().get(reverse("notifications:health_live"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready() -> None:
    response = Client().get(reverse("notifications:health_ready"))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
def test_health_ready_fails_when_cache_unavailable() -> None:
    response = Client().get(reverse("notifications:health_ready"))
    assert response.status_code == 503
    assert response.json()["checks"]["cache"] == "unavailable"


@override_settings(REDIS_URL="redis://127.0.0.1:6379/0")
def test_health_ready_checks_configured_redis() -> None:
    redis_client = Mock()
    redis_client.ping.return_value = True
    with patch("redis.Redis.from_url", return_value=redis_client):
        response = Client().get(reverse("notifications:health_ready"))
    assert response.status_code == 200
    assert response.json()["checks"]["redis"] == "ok"


@override_settings(REDIS_URL="redis://127.0.0.1:6379/0")
def test_health_ready_reports_redis_failure_without_details() -> None:
    with patch("redis.Redis.from_url", side_effect=OSError):
        response = Client().get(reverse("notifications:health_ready"))
    assert response.status_code == 503
    assert response.json()["checks"]["redis"] == "unavailable"


def test_health_response_contains_no_secrets() -> None:
    content = Client().get(reverse("notifications:health_ready")).content.decode()
    assert "DATABASE_URL" not in content
    assert "REDIS_URL" not in content
    assert "HOSTAWAY_API_SECRET" not in content


def test_system_status_permissions() -> None:
    assert Client().get(reverse("notifications:system_status")).status_code == 302


def test_system_status_http_200_without_secrets(superuser) -> None:
    client = Client()
    client.force_login(superuser)
    content = client.get(reverse("notifications:system_status")).content.decode()
    assert "DATABASE_URL" not in content
    assert "HOSTAWAY_API_SECRET" not in content
    assert "Access Token" not in content


def test_retention_dry_run_does_not_delete() -> None:
    notification = make_notification()
    Notification.objects.filter(pk=notification.pk).update(
        created_at=timezone.now() - timedelta(days=200)
    )
    output = StringIO()
    call_command(
        "purge_expired_operational_data",
        "--dry-run",
        "--model",
        "notifications",
        stdout=output,
    )
    assert Notification.objects.filter(pk=notification.pk).exists()
    assert "notifications=1" in output.getvalue()


def test_retention_deletes_only_selected_expired_model() -> None:
    notification = make_notification()
    contact = make_contact()
    Notification.objects.filter(pk=notification.pk).update(
        created_at=timezone.now() - timedelta(days=200)
    )
    ContactMessage.objects.filter(pk=contact.pk).update(
        created_at=timezone.now() - timedelta(days=400)
    )
    call_command("purge_expired_operational_data", "--model", "notifications")
    assert not Notification.objects.filter(pk=notification.pk).exists()
    assert ContactMessage.objects.filter(pk=contact.pk).exists()


def test_retention_never_touches_reservation_or_payment_tables() -> None:
    before = (Reservation.objects.count(), PaymentAttempt.objects.count())
    call_command("purge_expired_operational_data", "--model", "all", "--dry-run")
    after = (Reservation.objects.count(), PaymentAttempt.objects.count())
    assert before == after == (0, 0)


def test_site_setting_empty_contact_is_hidden() -> None:
    SiteSetting.objects.all().delete()
    SiteSetting.objects.create(site_name="Luxury Smart Apartments")
    content = Client().get(reverse("core:home")).content.decode()
    assert "Final contact details are being prepared." not in content


def test_contact_form_dispatches_event(django_capture_on_commit_callbacks) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        response = Client().post(
            reverse("core:contact"),
            {
                "name": "Synthetic Guest",
                "email": "guest@example.invalid",
                "phone": "",
                "subject": "Synthetic contact",
                "message": "Synthetic message long enough.",
                "website": "",
            },
        )
    assert response.status_code == 302
    assert Notification.objects.filter(
        notification_type=Notification.Type.CONTACT_MESSAGE_RECEIVED
    ).exists()


def test_no_reservation_or_payment_is_created_by_notifications() -> None:
    dispatch_event(
        "reservation.create_unknown",
        event_key="unknown-reservation-synthetic",
        related_object_type="Reservation",
        related_object_reference="public-reference",
    )
    assert Reservation.objects.count() == 0
    assert PaymentAttempt.objects.count() == 0
