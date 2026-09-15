"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.core.admin_views import customer_overview
from apps.core.seo import robots_txt, sitemap_xml
from apps.core.views import service_worker
from apps.reservations.views import AvailabilitySearchView

urlpatterns = [
    path("", include("apps.accounts.urls")),
    path("payments/", include("apps.payments.urls")),
    path("", include("apps.notifications.urls")),
    path("", include("apps.core.marketing_urls")),
    path("", include("apps.properties.admin_urls")),
    path("admin/customers/", customer_overview, name="admin_customers"),
    path("admin/", admin.site.urls),
    path("service-worker.js", service_worker, name="service_worker"),
    path("sitemap.xml", sitemap_xml, name="sitemap"),
    path("robots.txt", robots_txt, name="robots"),
    path(
        "properties/search-availability/",
        AvailabilitySearchView.as_view(),
        name="legacy_search_availability",
    ),
    path("reservations/", include("apps.reservations.urls")),
    path("integrations/", include("apps.integrations.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
]

urlpatterns += i18n_patterns(
    path("", include("apps.core.urls")),
    path("properties/", include("apps.properties.urls")),
    path("reviews/", include("apps.reviews.urls")),
    prefix_default_language=True,
)

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler400 = "apps.core.views.error_400"
handler403 = "apps.core.views.error_403"
handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"
