"""What a guest needs to read before paying: times, refunds, and house rules.

This is the display counterpart of ``host_policy``, which enforces the same
values at booking time. Nothing here invents a rule:

* arrival and departure hours come from Hostaway, with a site-wide default used
  only when Hostaway reported none;
* the refund ladder is rendered from the tiers the administration entered, so an
  empty table honestly shows "no automatic refund" rather than a friendly
  sentence nobody agreed to;
* house rules are administration-written content, never machine-translated from
  the host's English.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils.translation import get_language

from apps.core.models import SiteSetting
from apps.properties.models import Property
from apps.reservations.models import CancellationPolicyTier


@dataclass(frozen=True)
class RefundTier:
    hours_before: int
    percentage: Decimal
    refunds_cleaning_fee: bool
    note: str


@dataclass(frozen=True)
class StayPolicy:
    check_in_hour: int | None
    check_out_hour: int | None
    check_in_is_default: bool
    check_out_is_default: bool
    house_rules: str
    refund_tiers: list[RefundTier] = field(default_factory=list)
    policy_code: str = ""

    @property
    def has_times(self) -> bool:
        return self.check_in_hour is not None or self.check_out_hour is not None

    @property
    def has_refund_ladder(self) -> bool:
        return bool(self.refund_tiers)

    @property
    def is_empty(self) -> bool:
        """True when there is nothing worth rendering a section for."""
        return not (self.has_times or self.has_refund_ladder or self.house_rules)


def _language() -> str:
    return (get_language() or "ar").split("-")[0]


def _localized(obj: object, prefix: str) -> str:
    """Read ``prefix_<language>`` with the site's usual fallback order."""
    order = {"fr": ("fr", "en", "ar"), "en": ("en", "fr", "ar")}.get(
        _language(), ("ar", "en", "fr")
    )
    for language in order:
        value = (getattr(obj, f"{prefix}_{language}", "") or "").strip()
        if value:
            return value
    return ""


def _valid_hour(value: object) -> int | None:
    """Hostaway reports an hour of the day; 24 means midnight at the end."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 0 <= value <= 24 else None


def refund_tiers_for(policy_code: str) -> list[RefundTier]:
    """The administration's ladder for one Hostaway policy name, best first."""
    code = (policy_code or "").strip()
    if not code:
        return []
    rows = CancellationPolicyTier.objects.filter(
        policy_code__iexact=code,
        is_active=True,
    ).order_by("-min_hours_before_check_in")
    return [
        RefundTier(
            hours_before=row.min_hours_before_check_in,
            percentage=row.refund_percentage,
            refunds_cleaning_fee=row.refunds_cleaning_fee,
            note=row.note,
        )
        for row in rows
    ]


def stay_policy_for(property_obj: Property) -> StayPolicy:
    """Everything the guest should read about this stay before paying."""
    site_setting = SiteSetting.objects.order_by("pk").first()

    listing_check_in = _valid_hour(property_obj.check_in_time_start)
    listing_check_out = _valid_hour(property_obj.check_out_time)
    default_check_in = _valid_hour(getattr(site_setting, "default_check_in_hour", None))
    default_check_out = _valid_hour(getattr(site_setting, "default_check_out_hour", None))

    check_in = listing_check_in if listing_check_in is not None else default_check_in
    check_out = listing_check_out if listing_check_out is not None else default_check_out

    house_rules = _localized(property_obj, "house_rules")
    if not house_rules and site_setting is not None:
        house_rules = _localized(site_setting, "default_house_rules")

    return StayPolicy(
        check_in_hour=check_in,
        check_out_hour=check_out,
        check_in_is_default=listing_check_in is None and check_in is not None,
        check_out_is_default=listing_check_out is None and check_out is not None,
        house_rules=house_rules,
        refund_tiers=refund_tiers_for(property_obj.cancellation_policy),
        policy_code=(property_obj.cancellation_policy or "").strip(),
    )
