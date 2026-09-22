"""Serve Apple's merchant-domain verification file without transforming it."""

from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET


@require_GET
def apple_pay_domain_association(request):
    """Expose the exact Apple-provided bytes at Apple's required well-known URL."""
    association_file = Path(settings.APPLE_PAY_DOMAIN_ASSOCIATION_FILE)
    try:
        content = association_file.read_bytes()
    except OSError as exc:
        raise Http404 from exc

    if not content:
        raise Http404

    response = HttpResponse(content, content_type="text/plain")
    response["Cache-Control"] = "public, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response
