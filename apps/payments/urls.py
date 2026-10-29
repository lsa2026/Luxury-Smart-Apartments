from django.urls import path

from .morning_views import MorningPaymentResultView, MorningPaymentView
from .payment_link_views import ReservationPaymentAddressView, ReservationPaymentView
from .purchase_receipts import payment_link_receipt
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
    path("receipt/", payment_link_receipt, name="payment_link_receipt"),
    path("morning-pay/<str:token>/", MorningPaymentView.as_view(), name="morning_payment"),
    path(
        "hyperpay/morning-result/<uuid:payment_id>/",
        MorningPaymentResultView.as_view(),
        name="morning_result",
    ),
    path("pay/<str:token>/", ReservationPaymentView.as_view(), name="reservation_payment"),
    path(
        "pay/<str:reference>/address-suggestions/",
        ReservationPaymentAddressView.as_view(),
        name="reservation_address_suggestions",
    ),
    path(
        "pay/<str:reference>/address-details/",
        ReservationPaymentAddressView.as_view(details=True),
        name="reservation_address_details",
    ),
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
