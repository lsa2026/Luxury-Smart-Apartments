from django.urls import path

from .views import (
    AvailabilitySearchView,
    BookingIntentDetailView,
    BookingQuoteDetailView,
    GuestDetailsView,
    ModificationCreateView,
    ModificationDetailView,
    ReservationManageView,
)

app_name = "reservations"

urlpatterns = [
    path("quotes/", AvailabilitySearchView.as_view(), name="quote_create"),
    path(
        "quotes/<str:reference>/",
        BookingQuoteDetailView.as_view(),
        name="quote_detail",
    ),
    path(
        "quotes/<str:reference>/guest-details/",
        GuestDetailsView.as_view(),
        name="guest_details",
    ),
    path(
        "requests/<str:public_reference>/",
        BookingIntentDetailView.as_view(),
        name="intent_detail",
    ),
    path(
        "manage/<str:public_reference>/",
        ReservationManageView.as_view(),
        name="manage",
    ),
    path(
        "manage/<str:public_reference>/<str:action>/",
        ModificationCreateView.as_view(),
        name="modification_create",
    ),
    path(
        "modifications/<str:public_reference>/",
        ModificationDetailView.as_view(),
        name="modification_detail",
    ),
]
