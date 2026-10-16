"""The booking operations entrance is private and intentionally focused."""

from unittest.mock import patch

import pytest
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
    assert "إدارة الحجوزات" in content
    assert "مركز الإدارة" in content
    assert "فواتير الضيوف" in content
    assert "البحث عن حجز" not in content
    assert 'href="' + reverse("notifications:booking_list") + '"' in content
    assert 'href="' + reverse("notifications:invoice_list") + '"' in content
    assert content.count('class="lsa-admin-home__card"') == 3
    assert "إدارة المستخدمين" in content
    assert content.count("<h1>") == 1


@pytest.mark.parametrize(
    "programme,interface,visible",
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_owner_loyalty_card_uses_existing_programme_gates(
    db, settings, programme, interface, visible
):
    settings.OPERATIONS_OWNER_ENFORCEMENT_ENABLED = True
    settings.OPERATIONS_OWNER_EMAIL = "operations-owner@example.invalid"
    settings.LOYALTY_ENABLED = programme
    settings.LOYALTY_UI_ENABLED = interface
    client = Client()
    client.force_login(_superuser())
    with patch(
        "apps.integrations.hostaway.client.HostawayClient.__init__",
        side_effect=AssertionError("Opening a navigation card must not call a provider"),
    ):
        response = client.get(reverse("notifications:hub"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert ('href="' + reverse("loyalty:admin") + '"' in content) is visible
    assert ("إدارة برنامج الولاء" in content) is visible
    assert content.count('class="lsa-admin-home__card"') == 3 + int(visible)
    assert 'dir="rtl"' in content
    assert content.count("<h1>") == 1
