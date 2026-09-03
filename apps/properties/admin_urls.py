from django.urls import path

from .admin_views import image_alt_text_editor

app_name = "properties_admin"

urlpatterns = [
    path("admin/properties/alt-text/", image_alt_text_editor, name="image_alt_text"),
]
