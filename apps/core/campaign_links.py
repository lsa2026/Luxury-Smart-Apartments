"""Public branded campaign links; no message, guest, or booking data is collected."""

from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.http import require_safe

MISSED_CALL_CAMPAIGN = {
    "utm_source": "whatsapp",
    "utm_medium": "messaging",
    "utm_campaign": "missed_call",
    "utm_content": "scenario_05",
}


@require_safe
def missed_call_welcome_redirect(request: HttpRequest) -> HttpResponse:
    """Send visitors to their localized homepage with fixed, non-personal UTMs.

    Never forward incoming query parameters: callers cannot change the destination
    or add a guest's phone/email to the analytics URL. This redirect itself is not
    a GA event; the existing consent-aware homepage tag handles collection.
    """
    destination = f"{reverse('core:home')}?{urlencode(MISSED_CALL_CAMPAIGN)}"
    response = HttpResponseRedirect(destination)
    response["Cache-Control"] = "no-store, max-age=0"
    response["X-Robots-Tag"] = "noindex"
    return response
