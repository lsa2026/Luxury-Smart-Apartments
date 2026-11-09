"""Contact inbox navigation and private email links using synthetic local records."""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils.html import escape

from apps.accounts.models import OperationsManager
from apps.core.models import ContactMessage
from apps.notifications.models import EmailDelivery, Notification
from apps.notifications.services.email import build_guest_email, send_queued_email
from apps.notifications.services.events import handle_contact_created
from tests.test_operations_users import login_manager
from tests.test_phase9_notifications import make_contact

pytestmark = pytest.mark.django_db
OWNER = "contact-owner@example.invalid"


@pytest.fixture
def owner_client(settings):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = True
    settings.OPERATIONS_OWNER_EMAIL = OWNER
    owner = get_user_model().objects.create_superuser(
        username=OWNER, email=OWNER, password="synthetic-only"
    )
    client = Client()
    client.force_login(owner)
    return client


def test_owner_can_find_existing_contacts_and_read_full_message(owner_client):
    contact = make_contact()
    for status in ("in_progress", "closed", "spam"):
        make_contact(status=status)
    with patch(
        "apps.integrations.hostaway.client.HostawayClient.__init__",
        side_effect=AssertionError("Contact inbox must use local records only"),
    ):
        home = owner_client.get(reverse("notifications:hub"))
        inbox = owner_client.get(reverse("admin:core_contactmessage_changelist"))
        detail = owner_client.get(reverse("admin:core_contactmessage_change", args=[contact.pk]))
    assert home.status_code == inbox.status_code == detail.status_code == 200
    assert home.context["new_contact_count"] == 1
    assert 'href="' + reverse("admin:core_contactmessage_changelist") + '"' in home.content.decode()
    assert "رسائل التواصل" in home.content.decode()
    assert contact.subject in inbox.content.decode()
    for value in (contact.name, contact.email, contact.phone, contact.message):
        assert value in detail.content.decode()
    # GET navigation neither marks messages handled nor sends a notification.
    contact.refresh_from_db()
    assert contact.status == ContactMessage.Status.READ
    assert not EmailDelivery.objects.exists() and not Notification.objects.exists()
    owner_client.post(
        reverse("admin:core_contactmessage_change", args=[contact.pk]),
        {"status": ContactMessage.Status.IN_PROGRESS, "_save": "Save"},
    )
    assert owner_client.get(reverse("notifications:hub")).context["new_contact_count"] == 0


def test_empty_contact_inbox_is_visible_with_zero_count(owner_client):
    page = owner_client.get(reverse("notifications:hub"))
    assert page.context["new_contact_count"] == 0
    assert "رسائل التواصل" in page.content.decode()


def test_contacts_remain_private_and_hidden_from_scoped_managers(settings):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = True
    settings.OPERATIONS_OWNER_EMAIL = OWNER
    contact = make_contact()
    inbox = reverse("admin:core_contactmessage_changelist")
    detail = reverse("admin:core_contactmessage_change", args=[contact.pk])
    for path in (inbox, detail):
        response = Client().get(path)
        assert response.status_code == 302 and "/admin/login/" in response.url
        assert contact.message not in response.content.decode()
    OperationsManager.objects.create(email="synthetic.lsa.manager@gmail.com")
    _, manager = login_manager()
    page = manager.get(reverse("notifications:hub"))
    assert page.status_code == 200
    assert not page.context["can_view_contacts"]
    assert page.context["new_contact_count"] == 0
    assert inbox not in page.content.decode()
    assert "رسائل التواصل" not in page.content.decode()
    assert manager.get(detail).status_code == 403


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("legacy", [False, True])
def test_contact_alert_includes_message_only_for_operations_and_links_to_private_record(
    settings, owner_client, language, legacy
):
    settings.NOTIFICATIONS_ENABLED = True
    settings.CONTACT_NOTIFICATION_EMAIL_ENABLED = True
    settings.ADMIN_NOTIFICATION_EMAIL_ENABLED = True
    settings.OPERATIONS_EMAIL = "operations@example.invalid"
    settings.EMAIL_DELIVERY_ENABLED = True
    settings.SITE_BASE_URL = "https://luxurysmartapartments.com/"
    contact = make_contact()
    with patch("apps.notifications.services.email._dispatch_email_delivery") as dispatch:
        handle_contact_created(contact.pk, email=contact.email, language=language)
        handle_contact_created(contact.pk, email=contact.email, language=language)
    assert not dispatch.called  # callbacks wait for the test transaction
    assert EmailDelivery.objects.count() == 2 and Notification.objects.count() == 1
    delivery = EmailDelivery.objects.get(message_type="contact_admin_alert")
    assert delivery.recipient_reference == str(contact.pk)
    delivery.language = language
    if legacy:
        delivery.recipient_reference = "operations"
    delivery.save()
    path = (
        reverse("admin:core_contactmessage_changelist")
        if legacy
        else reverse("admin:core_contactmessage_change", args=[contact.pk])
    )
    with patch("apps.notifications.services.email.DjangoEmailProvider.send") as send:
        from apps.notifications.services.email import EmailSendResult

        send.return_value = EmailSendResult(True, "sent")
        assert send_queued_email(delivery.pk).sent
        request = send.call_args.args[0]
        assert request.recipient == settings.OPERATIONS_EMAIL
        assert request.context["action_url"] == settings.SITE_BASE_URL.rstrip("/") + path
        rendered = build_guest_email(request)
        html = rendered.alternatives[0].content
        assert (
            request.context["action_url"] in html and request.context["action_url"] in rendered.body
        )
        for value in (contact.name, contact.email, contact.phone, contact.message):
            if legacy:
                assert value not in html and value not in rendered.body
            else:
                assert value in html and value in rendered.body
        if not legacy:
            assert contact.subject in html and contact.subject in rendered.body
        assert owner_client.get(path).status_code == 200
        assert send_queued_email(delivery.pk).code == "already_sent"
        send.assert_called_once()
    confirmation = EmailDelivery.objects.get(message_type="contact_confirmation")
    assert confirmation.recipient_source == "contact"
    assert confirmation.recipient_reference == str(contact.pk)
    with patch("apps.notifications.services.email.DjangoEmailProvider.send") as send:
        send.return_value = EmailSendResult(True, "sent")
        assert send_queued_email(confirmation.pk).sent
        request = send.call_args.args[0]
        assert request.recipient == contact.email
        assert "contact_message" not in request.context
        rendered = build_guest_email(request)
        assert contact.message not in rendered.body
        assert contact.message not in rendered.alternatives[0].content


def test_contact_alert_escapes_html_and_preserves_full_multiline_message(settings, owner_client):
    settings.OPERATIONS_EMAIL = "operations@example.invalid"
    settings.EMAIL_DELIVERY_ENABLED = True
    contact = make_contact(
        name='<img src=x onerror="alert(1)">',
        subject="<script>subject</script>",
        message="<script>message</script>\nSecond line\n" + "تفاصيل " * 800 + "END-OF-MESSAGE",
    )
    with patch("apps.notifications.services.email._dispatch_email_delivery"):
        from apps.notifications.services.email import queue_email

        delivery = queue_email(
            message_type="contact_admin_alert",
            recipient=settings.OPERATIONS_EMAIL,
            recipient_source="operations",
            recipient_reference=str(contact.pk),
            language="ar",
            idempotency_key=f"synthetic-contact-alert:{contact.pk}",
        )
    with patch("apps.notifications.services.email.DjangoEmailProvider.send") as send:
        from apps.notifications.services.email import EmailSendResult

        send.return_value = EmailSendResult(True, "sent")
        assert send_queued_email(delivery.pk).sent
        rendered = build_guest_email(send.call_args.args[0])
    html = rendered.alternatives[0].content
    assert escape(contact.name) in html and escape(contact.subject) in html
    assert "<script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;message&lt;/script&gt;<br>Second line<br>" in html
    assert "END-OF-MESSAGE" in html and contact.message in rendered.body


def test_contact_alert_for_removed_record_links_to_inbox_without_guessing_content(
    settings, owner_client
):
    settings.OPERATIONS_EMAIL = "operations@example.invalid"
    settings.EMAIL_DELIVERY_ENABLED = True
    contact = make_contact()
    with patch("apps.notifications.services.email._dispatch_email_delivery"):
        from apps.notifications.services.email import queue_email

        delivery = queue_email(
            message_type="contact_admin_alert",
            recipient=settings.OPERATIONS_EMAIL,
            recipient_source="operations",
            recipient_reference=str(contact.pk),
            language="ar",
            idempotency_key=f"synthetic-contact-removed:{contact.pk}",
        )
    contact.delete()
    with patch("apps.notifications.services.email.DjangoEmailProvider.send") as send:
        from apps.notifications.services.email import EmailSendResult

        send.return_value = EmailSendResult(True, "sent")
        assert send_queued_email(delivery.pk).sent
    context = send.call_args.args[0].context
    assert context["contact_message"] is None
    assert context["action_url"].endswith(reverse("admin:core_contactmessage_changelist"))
