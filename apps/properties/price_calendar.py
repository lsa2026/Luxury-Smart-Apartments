"""Local, read-only price calendar. No provider request on a guest visit."""

from datetime import date, timedelta

from django.http import Http404, HttpRequest, JsonResponse
from django.utils import timezone
from django.views import View

from .models import PropertyPriceCalendar

SNAPSHOT_MAX_AGE = timedelta(hours=48)


class PropertyPriceCalendarView(View):
    http_method_names = ["get"]

    def get(self, request: HttpRequest, slug: str) -> JsonResponse:
        try:
            start = date.fromisoformat(request.GET["start"])
            end = date.fromisoformat(request.GET["end"])
        except (KeyError, TypeError, ValueError):
            return JsonResponse({"detail": "invalid_date_range"}, status=400)
        today = timezone.localdate()
        if start < today or end <= start or (end - start).days > 93:
            return JsonResponse({"detail": "invalid_date_range"}, status=400)

        snapshot = (
            PropertyPriceCalendar.objects.filter(
                property__slug=slug,
                property__is_visible=True,
                property__hostaway_is_active=True,
            )
            .only("currency", "start_date", "end_date", "days", "fetched_at")
            .first()
        )
        if snapshot is None:
            # Also covers a new listing before its first successful daily sync.
            raise Http404("Price calendar not ready.")

        if timezone.now() - snapshot.fetched_at > SNAPSHOT_MAX_AGE:
            response = JsonResponse(
                {"detail": "snapshot_outdated", "updated_at": snapshot.fetched_at.isoformat()},
                status=503,
            )
            response["Cache-Control"] = "no-store"
            return response
        start_key, end_key = start.isoformat(), end.isoformat()
        response = JsonResponse(
            {
                "currency": snapshot.currency,
                "updated_at": snapshot.fetched_at.isoformat(),
                "today": today.isoformat(),
                "coverage_end": snapshot.end_date.isoformat(),
                "days": [day for day in snapshot.days if start_key <= day["date"] < end_key],
            }
        )
        response["Cache-Control"] = "private, max-age=300"
        response["X-Robots-Tag"] = "noindex"
        return response
