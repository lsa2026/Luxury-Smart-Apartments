from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from .password_reset import LuxurySetPasswordForm, QueuedPasswordResetForm
from .views import (
    LoginView,
    LogoutView,
    RegisterView,
    dashboard,
    resend_verification,
    verify_email,
    verify_pending,
)

app_name = "accounts"

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("my-bookings/", dashboard, name="dashboard"),
    path("account/confirm/", verify_pending, name="verify_pending"),
    path("account/confirm/resend/", resend_verification, name="resend_verification"),
    path("account/verify/<str:token>/", verify_email, name="verify_email"),
    # Django's own reset views, given this project's forms and templates. The
    # form is what routes the message through the site's email queue.
    path(
        "account/reset/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset.html",
            form_class=QueuedPasswordResetForm,
            success_url=reverse_lazy("accounts:password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "account/reset/sent/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="accounts/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    path(
        "account/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            form_class=LuxurySetPasswordForm,
            success_url=reverse_lazy("accounts:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "account/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
]
