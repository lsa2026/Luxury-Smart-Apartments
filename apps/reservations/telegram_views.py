from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import telegram_activation
from .views import _owned_reservation


@never_cache
@require_POST
def activate(request, public_reference):
    request._disable_google_integrations = True
    reservation = _owned_reservation(request, public_reference)
    return telegram_activation.result_response(
        request, telegram_activation.issue(request, reservation)
    )
