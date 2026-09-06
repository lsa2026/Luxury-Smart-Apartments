from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import connection
from django.template import Context, Template
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from apps.core.models import ContactMessage, FAQItem, SitePage
from apps.core.templatetags.presentation import (
    localized_count,
    localized_date,
    localized_datetime,
    localized_money,
    localized_number,
    localized_price_component,
    money_amount,
)
from apps.payments.models import PaymentAttempt
from apps.properties.models import Amenity, Property, PropertyAmenity, PropertyImage
from apps.reservations.forms import AvailabilitySearchForm
from apps.reservations.models import Reservation
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


def property_factory(
    listing_id: int,
    *,
    visible: bool = True,
    active: bool = True,
) -> Property:
    return Property.objects.create(
        hostaway_listing_id=listing_id,
        slug=f"phase-eight-{listing_id}",
        hostaway_name=f"Verified stay {listing_id}",
        name_ar=f"إقامة {listing_id}",
        name_en=f"Stay {listing_id}",
        description_ar="وصف آمن للوحدة.",
        description_en="Safe property description.",
        city="Riyadh",
        city_ar="الرياض",
        city_en="Riyadh",
        country_code="SA",
        currency_code="SAR",
        person_capacity=4,
        bedrooms_number=2,
        beds_number=3,
        bathrooms_number=Decimal("2.0"),
        is_visible=visible,
        hostaway_special_status="" if active else "archived",
    )


def image_factory(property_obj: Property, image_id: int, *, visible: bool = True) -> PropertyImage:
    return PropertyImage.objects.create(
        property=property_obj,
        hostaway_image_id=image_id,
        hostaway_url=f"https://hostaway-platform.s3.us-west-2.amazonaws.com/{image_id}.jpg",
        sync_key=f"id:{image_id}",
        source=PropertyImage.Source.HOSTAWAY,
        is_visible=visible,
        is_cover=image_id % 100 == 1 and visible,
        alt_text_ar="غرفة معيشة في الوحدة",
        alt_text_en="Property living room",
    )


def review_factory(property_obj: Property, review_id: int, *, featured: bool = False) -> Review:
    return Review.objects.create(
        hostaway_review_id=review_id,
        property=property_obj,
        hostaway_listing_map_id=property_obj.hostaway_listing_id,
        review_type=Review.Type.GUEST_TO_HOST,
        status=Review.Status.PUBLISHED,
        guest_name="Synthetic Guest",
        rating=Decimal("9.0"),
        public_review="Synthetic public review.",
        departure_date=date(2026, 1, min(review_id, 28)),
        is_visible=True,
        is_featured=featured,
        synced_at=timezone.now(),
    )


def populated_property(listing_id: int = 801) -> Property:
    property_obj = property_factory(listing_id)
    image_factory(property_obj, listing_id * 100 + 1)
    amenity = Amenity.objects.create(
        hostaway_amenity_id=listing_id,
        name="WiFi",
        name_ar="واي فاي",
        name_en="Wi-Fi",
    )
    PropertyAmenity.objects.create(
        property=property_obj,
        amenity=amenity,
        source=PropertyAmenity.Source.HOSTAWAY,
    )
    review_factory(property_obj, listing_id)
    return property_obj


def test_home_is_arabic_rtl_and_has_accessible_landmarks() -> None:
    populated_property()
    content = Client().get("/").content.decode()
    assert 'lang="ar" dir="rtl"' in content
    assert "إقامة ذكية فاخرة" in content
    assert "إقامات ذكية فاخرة" not in content
    footer = content[content.index('<footer class="site-footer"') :]
    assert "<h2>إقامة ذكية فاخرة</h2>" in footer
    assert 'class="skip-link"' in content
    assert '<main id="main-content"' in content
    assert "<header" in content and "<footer" in content


def test_footer_does_not_show_stay_mode_badges() -> None:
    content = Client().get("/").content.decode()
    footer = content[content.index('<footer class="site-footer"') :]

    assert "إقامات يومية" not in footer
    assert "إقامات شهرية" not in footer
    assert "footer-brand__modes" not in footer


def test_english_switch_is_ltr_and_translated() -> None:
    property_obj = populated_property()
    property_obj.description_en = ""
    property_obj.description_fr = "Description française à ne pas afficher en anglais."
    property_obj.hostaway_description = "Hostaway English source description."
    property_obj.save(update_fields=["description_en", "description_fr", "hostaway_description"])
    client = Client()
    response = client.post(
        "/i18n/setlang/",
        {"language": "en", "next": property_obj.get_absolute_url()},
    )
    assert response.status_code == 302
    content = client.get(property_obj.get_absolute_url()).content.decode()
    assert 'lang="en" dir="ltr"' in content
    assert "About this stay" in content
    assert property_obj.hostaway_description in content
    assert property_obj.description_fr not in content


def test_french_switch_is_ltr_and_translates_interface_and_hostaway_content() -> None:
    property_obj = populated_property(802)
    property_obj.name_fr = "Séjour intelligent 802"
    property_obj.description_fr = "Description française complète du logement."
    property_obj.city_fr = "Riyad"
    property_obj.save(update_fields=["name_fr", "description_fr", "city_fr"])
    amenity = property_obj.property_amenities.select_related("amenity").get().amenity
    amenity.name_fr = "Wi-Fi"
    amenity.save(update_fields=["name_fr"])
    image = property_obj.images.get()
    image.alt_text_fr = "Salon du logement"
    image.save(update_fields=["alt_text_fr"])

    client = Client()
    response = client.post(
        "/i18n/setlang/",
        {"language": "fr", "next": property_obj.get_absolute_url()},
    )
    assert response.status_code == 302
    content = client.get(property_obj.get_absolute_url()).content.decode()
    assert 'lang="fr" dir="ltr"' in content
    assert "À propos de ce séjour" in content
    assert property_obj.name_fr in content
    assert property_obj.description_fr in content
    assert image.alt_text_fr in content


def test_language_switcher_has_all_three_languages() -> None:
    content = Client().get("/").content.decode()
    assert 'value="ar"' in content
    assert 'value="en"' in content
    assert 'value="fr"' in content
    assert "Français" in content
    assert "data-language-select" in content


@pytest.mark.parametrize(
    ("language", "direction"),
    (("ar", "rtl"), ("en", "ltr"), ("fr", "ltr")),
)
def test_currency_selector_is_custom_accessible_and_bidi_safe(
    language: str,
    direction: str,
) -> None:
    client = Client()
    client.post("/i18n/setlang/", {"language": language, "next": "/"})

    content = client.get("/").content.decode()

    assert f'lang="{language}" dir="{direction}"' in content
    assert content.count("data-currency-menu") == 2
    assert content.count('class="currency-selector__trigger"\n            role="button"') == 2
    assert content.count('role="menu"') == 2
    assert content.count('role="menuitemradio"') == 8
    assert content.count('name="currency"') == 8
    assert content.count('value="SAR"') >= 2
    assert content.count('value="MAD"') >= 2
    assert content.count('value="USD"') >= 2
    assert content.count('value="EUR"') >= 2
    assert content.count('aria-checked="true"') == 2
    assert content.count('class="currency-selector__identity" dir="ltr"') == 8
    assert "<select name=\"currency\"" not in content


def test_currency_selector_marks_the_persisted_currency_as_current() -> None:
    client = Client()
    client.post("/i18n/setlang/", {"language": "en", "next": "/"})
    selected = client.post(
        reverse("payments:set_currency"),
        {"currency": "USD", "next": "/"},
    )

    assert selected.status_code == 302
    content = client.get("/").content.decode()
    selected_option = (
        'class="currency-selector__option is-selected"\n'
        '                type="submit"\n'
        '                name="currency"\n'
        '                value="USD"'
    )
    assert content.count(selected_option) == 2
    assert content.count('aria-label="Display currency: USD"') == 2


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ar", [("", "كل المدن"), ("Riyadh", "الرياض"), ("Marrakesh", "مراكش")]),
        ("en", [("", "All cities"), ("Riyadh", "Riyadh"), ("Marrakesh", "Marrakech")]),
        ("fr", [("", "Toutes les villes"), ("Riyadh", "Riyad"), ("Marrakesh", "Marrakech")]),
    ],
)
def test_availability_filter_has_only_supported_cities(
    language: str,
    expected: list[tuple[str, str]],
) -> None:
    property_factory(806)
    with translation.override(language):
        choices = [
            (value, str(label)) for value, label in AvailabilitySearchForm().fields["city"].choices
        ]

    assert choices == expected


def test_availability_search_leaves_city_and_property_optional() -> None:
    form = AvailabilitySearchForm()

    assert form.fields["city"].required is False
    assert form.fields["property"].required is False
    assert form.fields["guests"].initial == 2


def test_availability_filter_exposes_city_capacity_and_readable_dates() -> None:
    riyadh = property_factory(807)
    marrakech = property_factory(808)
    marrakech.city = "Marrakesh"
    marrakech.city_ar = "مراكش"
    marrakech.city_en = "Marrakech"
    marrakech.city_fr = "Marrakech"
    marrakech.save(update_fields=["city", "city_ar", "city_en", "city_fr"])

    content = Client().get("/").content.decode()

    assert f'value="{riyadh.pk}" data-city="Riyadh" data-capacity="4"' in content
    assert f'value="{marrakech.pk}" data-city="Marrakesh" data-capacity="4"' in content
    assert content.count('type="date"') == 2
    assert content.count('dir="ltr" lang="en-CA"') == 2


@pytest.mark.parametrize(
    ("language", "expected_cities"),
    [
        ("ar", ("الرياض", "مراكش")),
        ("en", ("Riyadh", "Marrakech")),
        ("fr", ("Riyad", "Marrakech")),
    ],
)
def test_footer_has_only_the_two_fixed_cities_on_every_page(
    language: str,
    expected_cities: tuple[str, str],
) -> None:
    riyadh = property_factory(803)
    duplicate_riyadh = property_factory(804)
    marrakech = property_factory(805)
    marrakech.city = "Marrakech"
    marrakech.city_ar = "مراكش"
    marrakech.city_en = "Marrakech"
    marrakech.city_fr = "Marrakech"
    marrakech.save(update_fields=["city", "city_ar", "city_en", "city_fr"])
    duplicate_riyadh.city_ar = "Manea Al Mreidi"
    duplicate_riyadh.save(update_fields=["city_ar"])

    for path in ("/", "/properties/", riyadh.get_absolute_url()):
        content = Client().get(path, HTTP_ACCEPT_LANGUAGE=language).content.decode()
        city_list = content[content.index("<div data-footer-cities") :]
        city_list = city_list[: city_list.index("</ul>")]

        assert city_list.count("<li>") == 2
        assert city_list.count(f"<li>{expected_cities[0]}</li>") == 1
        assert city_list.count(f"<li>{expected_cities[1]}</li>") == 1
        assert "Manea Al Mreidi" not in city_list


def test_seven_visible_properties_are_listed() -> None:
    for listing_id in range(810, 817):
        property_factory(listing_id)
    response = Client().get("/properties/")
    assert response.status_code == 200
    assert len(response.context["properties"]) == 7


def test_hidden_and_archived_properties_are_not_public() -> None:
    visible = property_factory(820)
    hidden = property_factory(821, visible=False)
    archived = property_factory(822, active=False)
    content = Client().get("/properties/").content.decode()
    assert visible.name_ar in content
    assert hidden.name_ar not in content
    assert archived.name_ar not in content


def test_property_card_has_dimensions_lazy_image_and_alt() -> None:
    property_obj = populated_property(830)
    content = Client().get("/properties/").content.decode()
    assert property_obj.name_ar in content
    assert 'loading="lazy"' in content
    assert 'width="720"' in content
    assert 'alt="غرفة معيشة في الوحدة"' in content


def test_property_card_gallery_exposes_prefetched_slides_and_controls() -> None:
    property_obj = property_factory(831)
    for image_id in range(83101, 83106):
        image_factory(property_obj, image_id)

    content = Client().get("/properties/").content.decode()

    assert content.count("data-card-slide") == 5
    assert "data-card-previous" in content
    assert "data-card-next" in content
    assert content.count("data-card-dot") == 5
    assert "data-card-current>١</b>" in content
    assert "<span>من</span>" in content
    assert "data-card-total>٥</span>" in content


def test_property_filters_use_local_database() -> None:
    property_factory(840)
    with patch(
        "apps.integrations.hostaway.client.HostawayClient.get_listings",
        side_effect=AssertionError("Hostaway must not be called"),
    ):
        response = Client().get("/properties/", {"city": "Riyadh", "guests": 2})
    assert response.status_code == 200


def test_property_type_filter_uses_localized_customer_label() -> None:
    property_obj = property_factory(841)
    property_obj.room_type = "entire_home"
    property_obj.save(update_fields=["room_type"])
    content = Client().get("/properties/").content.decode()
    assert ">وحدة كاملة</option>" in content


def test_property_detail_gallery_opens_all_images_without_leaving_page() -> None:
    property_obj = property_factory(850)
    for image_id in range(85001, 85013):
        image_factory(property_obj, image_id)
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert content.count("data-lightbox-open") == 6
    assert "<dialog" in content
    assert "data-lightbox-close" in content
    assert "data-lightbox-progress" in content
    assert "data-lightbox-total>١٢</span>" in content
    assert "85012.jpg" in content
    assert f'href="{reverse("properties:gallery", args=[property_obj.slug])}"' not in content
    assert 'aria-label="إغلاق المعرض"' in content


def test_property_lightbox_and_gallery_pagination_are_rtl_safe() -> None:
    property_obj = property_factory(851)
    for image_id in range(85101, 85114):
        image_factory(property_obj, image_id)

    detail = Client().get(property_obj.get_absolute_url()).content.decode()
    gallery = Client().get(reverse("properties:gallery", args=[property_obj.slug])).content.decode()

    assert "data-lightbox-current>١</b>" in detail
    assert "<span>من</span>" in detail
    assert "data-lightbox-total>١٣</span>" in detail
    assert "صفحة ١ من ٢" in gallery


def test_hidden_image_and_private_address_are_not_rendered() -> None:
    property_obj = populated_property(860)
    property_obj.address = "PRIVATE ADDRESS 123"
    property_obj.public_address = "Public district"
    property_obj.save()
    image_factory(property_obj, 86002, visible=False)
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert "PRIVATE ADDRESS 123" not in content
    assert "86002.jpg" not in content


def test_stale_marketplace_cdn_image_is_not_rendered_in_the_public_gallery() -> None:
    property_obj = populated_property(861)
    PropertyImage.objects.create(
        property=property_obj,
        hostaway_image_id=86102,
        hostaway_url="https://a0.muscache.com/im/pictures/stale-image.jpg",
        sync_key="id:86102",
        source=PropertyImage.Source.HOSTAWAY,
        alt_text_ar="صورة غير صالحة",
    )

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "stale-image.jpg" not in content


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "ar",
            (
                "مراجعة واحدة",
                "٤.٩ عبر جميع قنوات الحجز",
                "ابتداءً من",
                "قبل الحجز",
                "تسجيل الوصول من",
            ),
        ),
        (
            "fr",
            (
                "1 avis",
                "4.9 sur l’ensemble des canaux de réservation",
                "À partir de",
                "Avant de réserver",
                "Arrivée à partir de",
            ),
        ),
    ],
)
def test_property_detail_renders_localized_review_price_and_policy_copy(
    language: str,
    expected: tuple[str, ...],
) -> None:
    property_obj = populated_property(862)
    property_obj.average_review_rating = Decimal("9.8")
    property_obj.indicative_nightly_from = Decimal("700.00")
    property_obj.indicative_currency = "SAR"
    property_obj.check_in_time_start = 15
    property_obj.save()

    content = (
        Client()
        .get(property_obj.get_absolute_url(), HTTP_ACCEPT_LANGUAGE=language)
        .content.decode()
    )

    for phrase in expected:
        assert phrase in content


def test_amenity_and_review_are_visible_on_detail() -> None:
    property_obj = populated_property(870)
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert "واي فاي" in content
    assert "Synthetic public review." in content


def test_featured_review_is_ordered_first() -> None:
    property_obj = property_factory(880)
    review_factory(property_obj, 2, featured=False)
    review_factory(property_obj, 1, featured=True)
    content = Client().get("/reviews/").content.decode()
    assert content.index("مميزة") < content.index("Synthetic public review.")


def test_property_detail_does_not_call_hostaway() -> None:
    property_obj = populated_property(890)
    with patch(
        "apps.integrations.hostaway.client.HostawayClient.get_listing_calendar",
        side_effect=AssertionError("Hostaway must not be called on GET"),
    ):
        response = Client().get(property_obj.get_absolute_url())
    assert response.status_code == 200


def test_availability_form_has_csrf_dates_loading_and_submit_guard() -> None:
    property_obj = populated_property(900)
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert "csrfmiddlewaretoken" in content
    assert 'type="date"' in content
    assert "availability-form__loading" in content
    assert "data-submit-once" in content
    assert "data-availability-errors" in content
    assert "data-required-message" in content


@pytest.mark.parametrize(
    ("url", "heading"),
    [
        ("/about/", "من نحن"),
        ("/faq/", "الأسئلة الشائعة"),
        ("/contact/", "اتصل بنا"),
        ("/legal/terms/", "الشروط والأحكام"),
        ("/legal/privacy/", "سياسة الخصوصية"),
        ("/legal/cancellation/", "سياسة الإلغاء"),
        ("/legal/cookies/", "سياسة ملفات الارتباط"),
    ],
)
def test_content_pages(url: str, heading: str) -> None:
    response = Client().get(url)
    assert response.status_code == 200
    assert heading in response.content.decode()


def test_faq_uses_accessible_details() -> None:
    FAQItem.objects.create(
        question_ar="سؤال مصطنع",
        question_en="Synthetic question",
        answer_ar="إجابة مصطنعة",
        answer_en="Synthetic answer",
    )
    content = Client().get("/faq/").content.decode()
    assert "<details" in content and "<summary>" in content


def test_contact_valid_submission_is_local_only() -> None:
    with patch("django.core.mail.send_mail", side_effect=AssertionError("No email")):
        response = Client().post(
            "/contact/",
            {
                "name": "Test Guest",
                "email": "guest@example.invalid",
                "phone": "",
                "subject": "Synthetic enquiry",
                "message": "A synthetic message with enough detail.",
                "website": "",
            },
        )
    assert response.status_code == 302
    assert ContactMessage.objects.count() == 1


def test_contact_honeypot_is_rendered_once_and_success_is_visible() -> None:
    client = Client()
    form_content = client.get("/contact/").content.decode()
    assert form_content.count('name="website"') == 1
    assert ">Website</label>" not in form_content

    response = client.post(
        "/contact/",
        {
            "name": "Test Guest",
            "email": "guest@example.invalid",
            "phone": "",
            "subject": "Synthetic enquiry",
            "message": "A synthetic message with enough detail.",
            "website": "",
        },
        follow=True,
    )
    assert "تم استلام رسالتك." in response.content.decode()


def test_money_amount_is_customer_friendly() -> None:
    assert money_amount(Decimal("5651.0000")) == "5,651.00"
    assert money_amount(Decimal("500.256")) == "500.26"


@pytest.mark.parametrize(
    # Arabic writes the currency as a symbol. An ISO code on an otherwise
    # Arabic page is the untranslated string this batch corrects, so the
    # expected currency is part of the case rather than assumed to be "SAR".
    ("language", "expected_number", "expected_currency", "currency_before_amount"),
    [
        ("ar", "٢٬٤٥٠٫٠٠", "ر.س", False),
        ("en", "2,450.00", "SAR", True),
        ("fr", "2\u202f450,00", "SAR", False),
    ],
)
def test_localized_money_follows_language_conventions(
    language: str,
    expected_number: str,
    expected_currency: str,
    currency_before_amount: bool,
) -> None:
    with translation.override(language):
        rendered = str(localized_money(Decimal("2450"), "sar"))

    assert expected_number in rendered
    assert 'dir="ltr"' in rendered
    assert expected_currency in rendered
    assert (
        rendered.index(expected_currency) < rendered.index(expected_number)
    ) is currency_before_amount


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ar", "٢٥ نوفمبر ٢٠٢٦"),
        ("en", "25 November 2026"),
        ("fr", "25 novembre 2026"),
    ],
)
def test_localized_date_matches_money_numerals(language: str, expected: str) -> None:
    with translation.override(language):
        assert localized_date(date(2026, 11, 25)) == expected


def test_localized_datetime_keeps_twenty_four_hour_time() -> None:
    moment = datetime(2026, 8, 28, 8, 47)
    with translation.override("ar"):
        assert localized_datetime(moment) == "٢٨ أغسطس ٢٠٢٦ ٠٨:٤٧"
    with translation.override("en"):
        assert localized_datetime(moment) == "28 August 2026 08:47"


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ar", "٣"), ("en", "3"), ("fr", "3")],
)
def test_localized_number_follows_the_active_language(language: str, expected: str) -> None:
    with translation.override(language):
        assert localized_number(3) == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "١ ضيف"), (2, "٢ ضيفان"), (4, "٤ ضيوف"), (11, "١١ ضيفًا")],
)
def test_localized_count_keeps_arabic_plural_forms(count: int, expected: str) -> None:
    """Arabic numerals must not cost us the six plural forms in the catalogue."""
    template = Template(
        "{% load i18n %}{% blocktrans count counter=n %}{{ counter }} guest"
        "{% plural %}{{ counter }} guests{% endblocktrans %}"
    )
    with translation.override("ar"):
        assert template.render(Context({"n": localized_count(count)})) == expected


def test_localized_temporal_filters_ignore_empty_values() -> None:
    with translation.override("ar"):
        assert localized_date(None) == ""
        assert localized_datetime("") == ""
        assert localized_number(None) == ""
        assert localized_date("not-a-date") == ""


def test_localized_money_uses_currency_minor_units() -> None:
    with translation.override("en"):
        yen = str(localized_money(Decimal("5651.8"), "JPY"))
        dinar = str(localized_money(Decimal("12.3456"), "KWD"))

    assert "JPY" in yen
    assert "5,652" in yen
    assert ".00" not in yen
    assert "KWD" in dinar
    assert "12.346" in dinar


def test_localized_money_handles_untrusted_or_invalid_values() -> None:
    with translation.override("en"):
        unknown_currency = str(localized_money(Decimal("25"), "<script>"))
        invalid_amount = str(localized_money("not-a-number", "SAR"))

    assert "<script>" not in unknown_currency
    assert "25.00" in unknown_currency
    assert "money--unavailable" in invalid_amount
    assert "—" in invalid_amount


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ar", "خصم أسبوعي"), ("en", "Weekly discount"), ("fr", "Remise hebdomadaire")],
)
def test_hostaway_price_component_is_localized(language: str, expected: str) -> None:
    with translation.override(language):
        assert localized_price_component("Weekly discount") == expected


def test_contact_honeypot_and_xss_cleaning() -> None:
    response = Client().post(
        "/contact/",
        {
            "name": "<b>Test</b>",
            "email": "guest@example.invalid",
            "subject": "<script>Subject</script>",
            "message": "<img src=x> Synthetic message body.",
            "website": "",
        },
    )
    assert response.status_code == 302
    message = ContactMessage.objects.get()
    assert "<" not in message.name
    assert "<" not in message.subject
    assert "<" not in message.message


def test_contact_honeypot_rejects_bots() -> None:
    response = Client().post(
        "/contact/",
        {
            "name": "Bot",
            "email": "bot@example.invalid",
            "subject": "Synthetic",
            "message": "Long enough synthetic message.",
            "website": "spam.example",
        },
    )
    assert response.status_code == 400
    assert ContactMessage.objects.count() == 0


def test_contact_rate_limit() -> None:
    cache.clear()
    client = Client()
    payload = {
        "name": "Test",
        "email": "test@example.invalid",
        "subject": "Synthetic",
        "message": "Long enough synthetic message.",
        "website": "reject",
    }
    for _ in range(5):
        client.post("/contact/", payload)
    assert client.post("/contact/", payload).status_code == 429


def test_private_pages_are_noindex() -> None:
    property_obj = populated_property(910)
    response = Client().post(
        "/reservations/quotes/",
        {
            "property": property_obj.pk,
            "city": property_obj.city,
            "check_in": "2020-01-01",
            "check_out": "2020-01-03",
            "guests": 2,
        },
    )
    assert response.status_code == 200
    assert 'content="noindex,nofollow"' in response.content.decode()


def test_home_has_canonical_open_graph_and_structured_data() -> None:
    content = Client().get("/").content.decode()
    assert 'rel="canonical"' in content
    assert 'property="og:title"' in content
    assert '"@type":"Organization"' in content
    assert 'type="application/ld+json"' in content


def test_detail_has_vacation_rental_structured_data_without_listing_id() -> None:
    property_obj = populated_property(920)
    content = Client().get(property_obj.get_absolute_url()).content.decode()
    assert '"@type":"VacationRental"' in content
    assert '"@type":"BreadcrumbList"' in content
    assert "hostaway_listing_id" not in content


def test_public_csp_has_nonce_and_no_unsafe_inline() -> None:
    response = Client().get("/")
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self' 'nonce-" in csp
    assert "style-src 'self';" in csp
    assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.parametrize("path", ["/missing-phase-eight-page/", "/properties/missing/"])
def test_branded_404_pages(path: str) -> None:
    response = Client().get(path)
    assert response.status_code == 404
    assert "Luxury Smart Apartments" in response.content.decode()


def test_property_list_query_count_is_bounded() -> None:
    for listing_id in range(930, 937):
        property_obj = property_factory(listing_id)
        image_factory(property_obj, listing_id * 100 + 1)
    with CaptureQueriesContext(connection) as queries:
        response = Client().get("/properties/")
        assert response.status_code == 200
    assert len(queries) <= 5


def test_property_detail_query_count_is_bounded() -> None:
    property_obj = populated_property(940)
    with CaptureQueriesContext(connection) as queries:
        response = Client().get(property_obj.get_absolute_url())
        assert response.status_code == 200
    assert len(queries) <= 12


def test_public_browsing_creates_no_reservation_or_payment() -> None:
    property_obj = populated_property(950)
    before = (Reservation.objects.count(), PaymentAttempt.objects.count())
    client = Client()
    client.get("/")
    client.get("/properties/")
    client.get(property_obj.get_absolute_url())
    assert (Reservation.objects.count(), PaymentAttempt.objects.count()) == before


def test_content_models_are_admin_editable_sources() -> None:
    assert SitePage.objects.filter(slug="about", is_published=True).exists()
    assert FAQItem.objects.filter(is_active=True).exists()
    assert reverse("admin:core_sitepage_changelist") == "/admin/core/sitepage/"
