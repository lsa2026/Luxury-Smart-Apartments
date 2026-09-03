from django.urls import path

from .views import LoginView, LogoutView, RegisterView, dashboard

app_name = "accounts"

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("my-bookings/", dashboard, name="dashboard"),
]
