from django.urls import path

from .live_directions import PropertyAirportRouteView
from .price_calendar import PropertyPriceCalendarView
from .views import (
    PropertyDetailView,
    PropertyGalleryView,
    PropertyListView,
    PropertyReviewListView,
)

app_name = "properties"

urlpatterns = [
    path("", PropertyListView.as_view(), name="list"),
    path("<slug:slug>/airport-route/", PropertyAirportRouteView.as_view(), name="airport_route"),
    path("<slug:slug>/price-calendar/", PropertyPriceCalendarView.as_view(), name="price_calendar"),
    path("<slug:slug>/gallery/", PropertyGalleryView.as_view(), name="gallery"),
    path("<slug:slug>/reviews/", PropertyReviewListView.as_view(), name="reviews"),
    path("<slug:slug>/", PropertyDetailView.as_view(), name="detail"),
]
