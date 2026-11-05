"""Owner reading/navigation only; no outbound messaging capability is added."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationsManager
from apps.core.models import ContactMessage
from apps.notifications.models import EmailDelivery
from tests.test_operations_users import login_manager
from tests.test_phase9_notifications import make_contact

pytestmark = pytest.mark.django_db
OWNER = "contact-owner@example.invalid"


@pytest.fixture
def owner(settings):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = True
    settings.OPERATIONS_OWNER_EMAIL = OWNER
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


def test_arabic_reading_preserves_original_and_never_calls_a_provider(client):
    contact = make_contact(message="مرحبا\n\nHello\n<script>alert('unsafe')</script>")
    with patch(
        "apps.integrations.hostaway.client.HostawayClient.__init__", side_effect=AssertionError
    ):
        inbox = client.get(reverse("admin:core_contactmessage_changelist"))
        response = client.get(url(contact))
    html = response.content.decode()
    assert inbox.status_code == response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert "تنبيه بريد الأعمال" in html and "ليس نسخة من نصها الكامل" in html
    assert "css/admin-contact-inbox.css" in html
    assert "Change Contact message" not in html and "Save and continue editing" not in html
    assert "<script>alert('unsafe')</script>" not in html
    assert "&lt;script&gt;" in html and 'class="lsa-contact__body" dir="auto"' in html
    assert contact.email in html and contact.phone in html
    assert not EmailDelivery.objects.exists()
    contact.refresh_from_db()
    assert contact.status == "read"


def test_stable_adjacent_navigation_preserves_search_and_status(client):
    contacts = [make_contact(subject="Shared subject") for _ in range(4)]
    ContactMessage.objects.update(created_at=timezone.now() - timedelta(days=1))
    contacts[3].status = "closed"
    contacts[3].save()
    page = client.get(url(contacts[1]) + "?status=new&q=Shared&page=2")
    assert page.context["next_message_url"].startswith(url(contacts[0]) + "?")
    assert page.context["previous_message_url"].startswith(url(contacts[2]) + "?")
    assert "status=new" in page.context["next_message_url"]
    assert "q=Shared" in page.context["next_message_url"]
    assert client.get(url(contacts[0]) + "?status=new").context["next_message_url"] == ""
    assert client.get(url(contacts[2]) + "?status=new").context["previous_message_url"] == ""


def test_search_and_pagination_reach_existing_messages(client):
    contacts = [make_contact(subject=f"Question {i}") for i in range(27)]
    contacts[0].status = "closed"
    contacts[0].save()
    path = reverse("admin:core_contactmessage_changelist")
    assert len(client.get(path).context["rows"]) == 25
    assert len(client.get(path + "?page=2").context["rows"]) == 2
    result = client.get(path, {"q": "Question 0", "status": "closed"})
    assert [row["item"].pk for row in result.context["rows"]] == [contacts[0].pk]
    assert client.get(path + "?page=invalid&status=invalid").status_code == 200


def test_existing_status_controls_work_with_csrf_and_do_not_send(client, owner):
    contact = make_contact()
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(owner)
    assert strict.post(url(contact), {"action": "status", "status": "closed"}).status_code == 403
    strict.get(url(contact), secure=True)
    response = strict.post(
        url(contact),
        {
            "action": "status",
            "status": "closed",
            "csrfmiddlewaretoken": strict.cookies["csrftoken"].value,
        },
        secure=True,
        HTTP_ORIGIN="https://testserver",
    )
    assert response.status_code == 302
    contact.refresh_from_db()
    assert contact.status == "closed"
    assert client.post(url(contact), {"_save": "Save", "status": "in_progress"}).status_code == 302
    assert not EmailDelivery.objects.exists()


@pytest.mark.parametrize("action", ["reply"])
def test_outbound_actions_are_unavailable(client, action):
    contact = make_contact()
    page = client.get(url(contact))
    assert 'name="action" value="send"' in page.content.decode()
    assert client.post(url(contact), {"action": action, "body": "Not sent"}).status_code == 405
    assert not EmailDelivery.objects.exists()


def test_owner_only_access_is_retained(client):
    contact = make_contact()
    assert Client().get(url(contact)).status_code == 302
    OperationsManager.objects.create(email="synthetic.lsa.manager@gmail.com")
    _, manager = login_manager()
    assert manager.get(url(contact)).status_code == 403
    assert manager.post(url(contact), {"action": "status", "status": "closed"}).status_code == 403


def test_legacy_unknown_status_does_not_break_reading(client):
    contact = make_contact(status="legacy")
    assert client.get(url(contact)).status_code == 200
    assert client.get(reverse("admin:core_contactmessage_changelist")).status_code == 200
