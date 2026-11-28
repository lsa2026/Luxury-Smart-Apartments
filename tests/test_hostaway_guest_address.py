"""New create payloads only: documented optional fields, no guest/provider writes."""

from copy import deepcopy
from dataclasses import replace

import pytest

from apps.reservations.services.hostaway_booking import prepare_local_reservation
from apps.reservations.services.reservation_payloads import build_hostaway_reservation_request
from tests import test_hostaway_booking_phase5 as phase5
from tests import test_stay_extensions as ext

setup_extension = ext.setup_extension
pytestmark = pytest.mark.django_db
ADDRESS_KEYS = {"guestAddress", "guestCity", "guestZipCode"}


@pytest.fixture
def booking(settings, monkeypatch):
    settings.HOSTAWAY_DIRECT_CHANNEL_ID = 2000
    monkeypatch.setattr("httpx.Client.request", lambda *a, **kw: pytest.fail("network forbidden"))
    intent = phase5.make_intent()
    reservation = prepare_local_reservation(intent)
    return intent, reservation


def build(booking, **fields):
    intent, reservation = booking
    for name, value in fields.items():
        setattr(intent, name, value)
    reservation.booking_intent = intent
    return build_hostaway_reservation_request(
        reservation, current_quote=phase5.complete_availability(intent).quote
    )


@pytest.mark.parametrize(
    "language,street,city,region,country,postcode",
    [
        ("ar", "شارع مصطنع ١٠", "الرياض", "Riyadh", "SA", "00123"),
        ("en", "10 Synthetic Road", "London", "England", "GB", "SW1A1AA"),
        ("fr", "10 rue synthétique", "Marrakech", "Marrakech", "MA", "04000"),
    ],
)
def test_new_booking_preserves_source_address_email_phone_and_finance(
    booking, language, street, city, region, country, postcode
):
    before = build(booking).to_payload()
    intent = booking[0]
    payload = build(
        booking,
        language=language,
        billing_street1=street,
        billing_city=city,
        billing_state=region,
        billing_country=country,
        billing_postcode=postcode,
    ).to_payload()
    assert payload["guestAddress"] == street + "\n" + region
    assert payload["guestCity"] == city and payload["guestZipCode"] == postcode
    assert payload["guestCountry"] == country
    assert payload["guestEmail"] == intent.guest_email and payload["phone"] == intent.guest_phone
    assert payload["guestLocale"] == language
    assert "guestState" not in payload
    for name in (
        "arrivalDate",
        "departureDate",
        "totalPrice",
        "currency",
        "financeField",
        "listingMapId",
        "channelId",
    ):
        assert payload[name] == before[name]


@pytest.mark.parametrize("missing", ["", "Not provided", " UNKNOWN ", "n/A", None])
def test_missing_or_placeholder_address_is_omitted_and_default_country_does_not_override(
    booking, missing
):
    payload = build(
        booking,
        guest_country_code="MA",
        billing_country="SA",
        **dict.fromkeys(
            ("billing_street1", "billing_city", "billing_state", "billing_postcode"), missing
        ),
    ).to_payload()
    assert ADDRESS_KEYS.isdisjoint(payload) and "guestState" not in payload
    assert payload["guestCountry"] == "MA"
    assert (
        payload["guestEmail"] == booking[0].guest_email
        and payload["phone"] == booking[0].guest_phone
    )


@pytest.mark.parametrize(
    "part,expected",
    [
        ("billing_street1", "guestAddress"),
        ("billing_state", "guestAddress"),
        ("billing_city", "guestCity"),
        ("billing_postcode", "guestZipCode"),
    ],
)
def test_partial_optional_address_does_not_invent_other_components(booking, part, expected):
    fields = dict.fromkeys(
        ("billing_street1", "billing_city", "billing_state", "billing_postcode"), ""
    )
    fields[part] = "00123" if part == "billing_postcode" else "Synthetic"
    payload = build(booking, **fields).to_payload()
    assert ADDRESS_KEYS.intersection(payload) == {expected}
    assert payload[expected] == fields[part]


@pytest.mark.parametrize("country", ["", "Unknown", "ZZ", "Not provided"])
def test_unverified_address_country_keeps_existing_guest_country(booking, country):
    assert (
        build(booking, billing_country=country, guest_country_code="FR").to_payload()[
            "guestCountry"
        ]
        == "FR"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("guest_address", "x" * 152),
        ("guest_city", "x" * 81),
        ("guest_zip_code", "x" * 17),
        ("guest_address", None),
        ("guest_city", ["synthetic"]),
    ],
)
def test_dto_rejects_malformed_optional_fields_without_echoing_address(booking, field, value):
    with pytest.raises(ValueError, match="^guest_address_invalid$"):
        replace(build(booking), **{field: value}).to_payload()


def test_entire_valid_source_length_is_preserved_without_silent_truncation(booking):
    value = build(
        booking,
        billing_street1="s" * 100,
        billing_state="r" * 50,
        billing_city="c" * 80,
        billing_postcode="0" * 16,
    ).to_payload()
    assert value["guestAddress"] == "s" * 100 + "\n" + "r" * 50
    assert len(value["guestCity"]) == 80 and value["guestZipCode"] == "0" * 16


def test_new_private_address_components_are_excluded_from_dto_repr(booking):
    request = build(
        booking,
        billing_street1="SYNTHETIC_PRIVATE_ADDRESS",
        billing_state="SYNTHETIC_PRIVATE_REGION",
        billing_city="SYNTHETIC_PRIVATE_CITY",
        billing_postcode="PRIVATE00012",
    )
    assert "SYNTHETIC_PRIVATE" not in repr(request) and "PRIVATE00012" not in repr(request)


def test_extension_child_uses_saved_correction_address_and_never_changes_ota_parent(
    setup_extension, monkeypatch
):
    case = setup_extension
    original = deepcopy(case.raw)
    result, remote, _ = ext.create_child(case, monkeypatch)
    assert result.code == "created" and len(remote.posts) == 1
    body = remote.posts[0]
    assert body["guestAddress"] == "10 Test Road\nRiyadh"
    assert body["guestCity"] == "Riyadh" and body["guestZipCode"] == "12345"
    assert body["guestEmail"] == "corrected@example.invalid" and body["phone"] == "+966500000000"
    assert body["hostNote"] == "EXT01:" + str(case.extension.pk)
    assert "guestState" not in body and case.raw == original
    assert not ext.Reservation.objects.filter(hostaway_reservation_id=original["id"]).exists()


def test_extension_address_mutation_requires_reapproval_before_creation(
    setup_extension, monkeypatch
):
    case = setup_extension
    ext.finalize(case)
    type(case.extension.draft).objects.filter(pk=case.extension.draft_id).update(billing_address={})
    result, remote, _ = ext.create_child(case, monkeypatch)
    assert result.code == "extension_contact_requires_review"
    assert not remote.posts
    assert not ext.Reservation.objects.exists()


def test_existing_created_reservation_never_posts_again_for_changed_address(booking, settings):
    settings.HOSTAWAY_LIVE_BOOKING_ENABLED = True
    intent, reservation = booking
    remote = phase5.CreateClientStub(phase5.create_result(reservation, payment_status=""))
    service = phase5.HostawayBookingService(
        client=remote,
        availability_service=phase5.AvailabilityStub(phase5.complete_availability(intent)),
    )
    assert service.create_manual_reservation_before_payment(reservation).code == "confirmed"
    assert remote.calls == 1
    type(intent).objects.filter(pk=intent.pk).update(billing_street1="Another synthetic address")
    reservation.refresh_from_db()
    assert service.create_manual_reservation_before_payment(reservation).code == "already_confirmed"
    assert remote.calls == 1
