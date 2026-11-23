"""Owner-directed scope change; synthetic source reads, no sender or replay."""

from copy import deepcopy

import pytest
from django.conf import settings as django_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.reservations import message_source_authorization as source
from tests import test_message_source_authorization as source_tests

pytestmark = pytest.mark.django_db
record = source_tests.record


@pytest.mark.parametrize(
    "kind", ["status_new", "house_rules", "checkin_guide", "reminder_checkout"]
)
def test_service_scope_comes_from_current_event_without_extra_opt_in_or_grant(client, record, kind):
    original = deepcopy(record[1].legal_acceptance)
    with CaptureQueriesContext(connection) as queries:
        response = source_tests.request_source(client, record, kind)
    assert response.status_code == 200
    facts = response.json()
    scope = facts["service_scope"]
    assert scope == {
        "contract_version": "lsa01-customer-service-scope-1",
        "business_identity": "Luxury Smart Apartments",
        "message_class": "customer_service",
        **{
            k: facts[k]
            for k in (
                "reservation_key",
                "reservation_revision",
                "recipient_ref",
                "source_event_key",
                "source_message_type",
                "purpose",
            )
        },
    }
    assert all(v is None for v in facts["consent"].values())
    assert "whatsapp_consent_unverified" not in facts["blockers"]
    assert facts["dispatch_allowed"] is False
    assert facts["human_takeover"] is None
    assert record[1].guest_phone not in str(scope)
    record[1].refresh_from_db()
    assert record[1].legal_acceptance == original
    # The private read uses an atomic snapshot; transaction-control SQL is not a data write.
    assert all(
        q["sql"].lstrip().split()[0].upper()
        in {"SELECT", "SAVEPOINT", "RELEASE", "BEGIN", "COMMIT", "ROLLBACK"}
        for q in queries.captured_queries
    )


@pytest.mark.parametrize("kind", ["payment_success", "loyalty_invitation"])
def test_financial_and_registry_gates_remain_even_without_opt_in_gate(client, record, kind):
    paid = source_tests.paid_record(record)
    facts = source_tests.request_source(client, paid, kind, financial=True).json()
    assert facts["service_scope"]["source_event_key"] == facts["source_event_key"]
    assert facts["service_scope"]["purpose"] == source.PURPOSES[kind]
    assert facts["domain_authorized"] is False and facts["dispatch_allowed"] is False
    assert facts["payment_verified"] is True
    assert facts["financial_proof_ref"] == paid[2].proof_key
    assert all(v is None for v in facts["consent"].values())
    assert "core_delivery_dependency_requires_verified_receipts" in facts["blockers"]
    assert "loyalty_registry_and_marketing_consent_required" not in facts["blockers"]
    if kind == "loyalty_invitation":
        assert "loyalty_registry_required" in facts["blockers"]


def test_known_withdrawal_remains_a_veto_not_a_grant(client, record, monkeypatch):
    consent = source.projected_consent(record[1], record[0], "booking_confirmation")
    consent["revoked"] = True
    monkeypatch.setattr(source, "projected_consent", lambda *_: deepcopy(consent))
    facts = source_tests.request_source(client, record, "status_new").json()
    assert facts["consent"]["revoked"] is True
    assert facts["consent"]["service"] is None
    assert facts["domain_authorized"] is False
    assert "customer_messaging_withdrawn" in facts["blockers"]
    assert (
        "predecessor_delivery_unverified" not in facts["blockers"]
    )  # status_new has no predecessor


def test_scope_never_falls_back_for_unknown_purpose_or_noncanonical_revision():
    assert source.service_scope({"purpose": "made_up", "source_message_type": "made_up"}) is None
    assert (
        source.service_scope(
            {
                "purpose": "booking_confirmation",
                "source_message_type": "status_new",
                "reservation_revision": 1,
            }
        )
        is None
    )


def test_removed_opt_in_does_not_enable_the_closed_legacy_mailer(record, settings):
    from apps.loyalty.notifications import dispatch_due
    from apps.notifications.models import EmailDelivery

    settings.LOYALTY_ENABLED = settings.LOYALTY_MESSAGES_ENABLED = True
    settings.EMAIL_DELIVERY_ENABLED = True
    settings.TELEGRAM_WORKGROUP_ONLY_POLICY = True
    assert dispatch_due() == {"status": "disabled", "queued": 0}
    assert not EmailDelivery.objects.filter(message_type="loyalty_join_invitation").exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_checkout_does_not_offer_extra_message_permission_but_keeps_legal_acceptance(language):
    from tests.test_booking_views_admin import owned_client_quote

    client, _, reference = owned_client_quote()
    client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get(f"/reservations/quotes/{reference}/")
    assert response.status_code == 200
    html = response.content.decode()
    assert 'name="marketing_consent"' not in html
    assert 'name="terms_accepted"' in html and 'name="house_rules_accepted"' in html
    assert 'name="special_requests"' in html
