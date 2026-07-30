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
    path("i18n/", include("django.conf.urls.i18n")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler400 = "apps.core.views.error_400"
handler403 = "apps.core.views.error_403"
handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"
