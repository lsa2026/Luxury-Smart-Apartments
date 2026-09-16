"""Phase 6.0: the operations hub is private and intentionally read-only."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse


def _superuser():
    return get_user_model().objects.create_superuser(
        username="operations-owner",
        email="operations-owner@example.invalid",
        password="pw12345!",
    )


def test_operations_hub_redirects_visitors_without_a_staff_session(db):
    response = Client().get(reverse("notifications:hub"))
    assert response.status_code == 302
    assert "/admin/login/" in response.url


def test_operations_hub_is_private_and_does_not_call_providers(db):
    client = Client()
    client.force_login(_superuser())
    with patch(
        "apps.integrations.hostaway.client.HostawayClient.__init__",
        side_effect=AssertionError("Operations hub must use local records only"),
    ):
        response = client.get(reverse("notifications:hub"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "مركز العمليات" in content
    assert "خاص ومغلق عن الضيوف" in content
    assert "لا تنشئ حجزًا أو رابط دفع أو رسالة أو استرجاعًا ماليًا" in content
