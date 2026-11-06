"""Read-only financial result visibility on the existing operations screen."""

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from apps.payments.models import MorningPaymentAttempt
from tests.test_morning_stays import create
from tests.test_morning_stays import isolated as isolated

pytestmark = pytest.mark.django_db


def test_owner_reads_result_without_guest_access_or_provider_calls():
    stay = create()
    attempt = MorningPaymentAttempt.objects.create(
        stay=stay,
        merchant_transaction_id=str(uuid.uuid4()),
        amount=stay.amount_sar,
        status="failed",
        provider_result_code="800.100.152",
    )
    url = reverse("notifications:morning_detail", args=[stay.pk])
    anonymous = Client().get(url)
    assert anonymous.status_code in {302, 403} and b"800.100.152" not in anonymous.content
    client = Client()
    client.force_login(stay.created_by)
    page = client.get(url)
    assert page.status_code == 200
    assert b"800.100.152" in page.content
    assert "رمز نتيجة HyperPay".encode() in page.content
    attempt.refresh_from_db()
    stay.refresh_from_db()
    assert attempt.status == "failed" and stay.paid_at is None
    assert MorningPaymentAttempt.objects.count() == 1
