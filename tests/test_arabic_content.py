"""Batch two: Arabic amenity names, review rendering, one rating, money, FAQ."""

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone, translation
from django.utils.html import strip_tags

from apps.core.models import FAQItem, SiteSetting
from apps.core.templatetags.presentation import format_money
from apps.properties.amenity_translations import AMENITY_COPY, copy_for
from apps.properties.models import Amenity, Property, PropertyAmenity
from apps.reviews.models import Review
from apps.reviews.presentation import is_meaningful, parse_review_body
from apps.reviews.summary import rating_summary

pytestmark = pytest.mark.django_db


def make_property(listing_id: int = 9301, **overrides: object) -> Property:
    defaults = {
        "hostaway_listing_id": listing_id,
        "hostaway_listing_map_id": listing_id,
        "slug": f"content-{listing_id}",
        "hostaway_name": "Source",
        "name_ar": "وحدة",
        "name_en": "Unit",
        "city": "Riyadh",
        "city_ar": "الرياض",
        "country_code": "SA",
        "currency_code": "SAR",
        "is_visible": True,
    }
    return Property.objects.create(**{**defaults, **overrides})


def make_review(property_obj: Property, rating: str, review_id: int) -> Review:
    return Review.objects.create(
        hostaway_review_id=review_id,
        property=property_obj,
        hostaway_listing_map_id=property_obj.hostaway_listing_map_id,
        rating=Decimal(rating),
        public_review="Positive: نظيفة ومريحة",
        review_type=Review.Type.GUEST_TO_HOST,
        status=Review.Status.PUBLISHED,
        synced_at=timezone.now(),
    )


# --- 6. Arabic amenity names -------------------------------------------------


def test_every_amenity_in_the_map_carries_all_four_values() -> None:
    assert len(AMENITY_COPY) == 33
    for key, entry in AMENITY_COPY.items():
        assert entry.name_ar.strip(), key
        assert entry.name_fr.strip(), key
        assert entry.category.strip(), key


def test_the_map_is_matched_case_and_space_insensitively() -> None:
    assert copy_for("Washing Machine") == copy_for("  washing   machine  ")


def test_seeding_fills_an_english_only_amenity() -> None:
    amenity = Amenity.objects.create(name="Washing Machine", hostaway_amenity_id=1)

    call_command("seed_amenity_arabic_names", stdout=StringIO())

    amenity.refresh_from_db()
    assert amenity.name_ar == "غسالة ملابس"
    assert amenity.category == "laundry"


def test_seeding_never_overwrites_an_existing_arabic_name() -> None:
    """The administration's wording outranks anything shipped in the repository."""
    amenity = Amenity.objects.create(
        name="Washing Machine",
        name_ar="غسالة أوتوماتيكية",
        hostaway_amenity_id=2,
    )

    call_command("seed_amenity_arabic_names", stdout=StringIO())

    amenity.refresh_from_db()
    assert amenity.name_ar == "غسالة أوتوماتيكية"


def test_an_unmapped_amenity_keeps_its_english_name_and_is_reported() -> None:
    amenity = Amenity.objects.create(name="Helipad", hostaway_amenity_id=3)
    out = StringIO()

    call_command("seed_amenity_arabic_names", stdout=out)

    amenity.refresh_from_db()
    assert amenity.name_ar == ""
    assert "Helipad" in out.getvalue()


def test_dry_run_writes_nothing() -> None:
    amenity = Amenity.objects.create(name="Kitchen", hostaway_amenity_id=4)

    call_command("seed_amenity_arabic_names", dry_run=True, stdout=StringIO())

    amenity.refresh_from_db()
    assert amenity.name_ar == ""


# --- 7 and 8. Review body ----------------------------------------------------


def test_the_two_sections_are_separated_from_the_headline() -> None:
    body = parse_review_body("تجربة رائعة\nPositive: نظيفة\nNegative: الإنترنت بطيء")

    assert body.headline == "تجربة رائعة"
    assert body.positive == "نظيفة"
    assert body.negative == "الإنترنت بطيء"


@pytest.mark.parametrize("filler", ["...", ".....", "-", "  ", "___", ". . ."])
def test_a_section_holding_only_punctuation_is_dropped(filler: str) -> None:
    body = parse_review_body(f"Positive: ممتازة\nNegative: {filler}")

    assert body.positive == "ممتازة"
    assert body.negative == ""


def test_a_short_but_real_answer_survives() -> None:
    assert parse_review_body("Negative: لا").negative == "لا"


def test_a_review_without_markers_is_left_whole() -> None:
    body = parse_review_body("إقامة ممتازة وموقع رائع")

    assert body.has_sections is False
    assert body.headline == "إقامة ممتازة وموقع رائع"


def test_markers_are_matched_regardless_of_case() -> None:
    assert parse_review_body("POSITIVE: جيد").positive == "جيد"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("...", False), ("", False), ("ok", True), ("لا", True), ("a", False)],
)
def test_meaningfulness_threshold(text: str, expected: bool) -> None:
    assert is_meaningful(text) is expected


def test_the_page_shows_arabic_headings_not_the_raw_markers() -> None:
    property_obj = make_property()
    review = make_review(property_obj, "9.0", 5001)
    review.public_review += "\nNegative: الموقع بعيد"
    review.save(update_fields=["public_review"])

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "Positive:" not in content
    assert "Negative:" not in content
    assert "الإيجابيات" in content
    assert "السلبيات" in content


def test_a_wholly_meaningless_review_body_is_not_echoed_back() -> None:
    property_obj = make_property()
    review = make_review(property_obj, "9.0", 5010)
    review.public_review = "....."
    review.save(update_fields=["public_review"])

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "....." not in content


def test_the_stored_review_is_never_edited() -> None:
    property_obj = make_property()
    review = make_review(property_obj, "9.0", 5002)

    Client().get(property_obj.get_absolute_url())

    review.refresh_from_db()
    assert review.public_review == "Positive: نظيفة ومريحة"


# --- 9. One rating -----------------------------------------------------------


def test_the_visible_rating_is_the_average_of_the_visible_reviews() -> None:
    property_obj = make_property(average_review_rating=Decimal("9.8"))
    make_review(property_obj, "10.0", 5003)
    make_review(property_obj, "6.0", 5004)

    summary = rating_summary(property_obj)

    assert summary.published_average_out_of_five == Decimal("4.0")
    assert summary.published_count == 2
    assert summary.all_channel_average_out_of_five == Decimal("4.9")


def test_structured_data_matches_what_a_person_reads() -> None:
    from apps.core.seo import property_structured_data

    property_obj = make_property(average_review_rating=Decimal("9.8"))
    make_review(property_obj, "10.0", 5005)
    make_review(property_obj, "6.0", 5006)

    data = property_structured_data(property_obj)
    summary = rating_summary(property_obj)

    assert data["aggregateRating"]["ratingValue"] == float(summary.published_average_out_of_five)
    assert data["aggregateRating"]["reviewCount"] == summary.published_count


def test_no_rating_is_published_without_reviews() -> None:
    from apps.core.seo import property_structured_data

    property_obj = make_property(average_review_rating=Decimal("9.8"))

    assert "aggregateRating" not in property_structured_data(property_obj)


def test_property_cards_use_the_same_published_review_average() -> None:
    property_obj = make_property(average_review_rating=Decimal("9.8"))
    make_review(property_obj, "10.0", 5007)
    make_review(property_obj, "6.0", 5008)

    with translation.override("en"):
        content = Client().get("/properties/").content.decode()

    assert "4.0" in content
    assert "4.9" not in content


def test_property_rating_explains_the_all_channel_figure_in_arabic() -> None:
    property_obj = make_property(average_review_rating=Decimal("9.8"))
    make_review(property_obj, "10.0", 5011)
    make_review(property_obj, "6.0", 5012)

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "٤.٩ من جميع قنوات الحجز" in content
    assert "مراجعتان" in content


# --- 10. Money ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0", "٠٫٠٠ر.س"),
        ("999.5", "٩٩٩٫٥٠ر.س"),
        ("1525", "١٬٥٢٥٫٠٠ر.س"),
        ("1234567.89", "١٬٢٣٤٬٥٦٧٫٨٩ر.س"),
    ],
)
def test_arabic_money_uses_arabic_numerals_separators_and_symbol(
    amount: str,
    expected: str,
) -> None:
    with translation.override("ar"):
        assert strip_tags(format_money(amount, "SAR")) == expected


def test_english_keeps_the_iso_code() -> None:
    with translation.override("en"):
        assert strip_tags(format_money("1525", "SAR")) == "SAR1,525.00"


def test_an_unmapped_currency_keeps_its_iso_code_in_arabic() -> None:
    with translation.override("ar"):
        assert "JPY" in strip_tags(format_money("1525", "JPY"))


def test_format_money_is_the_only_registered_direct_money_filter() -> None:
    from apps.core.templatetags import presentation

    assert presentation.register.filters["format_money"] is format_money
    assert "localized_money" not in presentation.register.filters


def test_indicative_price_copy_uses_the_reviewed_arabic_wording() -> None:
    property_obj = make_property(
        indicative_nightly_from=Decimal("590"),
        indicative_currency="SAR",
    )

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "تبدأ من" in content
    assert "السعر النهائي يتحدد بتواريخك" in content


# --- 11. FAQ -----------------------------------------------------------------


def make_faq(**overrides: object) -> FAQItem:
    defaults = {
        "question_ar": "سؤال",
        "question_en": "Question",
        "answer_ar": "إجابة",
        "answer_en": "Answer",
    }
    return FAQItem.objects.create(**{**defaults, **overrides})


def test_the_faq_page_groups_questions_by_category() -> None:
    make_faq(category=FAQItem.Category.POLICY, question_ar="سؤال الإلغاء")

    response = Client().get("/faq/")

    assert response.status_code == 200
    assert any(group["items"] for group in response.context["faq_groups"])


def test_a_property_question_stays_off_the_general_page() -> None:
    property_obj = make_property()
    make_faq(property=property_obj, question_ar="سؤال خاص بالوحدة")

    content = Client().get("/faq/").content.decode()

    assert "سؤال خاص بالوحدة" not in content


def test_a_property_question_appears_on_its_own_page() -> None:
    property_obj = make_property()
    make_faq(property=property_obj, question_ar="سؤال خاص بالوحدة")

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "سؤال خاص بالوحدة" in content


def test_another_property_never_shows_a_foreign_question() -> None:
    first = make_property(9401)
    second = make_property(9402)
    make_faq(property=first, question_ar="سؤال الوحدة الأولى")

    content = Client().get(second.get_absolute_url()).content.decode()

    assert "سؤال الوحدة الأولى" not in content


def test_faq_uses_admin_managed_default_stay_times() -> None:
    setting = SiteSetting.objects.order_by("pk").first() or SiteSetting()
    setting.default_check_in_hour = 15
    setting.default_check_out_hour = 12
    setting.save()

    content = Client().get("/faq/").content.decode()

    assert "ما أوقات تسجيل الوصول والمغادرة المعتادة؟" in content
    assert "١٥:٠٠" in content
    assert "١٢:٠٠" in content


def test_property_faq_uses_its_actual_times_and_visible_parking() -> None:
    property_obj = make_property(check_in_time_start=16, check_out_time=11)
    parking = Amenity.objects.create(
        name="Free parking",
        name_ar="موقف مجاني",
        icon_key="parking",
        hostaway_amenity_id=701,
    )
    PropertyAmenity.objects.create(property=property_obj, amenity=parking)

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "ما أوقات تسجيل الوصول والمغادرة لهذه الوحدة؟" in content
    assert "هل يتوفر موقف للسيارة في هذه الوحدة؟" in content
    assert "١٦:٠٠" in content
    assert "١١:٠٠" in content


def test_property_faq_never_claims_parking_without_source_data() -> None:
    property_obj = make_property(check_in_time_start=16)

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "هل يتوفر موقف للسيارة في هذه الوحدة؟" not in content
