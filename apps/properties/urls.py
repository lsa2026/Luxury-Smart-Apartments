from django.urls import path

from .views import (
    PropertyDetailView,
    PropertyGalleryView,
    PropertyListView,
    PropertyReviewListView,
)

app_name = "properties"

urlpatterns = [
    path("", PropertyListView.as_view(), name="list"),
    path("<slug:slug>/gallery/", PropertyGalleryView.as_view(), name="gallery"),
    path("<slug:slug>/reviews/", PropertyReviewListView.as_view(), name="reviews"),
    path("<slug:slug>/", PropertyDetailView.as_view(), name="detail"),
]
