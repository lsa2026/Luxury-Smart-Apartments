from django.urls import path

from .views import (
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
