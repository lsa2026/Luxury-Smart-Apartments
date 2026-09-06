"""Times, cancellation and house rules, as a guest reads them before paying."""

from decimal import Decimal

import pytest
from django.test import Client
from django.utils import translation

from apps.core.models import SiteSetting
from apps.properties.models import Property
from apps.reservations.models import CancellationPolicyTier
from apps.reservations.services.stay_policy import stay_policy_for

pytestmark = pytest.mark.django_db


def make_property(**overrides: object) -> Property:
    defaults = {
        "hostaway_listing_id": 8801,
        "slug": "policy-unit",
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


def set_site_defaults(**values: object) -> SiteSetting:
    """SiteSetting is a singleton seeded by migration; update it, do not add one."""
    setting = SiteSetting.objects.order_by("pk").first() or SiteSetting()
    for name, value in values.items():
        setattr(setting, name, value)
    setting.save()
    return setting


def make_tier(policy: str, hours: int, percentage: str, **overrides: object) -> None:
    CancellationPolicyTier.objects.create(
        policy_code=policy,
        min_hours_before_check_in=hours,
        refund_percentage=Decimal(percentage),
        **overrides,
    )


# --- ownership of each value ------------------------------------------------


def test_arrival_hours_come_from_the_channel_manager() -> None:
    property_obj = make_property(check_in_time_start=16, check_out_time=11)

    policy = stay_policy_for(property_obj)

    assert policy.check_in_hour == 16
    assert policy.check_out_hour == 11
    assert policy.check_in_is_default is False


def test_locally_managed_hours_override_the_channel_manager_for_display() -> None:
    property_obj = make_property(
        display_check_in_hour=17,
        display_check_out_hour=10,
        check_in_time_start=16,
        check_out_time=11,
    )

    policy = stay_policy_for(property_obj)

    assert policy.check_in_hour == 17
    assert policy.check_out_hour == 10
    assert policy.check_in_is_default is False
    assert policy.check_out_is_default is False


def test_the_site_default_fills_only_what_the_source_omitted() -> None:
    set_site_defaults(default_check_in_hour=15, default_check_out_hour=12)
    property_obj = make_property(check_in_time_start=16, check_out_time=None)

    policy = stay_policy_for(property_obj)

    assert policy.check_in_hour == 16
    assert policy.check_in_is_default is False
    assert policy.check_out_hour == 12
    assert policy.check_out_is_default is True


def test_an_unknown_hour_stays_unknown_rather_than_invented() -> None:
    property_obj = make_property(check_in_time_start=None, check_out_time=None)

    policy = stay_policy_for(property_obj)

    assert policy.check_in_hour is None
    assert policy.check_out_hour is None


def test_an_out_of_range_hour_is_refused() -> None:
    property_obj = make_property(check_in_time_start=99)

    assert stay_policy_for(property_obj).check_in_hour is None


# --- the refund ladder ------------------------------------------------------


def test_the_ladder_reads_the_administration_table_best_tier_first() -> None:
    property_obj = make_property(cancellation_policy="flexible")
    make_tier("flexible", 24, "50")
    make_tier("flexible", 72, "100")

    tiers = stay_policy_for(property_obj).refund_tiers

    assert [tier.hours_before for tier in tiers] == [72, 24]
    assert tiers[0].percentage == Decimal("100.00")


def test_an_unconfigured_policy_promises_no_refund() -> None:
    """The security of the business case: an empty table must not read as 100%."""
    property_obj = make_property(cancellation_policy="flexible")

    policy = stay_policy_for(property_obj)

    assert policy.refund_tiers == []
    assert policy.has_refund_ladder is False


def test_an_inactive_tier_is_not_shown() -> None:
    property_obj = make_property(cancellation_policy="flexible")
    make_tier("flexible", 24, "50", is_active=False)

    assert stay_policy_for(property_obj).refund_tiers == []


def test_tiers_of_another_policy_never_leak_in() -> None:
    property_obj = make_property(cancellation_policy="firm")
    make_tier("flexible", 24, "100")

    assert stay_policy_for(property_obj).refund_tiers == []


# --- house rules ------------------------------------------------------------


def test_cancellation_policy_prefers_property_content_over_the_site_default() -> None:
    set_site_defaults(default_cancellation_policy_ar="سياسة الموقع")
    property_obj = make_property(cancellation_policy_ar="سياسة الوحدة")

    with translation.override("ar"):
        assert stay_policy_for(property_obj).cancellation_policy_text == "سياسة الوحدة"


def test_cancellation_policy_falls_back_to_the_site_default() -> None:
    set_site_defaults(default_cancellation_policy_ar="سياسة الموقع")
    property_obj = make_property()

    with translation.override("ar"):
        assert stay_policy_for(property_obj).cancellation_policy_text == "سياسة الموقع"


def test_house_rules_prefer_the_property_over_the_site_default() -> None:
    set_site_defaults(default_house_rules_ar="قواعد الموقع")
    property_obj = make_property(house_rules_ar="قواعد الوحدة")

    with translation.override("ar"):
        assert stay_policy_for(property_obj).house_rules == "قواعد الوحدة"


def test_house_rules_fall_back_to_the_site_default() -> None:
    set_site_defaults(default_house_rules_ar="قواعد الموقع")
    property_obj = make_property()

    with translation.override("ar"):
        assert stay_policy_for(property_obj).house_rules == "قواعد الموقع"


def test_nothing_configured_renders_no_section() -> None:
    property_obj = make_property()

    assert stay_policy_for(property_obj).is_empty is True


# --- what the pages show ----------------------------------------------------


def test_the_property_page_shows_the_three_answers() -> None:
    property_obj = make_property(
        check_in_time_start=16,
        check_out_time=11,
        cancellation_policy="flexible",
        cancellation_policy_ar="يمكن الإلغاء وفق السياسة المعروضة.",
        house_rules_ar="ممنوع التدخين داخل الوحدة.",
    )
    make_tier("flexible", 48, "100")

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert "stay-policy" in content
    assert "١٦:٠٠" in content
    assert "١١:٠٠" in content
    assert "يمكن الإلغاء وفق السياسة المعروضة." in content
    assert "ممنوع التدخين داخل الوحدة." in content


def test_the_property_page_omits_the_section_when_nothing_is_configured() -> None:
    property_obj = make_property()

    content = Client().get(property_obj.get_absolute_url()).content.decode()

    assert 'id="stay-policy-title"' not in content


def test_the_quote_review_summarizes_managed_policy_and_house_rules() -> None:
    from tests.test_booking_views_admin import owned_client_quote

    client, quote, reference = owned_client_quote()
    quote.property.cancellation_policy_ar = "إلغاء مُدار من لوحة التحكم."
    quote.property.house_rules_ar = "قواعد مُدارة من لوحة التحكم."
    quote.property.display_check_in_hour = 17
    quote.property.save(
        update_fields=(
            "cancellation_policy_ar",
            "house_rules_ar",
            "display_check_in_hour",
        )
    )

    content = client.get(f"/reservations/quotes/{reference}/").content.decode()

    assert "إلغاء مُدار من لوحة التحكم." in content
    assert "قواعد مُدارة من لوحة التحكم." in content
    assert "١٧:٠٠" in content
    assert "سياسة الإلغاء وقواعد المنزل المعروضة لهذه الإقامة" in content
