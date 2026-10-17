"""Fictional transfers only; statement reads and template renders never send."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils.translation import override

from apps.loyalty.copy import copy
from apps.loyalty.models import Ledger
from apps.loyalty.notifications import resolve_recipient
from apps.loyalty.services import RULES, balance, transfer
from apps.loyalty.statement import decorate
from apps.notifications.services.email import EmailMessageRequest, build_guest_email
from tests.test_loyalty_stage9 import funded_pair, login, owner
from tests.test_loyalty_stage9 import ui as ui  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_transfer_both_sides_have_snapshot_counterparty_and_private_balance(language, settings):
    a, b, am, bm = funded_pair()
    transfer(a, "fictional-statement-transfer", bm.number, "3.00")
    # Current profiles are not allowed to rewrite the historical transaction name.
    b.first_name = "Renamed after transfer"
    b.save()
    before = (Ledger.objects.count(), balance(am), balance(bm))
    for account, _member, other, name, debit in [
        (a, am, bm, "ضيف b", True),
        (b, bm, am, "ضيف a", False),
    ]:
        client = login(account)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        response = client.get("/my-bookings/rewards/")
        html = response.content.decode()
        labels = copy(language)
        row = next(r for r in response.context["loyalty_rows"] if r.event_type == "transfer")
        assert row.ui_counterpart["name"] == name
        assert row.ui_counterpart["number"] == other.number
        assert labels["gift_out" if debit else "gift_in"] in html
        assert labels["paired_operation"] in html and labels["before_balance"] in html
        assert RULES not in html
        assert labels["rules_signed"] in html and labels["rules_help"] in html
        assert labels["program_balance"] not in html
        assert "Renamed after transfer" not in str(row.ui_counterpart)
        assert other.customer.email not in html
        assert row.ui_balance_before == (Decimal("22.50") if debit else Decimal("0.00"))
    assert before == (Ledger.objects.count(), balance(am), balance(bm))


def test_decoration_pairs_in_one_query_and_does_not_infer_missing_party(django_assert_num_queries):
    a, b, am, bm = funded_pair()
    transfer(a, "fictional-snapshot-query", bm.number, "3.00")
    rows = list(Ledger.objects.filter(member=am))
    with django_assert_num_queries(1):
        decorate(rows)
    row = next(r for r in rows if r.event_type == "transfer")
    assert row.ui_counterpart["number"] == bm.number
    row.operation_group = "no-such-pair"
    row.rules_version = "future-unknown-version"
    decorate([row])
    assert row.ui_counterpart is None
    assert row.ui_rules == copy()["rules_other"]


def test_snapshot_email_fallback_is_not_disclosed():
    a, b, am, bm = funded_pair()
    b.first_name = b.last_name = ""
    b.save()
    transfer(a, "fictional-private-email-snapshot", bm.number, "3.00")
    row = Ledger.objects.get(member=am, event_type="transfer")
    decorate([row])
    assert row.ui_counterpart["name"] == copy()["member"]
    assert "@" not in str(row.ui_counterpart)


def test_admin_keeps_internal_rules_reference_without_changing_it():
    a, b, am, bm = funded_pair()
    transfer(a, "fictional-admin-details", bm.number, "3.00")
    html = login(owner()).get("/admin/loyalty/").content.decode()
    assert "المرجع التقني للقواعد" in html
    assert RULES in html
    assert "رقم العملية المقابلة" in html


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize(
    "kind",
    [
        "account_verify_email",
        "account_password_reset",
        "account_welcome",
        "loyalty_join_invitation",
        "loyalty_movement",
        "loyalty_invite_ready",
    ],
)
def test_message_catalog_email_families_already_include_official_logo(language, kind):
    from apps.notifications.services.email import (
        SUBJECTS,
        TEMPLATE_GROUPS,
        _register_loyalty_copy,
    )

    _register_loyalty_copy(kind)
    request = EmailMessageRequest(
        recipient="fictional@example.invalid",
        subject=SUBJECTS[kind][language],
        language=language,
        template_name=TEMPLATE_GROUPS[kind],
        context={
            "heading": "Synthetic",
            "message": "Synthetic",
            "action_url": "https://example.invalid/",
        },
    )
    html = build_guest_email(request).alternatives[0].content
    assert "/static/images/brand/luxury-living-lockup-light.png" in html
    assert f'lang="{language}"' in html
    assert f'dir="{"rtl" if language == "ar" else "ltr"}"' in html


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_movement_email_uses_its_saved_language_and_same_safe_counterparty(language, monkeypatch):
    monkeypatch.setattr("apps.loyalty.notifications.messages_enabled", lambda: True)
    a, b, am, bm = funded_pair()
    transfer(a, "fictional-email-details", bm.number, "3.00")
    row = Ledger.objects.get(member=am, event_type="transfer")
    delivery = SimpleNamespace(
        recipient_source="loyalty_ledger", recipient_reference=row.pk, language=language
    )
    with override("fr" if language != "fr" else "ar"):
        recipient, values = resolve_recipient(delivery)
    assert recipient == a.email
    assert values["transaction_kind"] == copy(language)["gift_out"]
    assert values["counterpart"]["number"] == bm.number
    assert b.email not in str(values)
    assert values["balance"] == "19.50" and values["balance_before"] == "22.50"
