"""Serve the Apple-provided merchant-domain association without redirects."""

from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_safe


@require_safe
def apple_pay_domain_association(request):
    filename = settings.APPLE_PAY_DOMAIN_ASSOCIATION_FILE
    if not filename:
        raise Http404
    try:
        content = Path(filename).read_bytes()
    except OSError as exc:
        raise Http404 from exc
    if not content:
        raise Http404
    response = HttpResponse(content, content_type="text/plain")
    response["Cache-Control"] = "public, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response
