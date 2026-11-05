"""Synthetic owner contact conversations; no real recipients or provider traffic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationsManager
from apps.core.contact_inbox import _queue_reply, _token
from apps.core.models import ContactMessage, ContactReply
from apps.notifications.models import AuditLog, EmailDelivery
from apps.notifications.services.email import (
    EmailProviderError,
    EmailSendResult,
    build_guest_email,
    send_queued_email,
)
from tests.test_operations_users import login_manager
from tests.test_phase9_notifications import make_contact

pytestmark = pytest.mark.django_db
OWNER = "contact-owner@example.invalid"
BODY = "مرحبًا، شكرًا لرسالتك.\nيسعدنا توضيح التفاصيل.\nHello from our team."


@pytest.fixture
def owner(settings):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = True
    settings.OPERATIONS_OWNER_EMAIL = OWNER
    settings.EMAIL_DELIVERY_ENABLED = True
    settings.DEFAULT_FROM_EMAIL = "Luxury Living <business@example.invalid>"
    settings.EMAIL_REPLY_TO = "business@example.invalid"
    return get_user_model().objects.create_superuser(
        username=OWNER, email=OWNER, password="synthetic-only"
    )


@pytest.fixture
def client(owner):
    value = Client()
    value.force_login(owner)
    return value


def url(contact):
    return reverse("admin:core_contactmessage_change", args=[contact.pk])


def payload(client, contact, **overrides):
    page = client.get(url(contact))
    data = {
        "action": "send",
        "body": BODY,
        "language": "ar",
        "request_token": page.context["reply_form"]["request_token"].value(),
    }
    return {**data, **overrides}


def test_workspace_is_arabic_readable_escaped_and_opening_does_not_send(client):
    contact = make_contact(message="مرحبا\n\nHello\n<script>alert('unsafe')</script>")
    inbox = client.get(reverse("admin:core_contactmessage_changelist"))
    response = client.get(url(contact))
    html = response.content.decode()
    assert inbox.status_code == response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert "الرد على الرسالة" in html and "تنبيه بريد الأعمال" in html
    assert "css/admin-contact-inbox.css" in html
    assert "Change Contact message" not in html and "Save and continue editing" not in html
    assert "<script>alert('unsafe')</script>" not in html
    assert "&lt;script&gt;" in html and 'class="lsa-contact__body" dir="auto"' in html
    assert contact.email in html and contact.phone in html
    assert not ContactReply.objects.exists() and not EmailDelivery.objects.exists()
    contact.refresh_from_db()
    assert contact.status == "new"


def test_adjacent_navigation_has_stable_order_and_preserves_filters(client):
    contacts = [make_contact(subject="Shared subject") for _ in range(4)]
    ContactMessage.objects.update(created_at=timezone.now() - timedelta(days=1))
    contacts[3].status = "closed"
    contacts[3].save()
    middle = contacts[1]
    page = client.get(url(middle) + "?status=new&q=Shared&page=2")
    assert page.context["next_message_url"].startswith(url(contacts[0]) + "?")
    assert page.context["previous_message_url"].startswith(url(contacts[2]) + "?")
    assert "status=new" in page.context["next_message_url"]
    assert "q=Shared" in page.context["next_message_url"]
    assert client.get(url(contacts[0]) + "?status=new").context["next_message_url"] == ""
    assert client.get(url(contacts[2]) + "?status=new").context["previous_message_url"] == ""


def test_search_status_and_pagination_reach_saved_contacts(client):
    contacts = [make_contact(subject=f"Question {i}") for i in range(27)]
    contacts[0].status = "closed"
    contacts[0].save()
    path = reverse("admin:core_contactmessage_changelist")
    assert len(client.get(path).context["rows"]) == 25
    assert len(client.get(path + "?page=2").context["rows"]) == 2
    result = client.get(path, {"q": "Question 0", "status": "closed"})
    assert [r["item"].pk for r in result.context["rows"]] == [contacts[0].pk]
    assert client.get(path + "?page=invalid&status=invalid").status_code == 200


def test_preview_is_optional_and_never_queues_or_changes_status(client):
    contact = make_contact()
    data = payload(client, contact, action="preview", body="<b>اختبار</b>\nSecond line")
    response = client.post(url(contact), data)
    assert response.status_code == 200
    assert response.context["preview_body"] == data["body"]
    assert "&lt;b&gt;اختبار&lt;/b&gt;" in response.content.decode()
    assert not ContactReply.objects.exists() and not EmailDelivery.objects.exists()
    contact.refresh_from_db()
    assert contact.status == "new"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_explicit_reply_uses_business_email_queue_and_branded_safe_content(
    client, settings, language, django_capture_on_commit_callbacks
):
    contact = make_contact(message="PRIVATE INBOUND TEXT", subject="A subject\n<header>")
    data = payload(client, contact, language=language, body=BODY + "\n<script>unsafe()</script>")
    with patch("apps.notifications.services.email._dispatch_email_delivery") as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(url(contact), data)
    assert response.status_code == 302
    reply = ContactReply.objects.select_related("delivery").get()
    assert reply.recipient_email == contact.email and reply.body == data["body"]
    assert reply.created_by.email == OWNER and reply.delivery.status == "queued"
    dispatch.assert_called_once_with(reply.delivery.pk)
    provider = Mock()
    provider.send.return_value = EmailSendResult(True, "sent")
    assert send_queued_email(reply.delivery_id, provider=provider).sent
    packet = provider.send.call_args.args[0]
    mail = build_guest_email(packet)
    assert mail.to == [contact.email]
    assert mail.from_email == settings.DEFAULT_FROM_EMAIL
    assert mail.reply_to == [settings.EMAIL_REPLY_TO]
    assert "\n" not in mail.subject and "<header>" not in mail.subject
    assert BODY in mail.body
    assert "&lt;script&gt;" in mail.alternatives[0].content
    assert "PRIVATE INBOUND TEXT" not in mail.body + mail.alternatives[0].content
    assert "/admin/" not in mail.body + mail.alternatives[0].content
    assert send_queued_email(reply.delivery_id, provider=provider).code == "already_sent"
    assert provider.send.call_count == 1
    page = client.get(url(contact))
    assert "تم الإرسال" in page.content.decode()
    assert page.context["contact"].status == "in_progress"
    audit = AuditLog.objects.get(action="contact.reply_queued")
    assert data["body"] not in str(audit.metadata) + audit.summary


def test_double_submit_is_one_reply_but_fresh_manual_reply_is_allowed(client):
    contact = make_contact()
    data = payload(client, contact)
    assert client.post(url(contact), data).status_code == 302
    assert client.post(url(contact), data).status_code == 302
    assert ContactReply.objects.count() == EmailDelivery.objects.count() == 1
    assert client.post(url(contact), {**data, "body": "A changed body"}).status_code == 200
    assert ContactReply.objects.count() == 1
    assert client.post(url(contact), payload(client, contact)).status_code == 302
    assert ContactReply.objects.count() == EmailDelivery.objects.count() == 2


@pytest.mark.parametrize(
    "reason", ["tampered", "another_contact", "another_user", "recipient_changed", "expired"]
)
def test_reply_token_is_bound_to_owner_contact_recipient_and_age(client, owner, reason):
    contact = make_contact()
    data = payload(client, contact)
    if reason == "tampered":
        data["request_token"] += "tampered"
    elif reason == "another_contact":
        data["request_token"] = _token(make_contact(), owner)
    elif reason == "another_user":
        other = get_user_model().objects.create_user(username="other")
        data["request_token"] = _token(contact, other)
    elif reason == "recipient_changed":
        ContactMessage.objects.filter(pk=contact.pk).update(email="changed@example.invalid")
    if reason == "expired":
        with patch(
            "django.core.signing.time.time", return_value=timezone.now().timestamp() + 90000
        ):
            response = client.post(url(contact), data)
    else:
        response = client.post(url(contact), data)
    assert response.status_code == 200
    assert response.context["reply_form"].errors
    assert not ContactReply.objects.exists() and not EmailDelivery.objects.exists()


@pytest.mark.parametrize(
    "changes", [{"body": ""}, {"body": "x" * 5001}, {"language": "invalid"}, {"request_token": ""}]
)
def test_invalid_replies_never_queue(client, changes):
    contact = make_contact()
    response = client.post(url(contact), payload(client, contact, **changes))
    assert response.status_code == 200
    assert not ContactReply.objects.exists() and not EmailDelivery.objects.exists()


def test_disabled_delivery_and_queue_failure_do_not_leave_reply_records(client, settings):
    contact = make_contact()
    data = payload(client, contact)
    settings.EMAIL_DELIVERY_ENABLED = False
    assert client.post(url(contact), data).status_code == 200
    assert not ContactReply.objects.exists() and not EmailDelivery.objects.exists()
    settings.EMAIL_DELIVERY_ENABLED = True
    from django.db import DatabaseError

    with patch("apps.core.contact_inbox.queue_email", side_effect=DatabaseError):
        response = client.post(url(contact), data)
    assert response.status_code == 200
    assert not ContactReply.objects.exists() and not EmailDelivery.objects.exists()


def test_csrf_and_owner_permission_apply_to_all_actions(client, owner, settings):
    contact = make_contact()
    data = payload(client, contact)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(owner)
    assert strict.post(url(contact), data).status_code == 403
    assert Client().get(url(contact)).status_code == 302
    OperationsManager.objects.create(email="synthetic.lsa.manager@gmail.com")
    _, manager = login_manager()
    assert manager.get(url(contact)).status_code == 403
    assert manager.post(url(contact), data).status_code == 403
    assert not ContactReply.objects.exists()


@pytest.mark.parametrize(
    "failure",
    [
        "recipient_changed",
        "author_revoked",
        "deleted",
        "other_delivery",
        "invalid_reference",
        "smtp",
    ],
)
def test_worker_rechecks_reply_binding_and_displays_failed_state(client, failure):
    contact = make_contact()
    client.post(url(contact), payload(client, contact))
    reply = ContactReply.objects.get()
    provider = Mock()
    provider.send.return_value = EmailSendResult(True, "sent")
    if failure == "recipient_changed":
        ContactMessage.objects.filter(pk=contact.pk).update(email="changed@example.invalid")
    elif failure == "author_revoked":
        get_user_model().objects.filter(pk=reply.created_by_id).update(is_active=False)
    elif failure == "deleted":
        contact.delete()
    elif failure == "other_delivery":
        EmailDelivery.objects.filter(pk=reply.delivery_id).update(
            message_type="contact_confirmation"
        )
    elif failure == "invalid_reference":
        EmailDelivery.objects.filter(pk=reply.delivery_id).update(recipient_reference="invalid")
    else:
        provider.send.side_effect = EmailProviderError("email_provider_unavailable")
    assert not send_queued_email(reply.delivery_id, provider=provider).sent
    delivery = EmailDelivery.objects.get(pk=reply.delivery_id)
    assert delivery.status == "failed"
    assert provider.send.call_count == (1 if failure == "smtp" else 0)
    if failure not in {"author_revoked", "deleted"}:
        assert "تعذر الإرسال" in client.get(url(contact)).content.decode()


def test_business_alert_remains_a_notice_and_link_not_a_full_inbound_copy(client):
    contact = make_contact()
    page = client.get(url(contact))
    assert "ليس نسخة من نصها الكامل" in page.content.decode()
    assert "لا تُستورد تلقائيًا" in page.content.decode()


@pytest.mark.django_db(transaction=True)
def test_postgres_parallel_submits_queue_one_owner_reply(owner):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks.")
    contact = make_contact()
    values = {"body": BODY, "language": "ar", "request_token": _token(contact, owner)}
    barrier = Barrier(2)

    def send(_):
        close_old_connections()
        try:
            request = RequestFactory().post(url(contact))
            request.user = owner
            barrier.wait(timeout=10)
            return _queue_reply(request, contact, values)[1]
        finally:
            close_old_connections()

    with patch("apps.notifications.services.email._dispatch_email_delivery") as dispatch:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(send, range(2))) == [False, True]
    assert ContactReply.objects.count() == EmailDelivery.objects.count() == 1
    dispatch.assert_called_once()
