from django.urls import path

from .views import (
    ApplePayFastAuthorizeView,
    ApplePayFastCreateView,
    ApplePayFastResultView,
    CurrencyPreferenceView,
    HyperPayBookingCheckoutView,
    HyperPayModificationCheckoutView,
    HyperPayResultView,
    SandboxBookingCheckoutView,
    SandboxModificationCheckoutView,
)

app_name = "payments"

urlpatterns = [
    path("currency/", CurrencyPreferenceView.as_view(), name="set_currency"),
    path(
        "hyperpay/fast/<str:reference>/create/",
        ApplePayFastCreateView.as_view(),
        name="apple_fast_create",
    ),
    path(
        "hyperpay/fast/<str:reference>/authorize/",
        ApplePayFastAuthorizeView.as_view(),
        name="apple_fast_authorize",
    ),
    path("hyperpay/fast/result/", ApplePayFastResultView.as_view(), name="apple_fast_result"),
    path(
        "hyperpay/modification/<str:public_reference>/",
        HyperPayModificationCheckoutView.as_view(),
        name="hyperpay_modification",
    ),
    path(
        "hyperpay/booking/<str:public_reference>/",
        HyperPayBookingCheckoutView.as_view(),
        name="hyperpay_booking",
    ),
    path(
        "hyperpay/result/<uuid:payment_id>/",
        HyperPayResultView.as_view(),
        name="hyperpay_result",
    ),
    path(
        "sandbox/booking/<str:public_reference>/",
        SandboxBookingCheckoutView.as_view(),
        name="sandbox_booking",
    ),
    path(
        "sandbox/modification/<str:public_reference>/",
        SandboxModificationCheckoutView.as_view(),
        name="sandbox_modification",
    ),
]
