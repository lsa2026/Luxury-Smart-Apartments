"""One fictional unpaid checkout, real authentication views, offline providers."""

from datetime import UTC, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import VerifiedPhone, profile_for
from apps.accounts.tokens import make_verification_code
from apps.loyalty.checkout_account import PENDING, TEXT
from apps.loyalty.models import Ledger
from apps.loyalty.services import available
from apps.payments.currency import CurrencyService
from apps.payments.models import PaymentAttempt
from apps.reservations.models import BookingIntent
from tests.test_hyperpay import own_intent, payable_intent
from tests.test_loyalty_stage8 import booking, join, reconcile, user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def offline(settings, monkeypatch):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_PILOT_ONLY = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = "test-not-published-v1"
    settings.LOYALTY_TERMS_REFERENCE = "/test-only-terms/"
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    settings.LOYALTY_WHATSAPP_NOTICES_ENABLED = False
    settings.EMAIL_DELIVERY_ENABLED = False
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 10, 13, tzinfo=UTC))
    monkeypatch.setattr("apps.accounts.views.queue_verification_email", lambda *a, **kw: None)
    monkeypatch.setattr("apps.accounts.views.queue_welcome_email", lambda *a, **kw: None)
    cache.clear()


@pytest.fixture
def journey():
    guest = user("checkout-resume")
    member = join(guest)
    reconcile(booking(guest, "8333.34").pk)
    intent = payable_intent()
    intent.total_price = 950
    with CurrencyService() as currency:
        quote = currency.create_quote(
            source_amount=intent.total_price,
            source_currency="SAR",
            display_currency="SAR",
            quote_created_at=intent.created_at,
            quote_expires_at=intent.expires_at,
        )
    intent.payment_amount_sar = quote.payment_amount_sar
    intent.exchange_rate_snapshot = dict(quote.snapshot)
    intent.customer = None
    intent.guest_email = guest.email
    intent.save()
    client = Client()
    own_intent(client, intent)
    return guest, member, intent, client


def detail(intent):
    return reverse("reservations:intent_detail", args=[intent.public_reference])


def action(intent):
    return reverse("reservations:booking_account", args=[intent.public_reference])


def test_anonymous_prompt_preserves_cash_option_and_only_posts_intent(journey):
    _, _, intent, client = journey
    page = client.get(detail(intent))
    assert page.status_code == 200 and "no-store" in page["Cache-Control"]
    assert TEXT["ar"]["login"] in page.content.decode()
    assert action(intent) in page.content.decode()
    before = Ledger.objects.count()
    response = client.post(action(intent), {"next": "https://evil.invalid/"})
    parsed = urlparse(response.url)
    assert parsed.path == reverse("accounts:login")
    assert parse_qs(parsed.query)["next"] == [action(intent)]
    intent.refresh_from_db()
    assert intent.customer_id is None and Ledger.objects.count() == before
    assert not PaymentAttempt.objects.filter(booking_intent=intent).exists()


def test_email_code_return_links_same_request_and_allows_points(journey):
    guest, member, intent, client = journey
    original = (
        intent.property_id,
        intent.check_in,
        intent.check_out,
        intent.total_price,
        intent.guest_email,
        intent.guest_phone,
        intent.expires_at,
        intent.legal_acceptance,
    )
    before = Ledger.objects.count()
    login_url = client.post(action(intent)).url
    assert client.get(login_url).status_code == 200
    response = client.post(login_url, {"email": guest.email, "next": action(intent)})
    assert response.status_code == 302
    profile = profile_for(guest)
    profile.refresh_from_db()
    code = make_verification_code(guest.pk, guest.email, profile.verification_sent_at)
    response = client.post(reverse("accounts:verify_email_code"), {"code": code})
    assert response.url == action(intent)
    page = client.get(response.url, follow=True)
    assert page.status_code == 200 and page.request["PATH_INFO"] == detail(intent)
    intent.refresh_from_db()
    assert intent.customer_id == guest.pk
    assert original == (
        intent.property_id,
        intent.check_in,
        intent.check_out,
        intent.total_price,
        intent.guest_email,
        intent.guest_phone,
        intent.expires_at,
        intent.legal_acceptance,
    )
    assert "points_checkout" in page.context
    assert Ledger.objects.count() == before  # No automatic spend or reward.
    response = client.post(
        reverse("reservations:booking_points", args=[intent.public_reference]),
        {"action": "apply", "points": "200.00", "request_reference": "TEST-RESUME-POINTS-01"},
    )
    assert response.status_code == 302
    page = client.get(detail(intent))
    assert page.context["points_checkout"]["cash"] == 750
    assert available(member) == 50


@pytest.mark.parametrize("provider", ["google", "apple", "phone"])
def test_all_signin_routes_receive_same_safe_return(journey, provider, settings, monkeypatch):
    _, _, intent, client = journey
    settings.GOOGLE_SIGN_IN_ENABLED = settings.APPLE_SIGN_IN_ENABLED = True
    settings.SOCIALACCOUNT_PROVIDERS = {
        name: {"APPS": [{"client_id": "isolated-test-client", "secret": "not-a-real-secret"}]}
        for name in ("google", "apple")
    }
    settings.ACCOUNT_WHATSAPP_AUTH_ENABLED = True
    settings.ACCOUNT_WHATSAPP_AUTH_ISOLATED = True
    settings.ACCOUNT_WHATSAPP_AUTH_PUBLIC = True
    monkeypatch.setattr("apps.accounts.views.phone_ready", lambda: True)
    page = client.get(client.post(action(intent)).url)

    class Targets(HTMLParser):
        values = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form" and "action" in attrs:
                self.values.append(attrs["action"])
            if tag == "a" and "href" in attrs:
                self.values.append(attrs["href"])

    targets = Targets()
    targets.values = []
    targets.feed(page.content.decode())
    if provider == "phone":
        target = next(value for value in targets.values if "/account/mobile/?next=" in value)
    else:
        target = next(value for value in targets.values if f"/{provider}/" in value)
    assert parse_qs(urlparse(unescape(target)).query)["next"] == [action(intent)]


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_copy_is_single_guest_language(journey, language):
    _, _, intent, client = journey
    intent.language = language
    intent.save()
    page = client.get(detail(intent))
    assert TEXT[language]["login"] in page.content.decode()
    assert all(
        TEXT[other]["login"] not in page.content.decode() for other in TEXT if other != language
    )


def test_signed_in_guest_can_link_without_starting_on_rewards(journey):
    guest, _, intent, client = journey
    client.force_login(guest)
    assert TEXT["ar"]["link"] in client.get(detail(intent)).content.decode()
    response = client.post(action(intent), follow=True)
    intent.refresh_from_db()
    assert intent.customer_id == guest.pk and "points_checkout" in response.context


def test_verified_phone_handles_private_email_without_contact_rewrite(journey):
    guest, _, intent, client = journey
    intent.guest_email = "other-booking-mail@example.invalid"
    intent.guest_phone = "+12125551234"
    intent.save()
    VerifiedPhone.objects.create(user=guest, phone=intent.guest_phone)
    client.post(action(intent))
    client.force_login(guest)
    client.get(action(intent))
    intent.refresh_from_db()
    assert intent.customer_id == guest.pk
    assert intent.guest_email == "other-booking-mail@example.invalid"


@pytest.mark.parametrize("reason", ["mismatch", "unverified", "contact_only", "owned_elsewhere"])
def test_identity_and_ownership_failures_never_attach(journey, reason):
    guest, _, intent, client = journey
    other = user("other-checkout-resume")
    if reason == "owned_elsewhere":
        intent.customer = other
    if reason in {"mismatch", "contact_only"}:
        intent.guest_email = other.email
    if reason == "contact_only":
        profile = profile_for(guest)
        profile.phone = intent.guest_phone
        profile.save()
    if reason == "unverified":
        profile = profile_for(guest)
        profile.email_verified_at = None
        profile.save()
    intent.save()
    before = intent.customer_id
    client.post(action(intent))
    client.force_login(guest)
    client.get(action(intent))
    intent.refresh_from_db()
    assert intent.customer_id == before


def test_reference_is_not_proof_and_get_cannot_start_link(journey):
    guest, _, intent, client = journey
    stranger = Client()
    stranger.force_login(guest)
    assert stranger.post(action(intent)).status_code == 404
    client.force_login(guest)
    assert client.get(action(intent)).status_code == 404
    intent.refresh_from_db()
    assert intent.customer_id is None


def test_csrf_and_lost_session_cannot_resume(journey):
    guest, _, intent, client = journey
    protected = Client(enforce_csrf_checks=True)
    own_intent(protected, intent)
    assert protected.post(action(intent)).status_code == 403
    client.post(action(intent))
    client.force_login(guest)
    session = client.session
    session.pop("booking_session_marker")
    session.save()
    assert client.get(action(intent)).status_code == 404


@pytest.mark.parametrize(
    "state", ["expired", "completed", "payment", "unknown_payment", "disabled"]
)
def test_changes_while_signing_in_do_not_attach(journey, state, settings):
    guest, _, intent, client = journey
    client.post(action(intent))
    if state == "expired":
        intent.expires_at = timezone.now() - timedelta(seconds=1)
        intent.save()
    elif state == "completed":
        intent.status = BookingIntent.Status.COMPLETED
        intent.save()
    elif state in {"payment", "unknown_payment"}:
        PaymentAttempt.objects.create(
            booking_intent=intent,
            provider="hyperpay",
            amount=1250,
            currency="SAR",
            status="pending" if state == "payment" else "unknown",
            idempotency_key="TEST-RESUME-PENDING-PAYMENT",
        )
    else:
        settings.LOYALTY_ENABLED = False
    client.force_login(guest)
    response = client.get(action(intent))
    assert response.status_code in {302, 404}
    intent.refresh_from_db()
    assert intent.customer_id is None


def test_no_historical_backfill_or_new_membership(journey):
    guest, _, intent, client = journey
    from tests.test_hostaway_booking_phase5 import make_intent

    older = make_intent(property_obj=intent.property)
    older.guest_email = guest.email
    older.save()
    client.post(action(intent))
    client.force_login(guest)
    client.get(action(intent))
    older.refresh_from_db()
    assert older.customer_id is None
    assert PENDING not in client.session or intent.public_reference not in client.session[PENDING]
