import hashlib
import json

import pytest
from django.urls import reverse

from apps.core.models import ContactAIDraft
from apps.notifications.models import EmailDelivery
from tests import test_contact_workspace as workspace
from tests.test_phase9_notifications import make_contact

client = workspace.client
owner = workspace.owner
pytestmark = pytest.mark.django_db
TOKEN = "a" * 64


def api(client, path, data):
    return client.post(
        "/reservations/sama/contact-drafts/" + path + "/",
        data=json.dumps(data),
        content_type="application/json",
        secure=True,
        HTTP_AUTHORIZATION="Bearer " + TOKEN,
    )


def test_sama_drafts_require_owner_request_and_never_send(client, settings):
    settings.SAMA_BOOKING_ENABLED = True
    settings.SAMA_BOOKING_KEY_SHA256 = hashlib.sha256(TOKEN.encode()).hexdigest()
    message = make_contact()
    path = reverse("admin:core_contactmessage_change", args=[message.pk])
    page = client.get(path)
    assert page.context["contact"].status == "read"
    token = page.context["reply_form"]["request_token"].value()
    assert (
        client.post(path, {"action": "draft", "request_token": token, "language": "ar"}).status_code
        == 302
    )
    assert ContactAIDraft.objects.count() == 1
    item = api(client, "claim", {}).json()["draft"]
    assert "email" not in item and "phone" not in item
    assert api(client, "claim", {}).json()["code"] == "empty"
    data = {
        "id": item["id"],
        "lease": item["lease"],
        "body": "مسودة للمراجعة فقط",
        "status": "ready",
    }
    assert api(client, "complete", data).json()["code"] == "completed"
    assert api(client, "complete", data).status_code == 409
    page = client.get(path)
    assert page.context["reply_form"]["body"].value() == data["body"]
    assert not EmailDelivery.objects.exists()
    assert (
        client.post(
            "/reservations/sama/contact-drafts/claim/",
            data="{}",
            content_type="application/json",
            secure=True,
        ).status_code
        == 401
    )


def test_opening_only_marks_new_read_and_updates_counter(client):
    for status in ["new", "in_progress", "closed", "spam"]:
        contact = make_contact(status=status)
        client.get(reverse("admin:core_contactmessage_change", args=[contact.pk]))
        contact.refresh_from_db()
        assert contact.status == ("read" if status == "new" else status)
    assert client.get(reverse("notifications:hub")).context["new_contact_count"] == 0
