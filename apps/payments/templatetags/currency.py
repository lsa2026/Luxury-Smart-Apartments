"""Render structured source money in the visitor's display-only currency."""

from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import format_html
from django.utils.translation import gettext as _

from apps.core.templatetags.presentation import format_money
from apps.payments.currency import (
    CurrencyError,
    CurrencyService,
    normalize_currency,
    rates_from_audit_snapshot,
)

register = template.Library()


@register.filter
def nightly_average(total: object, nights: object) -> Decimal | str:
    """Present the quote total per night without changing its authority."""
    try:
        night_count = int(nights)
        if night_count < 1:
            return ""
        return Decimal(str(total)) / Decimal(night_count)
    except (InvalidOperation, TypeError, ValueError):
        return ""


@register.simple_tag(takes_context=True)
def display_money(
    context: template.Context,
    value: object,
    source_currency: object,
    audit_snapshot: object = None,
) -> str:
    """Convert server-side only; on failure show the explicit source amount."""

    try:
        source = normalize_currency(source_currency)
        display = normalize_currency(context.get("display_currency", "SAR"))
        if source == display:
            return format_money(value, source)
        if audit_snapshot:
            rates = rates_from_audit_snapshot(audit_snapshot)
        else:
            rates = None
        if rates is None or source not in rates.rates or display not in rates.rates:
            request = context.get("request")
            live_rates = getattr(request, "_fx_display_rates", None)
            if live_rates is None:
                with CurrencyService() as service:
                    live_rates = service.get_rates()
                if request is not None:
                    request._fx_display_rates = live_rates
            rates = live_rates
        amount_sar = CurrencyService.source_to_sar(value, source, rates)
        converted = CurrencyService.sar_to_display(amount_sar, display, rates)
        return format_money(converted, display)
    except CurrencyError:
        return format_html(
            '<span class="money-conversion-unavailable" title="{}">{}</span>',
            _("Currency conversion is temporarily unavailable; the original price is shown."),
            format_money(value, source_currency),
        )
