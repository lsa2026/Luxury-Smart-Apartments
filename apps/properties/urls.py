from django.urls import path

from .views import PropertyDetailView, PropertyGalleryView, PropertyListView

app_name = "properties"

urlpatterns = [
    path("", PropertyListView.as_view(), name="list"),
    path("<slug:slug>/gallery/", PropertyGalleryView.as_view(), name="gallery"),
    path("<slug:slug>/", PropertyDetailView.as_view(), name="detail"),
]
