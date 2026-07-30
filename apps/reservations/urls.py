from django.urls import path

from .views import AvailabilitySearchView

app_name = "reservations"

urlpatterns = [
    path(
        "search-availability/",
        AvailabilitySearchView.as_view(),
        name="search_availability",
    ),
]
