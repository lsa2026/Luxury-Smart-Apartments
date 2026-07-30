"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.reservations.views import AvailabilitySearchView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path(
        "properties/search-availability/",
        AvailabilitySearchView.as_view(),
        name="legacy_search_availability",
    ),
    path("properties/", include("apps.properties.urls")),
    path("reservations/", include("apps.reservations.urls")),
    path("integrations/", include("apps.integrations.urls")),
    path("reviews/", include("apps.reviews.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
