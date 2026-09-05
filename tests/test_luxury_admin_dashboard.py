import pytest
from django.urls import reverse

from apps.core.models import ContactMessage
from apps.payments.models import PaymentAttempt
from tests.test_booking_modifications_phase6 import confirmed_reservation


@pytest.mark.django_db
def test_luxury_dashboard_renders_for_superuser(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="owner",
        email="owner@example.com",
        password="strong-password",
    )
    client.force_login(user)

    response = client.get(reverse("admin:index"))

    assert response.status_code == 200
    assert "مركز القيادة" in response.content.decode()
    assert "Hostaway" in response.content.decode()
    assert reverse("admin_customers") in response.content.decode()
    assert "أولويات تحتاج قرارًا" in response.content.decode()
    assert "خطة الإقامات" in response.content.decode()
    assert "مسار الحجز" in response.content.decode()
    assert "التحصيل المالي" in response.content.decode()


@pytest.mark.django_db
def test_dashboard_surfaces_actionable_customer_contact(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="duty-manager",
        email="duty-manager@example.com",
        password="strong-password",
    )
    ContactMessage.objects.create(
        name="عميل تجريبي",
        email="guest@example.test",
        subject="استفسار قبل الوصول",
        message="رسالة اختبار للوحة العمليات.",
        language="ar",
    )
    client.force_login(user)

    response = client.get(reverse("admin:index"))
    body = response.content.decode()

    assert response.status_code == 200
    assert "رسائل عملاء تنتظر الرد" in body
    assert "حوّل الرسالة إلى قيد المتابعة" in body
    assert reverse("admin:core_contactmessage_changelist") in body
    assert reverse("admin:core_siteinterfaceimage_changelist") in body


@pytest.mark.django_db
def test_customer_overview_combines_booking_and_payment_filters(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="operations",
        email="operations@example.com",
        password="strong-password",
    )
    client.force_login(user)

    response = client.get(
        reverse("admin_customers"),
        {"status": "awaiting_payment", "payment": "pending"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "العملاء والحجوزات" in body
    assert "كل حالات الدفع" in body


@pytest.mark.django_db
def test_customer_overview_requires_staff(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="guest",
        password="strong-password",
    )
    client.force_login(user)

    response = client.get(reverse("admin_customers"))

    assert response.status_code == 302
    assert reverse("admin:login") in response.url


@pytest.mark.django_db
def test_payment_attempt_admin_is_registered(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="finance",
        email="finance@example.com",
        password="strong-password",
    )
    client.force_login(user)

    response = client.get(reverse("admin:payments_paymentattempt_changelist"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_reservation_detail_is_read_only_and_uses_operational_arabic_labels(
    client, django_user_model
):
    user = django_user_model.objects.create_superuser(
        username="stay-manager",
        email="stay-manager@example.com",
        password="strong-password",
    )
    reservation = confirmed_reservation()
    client.force_login(user)

    response = client.get(reverse("admin:reservations_reservation_change", args=(reservation.pk,)))
    body = response.content.decode()

    assert response.status_code == 200
    assert "ملخص الإقامة" in body
    assert "حالة الحجز والدفع" in body
    assert "الربط التقني مع Hostaway" in body
    assert "إدارة الحجز بأمان" in body
    assert "عرض طلبات التعديل والإلغاء" in body
    assert 'name="_save"' not in body
    assert 'name="_continue"' not in body


@pytest.mark.django_db
def test_booking_request_detail_is_a_compact_arabic_read_only_summary(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="request-manager",
        email="request-manager@example.com",
        password="strong-password",
    )
    reservation = confirmed_reservation()
    client.force_login(user)

    response = client.get(
        reverse(
            "admin:reservations_bookingintent_change",
            args=(reservation.booking_intent_id,),
        )
    )
    body = response.content.decode()

    assert response.status_code == 200
    assert "ملخص طلب الحجز" in body
    assert "بيانات الضيف المحمية" in body
    assert "Public reference" not in body
    assert 'name="_save"' not in body


@pytest.mark.django_db
def test_payment_list_hides_provider_noise_and_formats_money(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="payment-manager",
        email="payment-manager@example.com",
        password="strong-password",
    )
    reservation = confirmed_reservation()
    PaymentAttempt.objects.create(
        booking_intent_id=reservation.booking_intent_id,
        provider="hyperpay",
        provider_reference="synthetic-provider-reference",
        merchant_transaction_id="synthetic-merchant-reference",
        amount=reservation.total_price,
        currency=reservation.currency,
        status=PaymentAttempt.Status.SUCCEEDED,
        idempotency_key="admin-payment-list-test",
    )
    client.force_login(user)

    response = client.get(reverse("admin:payments_paymentattempt_changelist"))
    body = response.content.decode()

    assert response.status_code == 200
    assert "synthetic-provider-reference" not in body
    assert "synthetic-merchant-reference" not in body
    assert f"{reservation.total_price:,.2f}" in body


@pytest.mark.django_db
def test_admin_action_checkbox_survives_localized_result_headers(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="localized-admin",
        email="localized-admin@example.com",
        password="strong-password",
    )
    client.force_login(user)

    response = client.get(reverse("admin:auth_user_changelist"))
    body = response.content.decode()

    assert response.status_code == 200
    assert 'id="action-toggle"' in body
    assert '&lt;input type="checkbox" id="action-toggle"' not in body


@pytest.mark.django_db
def test_admin_shell_exposes_permission_aware_command_center(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="command-admin",
        email="command-admin@example.com",
        password="strong-password",
    )
    client.force_login(user)

    response = client.get(reverse("admin:index"))
    body = response.content.decode()

    assert response.status_code == 200
    assert "كل الأدوات" in body
    assert "التعديل والتمديد" in body
    assert "الفريق والصلاحيات" in body
