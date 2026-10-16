"""Optional existing address search, isolated to the signed-in account/session."""

from dataclasses import dataclass

from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator

from apps.reservations.address_places import CheckoutAddressView, make_address_ticket
from apps.reservations.security import session_key_hash


@dataclass(frozen=True)
class AccountAddressScope:
    pk: str
    session_key_hash: str


def account_address_scope(request):
    # A different identity namespace keeps checkout tickets unusable here and
    # binds this ticket to both the current account and its browser session.
    return AccountAddressScope(f"guest-account:{request.user.pk}", session_key_hash(request))


def account_address_ticket(request):
    return make_address_ticket(account_address_scope(request))


@method_decorator(login_required(login_url="accounts:login"), name="dispatch")
class AccountAddressView(CheckoutAddressView):
    """Reuse provider masks, quotas, expiry and selection checks; never save."""

    def get_address_scope(self, request, reference):
        return account_address_scope(request)


class AccountAddressDetailsView(AccountAddressView):
    details = True
