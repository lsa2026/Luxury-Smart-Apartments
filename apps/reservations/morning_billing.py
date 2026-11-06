"""Reuse checkout address validation; management never accepts guest terms."""

from apps.payments.payment_link_views import PaymentLinkBillingForm


class MorningBillingForm(PaymentLinkBillingForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in list(self.fields):
            if not name.startswith("billing_"):
                self.fields.pop(name)


def saved_billing(stay):
    form = MorningBillingForm(stay.billing_address)
    return dict(form.cleaned_data) if form.is_valid() else None


def address_scope(request):
    from types import SimpleNamespace

    from .security import session_key_hash

    return SimpleNamespace(
        pk=f"morning-admin:{request.user.pk}", session_key_hash=session_key_hash(request)
    )


def address_context(request):
    from django.urls import reverse

    from .address_places import make_address_ticket, places_enabled

    if not places_enabled():
        return {}
    return {
        "address_places_enabled": True,
        "address_places_ticket": make_address_ticket(address_scope(request)),
        "address_suggestions_url": reverse("notifications:morning_address", args=["suggestions"]),
        "address_details_url": reverse("notifications:morning_address", args=["details"]),
    }


def address_view(request, kind):
    from .address_places import CheckoutAddressView

    class AdminAddressView(CheckoutAddressView):
        def get_address_scope(self, request, reference):
            return address_scope(request)

    return AdminAddressView.as_view(details=kind == "details")(request, reference="morning-admin")
