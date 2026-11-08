"""Reuse the production phone/address journey without bookings or messages."""

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from apps.reservations.morning_views import MorningStayEditForm, MorningStayForm
from tests.test_morning_stays import create, property_and_remote, window
from tests.test_morning_stays import isolated as isolated  # noqa: F401

pytestmark = pytest.mark.django_db


def data():
    prop, _ = property_and_remote()
    start, _ = window()
    return dict(
        request_id=uuid.uuid4(),
        property=prop.pk,
        date=start.date(),
        start_time="01:00",
        end_time="11:00",
        guest_name="Synthetic Guest",
        guest_email="synthetic@example.test",
        guests=1,
        amount_sar="500",
    )


@pytest.mark.parametrize(
    "phone,country,expected",
    [
        ("0501234567", "SA", "+966501234567"),
        ("020 7946 0958", "GB", "+442079460958"),
        ("+44 20 7946 0958", "SA", "+442079460958"),
        ("0044 20 7946 0958", "SA", "+442079460958"),
    ],
)
def test_morning_uses_shared_phone_parser(phone, country, expected):
    form = MorningStayForm(data() | {"guest_phone": phone, "guest_phone_country": country})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_phone"] == expected


def test_invalid_country_and_phone_are_rejected():
    base = data()
    assert not MorningStayForm(
        base | {"guest_phone": "invalid", "guest_phone_country": "SA"}
    ).is_valid()
    assert not MorningStayForm(
        base | {"guest_phone": "0501234567", "guest_phone_country": "ZZ"}
    ).is_valid()


def test_edit_infers_country_and_create_groups_address_after_phone():
    stay = create()
    assert MorningStayEditForm(stay=stay).initial["guest_phone_country"] == "US"
    client = Client()
    client.force_login(stay.created_by)
    page = client.get(reverse("notifications:morning_create"))
    html = page.content.decode()
    assert html.count("data-checkout-phone ") == 1
    assert html.count("data-checkout-address hidden") == 0  # disabled provider keeps manual entry
    assert html.index('name="guest_phone"') < html.index('class="morning-address-fields"')
    assert html.count('name="guest_phone_country"') == 1
    assert html.count('name="billing_street1"') == 1
    assert "js/checkout-phone.js" in html
    result = client.post(
        reverse("accounts:profile_phone_format"), {"phone": "+442079460958", "country": "SA"}
    )
    assert result.status_code == 200
    assert result.json()["country"] == "GB"


def test_list_has_readable_mobile_field_labels():
    stay = create()
    client = Client()
    client.force_login(stay.created_by)
    html = client.get(reverse("notifications:morning_list")).content.decode()
    assert "morning-stays-table" in html
    for label in ["الضيف", "الشقة", "الدخول", "الخروج", "الإجمالي", "الحالة"]:
        assert f'data-label="{label}"' in html


def test_bare_country_code_number_infers_us_without_creating_a_booking():
    stay = create()
    client = Client()
    client.force_login(stay.created_by)
    url = reverse("notifications:morning_address", args=["phone"])
    response = client.post(url, {"phone": "16465550123", "country": "SA"})
    assert response.status_code == 200
    assert response.json() == {"phone": "+16465550123", "country": "US"}
    form = MorningStayForm(
        {
            "request_id": uuid.uuid4(),
            "property": stay.property_id,
            "date": window()[0].date(),
            "start_time": "01:00",
            "end_time": "11:00",
            "guest_name": "Synthetic Guest",
            "guest_email": "synthetic@example.test",
            "guests": 1,
            "amount_sar": "500",
            "guest_phone": "16465550123",
            "guest_phone_country": "SA",
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["guest_phone"] == "+16465550123"
    assert client.get(url).status_code == 405
    assert Client().post(url, {"phone": "16465550123"}).status_code == 302
    assert client.post(url, {"phone": "1", "country": "SA"}).status_code == 400
