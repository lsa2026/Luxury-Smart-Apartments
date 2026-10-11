from django.urls import path

from . import views
from .operations_ui import settlement_command

app_name = "loyalty"
urlpatterns = [
    path("settlement/", settlement_command, name="settlement"),
    path("", views.admin_workspace, name="admin"),
    path("tree/", views.admin_tree, name="tree"),
    path("arrival/", views.admin_arrival, name="arrival"),
]
