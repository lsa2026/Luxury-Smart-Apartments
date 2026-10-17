from django.urls import path

from . import views
from .booking_operations import booking_stay
from .identity_ui import admin_identity
from .operations_ui import settlement_command
from .referral_ui import admin_referrals

app_name = "loyalty"
urlpatterns = [
    path("identity/", admin_identity, name="identity"),
    path("referrals/", admin_referrals, name="referrals"),
    path("booking/<uuid:reservation_id>/stay/", booking_stay, name="booking_stay"),
    path("settlement/", settlement_command, name="settlement"),
    path("", views.admin_workspace, name="admin"),
    path("tree/", views.admin_tree, name="tree"),
    path("arrival/", views.admin_arrival, name="arrival"),
]
