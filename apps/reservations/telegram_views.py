from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import telegram_activation
from .security import is_rate_limited
from .views import _owned_reservation


@never_cache
@require_POST
def activate(request, public_reference):
    request._disable_google_integrations = True
    reservation = _owned_reservation(request, public_reference)
    if is_rate_limited(request, scope="telegram-activation", requests=10, window=300):
        response = telegram_activation.result_response(request, {"state": "hold", "url": ""})
        response.status_code = 429
        return response
    return telegram_activation.result_response(
        request, telegram_activation.issue(request, reservation)
    )
