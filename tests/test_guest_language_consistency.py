"""Public language selection, compiled copy and display-only regressions."""

import ast
import gettext
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.urls import ResolverMatch
from django.utils.translation import override, templatize

from apps.core.middleware import GuestJourneyLanguageMiddleware
from apps.core.templatetags.presentation import localized_policy_copy, localized_public_address
from apps.reservations.services.stay_policy import stay_policy_for
from tests.test_booking_views_admin import owned_client_quote
from tests.test_stay_policy_display import make_property, make_tier


@pytest.fixture(autouse=True)
def isolated_language():
    # LocaleMiddleware activates a language per request; do not leak it to tests.
    with override(settings.LANGUAGE_CODE):
        yield


def _messages(path):
    source = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        quoted = r"""(?:u)?(?:'([^'\\]|\\.)*'|"([^"\\]|\\.)*")"""
        pattern = rf"\b(gettext|ngettext|pgettext|npgettext)\(\s*({quoted})(?:\s*,\s*({quoted}))?"
        for match in re.finditer(pattern, templatize(source)):
            name = match[1]
            message = ast.literal_eval(match[2])
            if name in {"pgettext", "npgettext"}:
                message = f"{message}\x04{ast.literal_eval(match[5])}"
            yield name, message
        return
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"_", "gettext", "gettext_lazy"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.func.id, node.args[0].value


@pytest.mark.parametrize("language", ["ar", "fr"])
def test_shipped_catalog_covers_public_guest_templates_and_form_errors(language):
    root = Path(settings.BASE_DIR)
    with (root / f"locale/{language}/LC_MESSAGES/django.mo").open("rb") as source:
        catalog = gettext.GNUTranslations(source)._catalog
    paths = list((root / "templates").glob("*.html"))
    for directory in (
        "accounts",
        "reservations",
        "payments",
        "core",
        "properties",
        "reviews",
        "includes",
        "errors",
        "loyalty",
    ):
        paths.extend((root / "templates" / directory).glob("*.html"))
    paths.extend(
        root / path
        for path in (
            "apps/accounts/forms.py",
            "apps/accounts/views.py",
            "apps/accounts/password_reset.py",
            "apps/reservations/forms.py",
            "apps/reservations/booking_forms.py",
            "apps/reservations/views.py",
            "apps/reservations/modification_forms.py",
            "apps/core/forms.py",
        )
    )
    missing = []
    for path in paths:
        for name, message in _messages(path):
            if name in {"ngettext", "npgettext"}:
                translations = [
                    value
                    for key, value in catalog.items()
                    if isinstance(key, tuple) and key[0] == message
                ]
            else:
                translations = [catalog.get(message)]
            if not translations or not all(translations):
                missing.append((str(path.relative_to(root)), message))
            # Never introduce variables the rendering code cannot supply.
            expected = set(re.findall(r"%\((\w+)\)[sd]", message))
            for text in filter(None, translations):
                assert set(re.findall(r"%\((\w+)\)[sd]", text)) <= expected, message
    assert not missing, missing


@pytest.mark.django_db
@pytest.mark.parametrize(
    "language, expected",
    [
        ("ar", "يشمل الإجمالي جميع بنود السعر الموضحة أعلاه."),
        ("fr", "Le total comprend tous les éléments de prix indiqués ci-dessus."),
        ("en", "The total includes every price component listed above."),
    ],
)
def test_public_language_reaches_the_signed_quote_without_altering_money(language, expected):
    client, quote, reference = owned_client_quote()
    original = (quote.total_price, quote.currency, quote.payment_amount_sar)
    client.get(f"/{language}/")
    response = client.get(f"/reservations/quotes/{reference}/")
    assert response.status_code == 200
    assert f'lang="{language}"' in response.content.decode()
    assert expected in response.content.decode()
    quote.refresh_from_db()
    assert (quote.total_price, quote.currency, quote.payment_amount_sar) == original


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_payment_result_error_and_account_prompt_are_localized_without_calling_gateway(language):
    property_obj = SimpleNamespace(
        slug="test-property", name_ar="اختبار", name_en="Test", name_fr="Test"
    )
    attempt = SimpleNamespace(
        booking_intent=SimpleNamespace(property=property_obj), amount="500.25", currency="SAR"
    )
    with override(language):
        html = render_to_string(
            "payments/hyperpay_result.html",
            {
                "attempt": attempt,
                "outcome": None,
                "claimable_reference": "synthetic-only",
            },
        )
    if language == "ar":
        assert "Save it to your account" not in html
        assert "Verification is temporarily unavailable" not in html
        assert "احفظه في حسابك" in html
    elif language == "fr":
        assert "Save it to your account" not in html
        assert "L’enregistrer dans votre compte" in html
        assert "احفظه" not in html
    else:
        assert "Save it to your account" in html
        assert "احفظه" not in html


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_public_prefix_overrides_old_cookie_and_browser_and_persists(language):
    client = Client(HTTP_ACCEPT_LANGUAGE="en")
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr" if language != "fr" else "en"
    response = client.get(f"/{language}/")
    assert response.status_code == 200
    assert f'lang="{language}"' in response.content.decode()
    assert client.cookies[settings.LANGUAGE_COOKIE_NAME].value == language
    for path in ("/login/", "/register/", "/reservations/manage/", "/account/reset/"):
        response = client.get(path)
        assert response.status_code == 200
        assert f'lang="{language}"' in response.content.decode()
    response = client.get(f"/{language}/")
    assert settings.LANGUAGE_COOKIE_NAME not in response.cookies


@pytest.mark.parametrize(
    "path, view, status, content_type",
    [
        ("/ar/integrations/", "integrations:callback", 200, "text/html"),
        ("/fr/admin/", "admin:index", 200, "text/html"),
        ("/de/", "core:home", 200, "text/html"),
        ("/en/", "core:home", 404, "text/html"),
        ("/ar/", "core:home", 200, "application/json"),
    ],
)
def test_language_memory_never_changes_callbacks_admin_invalid_prefix_or_errors(
    path, view, status, content_type
):
    request = RequestFactory().get(path)
    namespace, name = view.split(":")
    request.resolver_match = ResolverMatch(
        lambda: None, (), {}, url_name=name, namespaces=[namespace]
    )
    middleware = GuestJourneyLanguageMiddleware(
        lambda request: HttpResponse(status=status, content_type=content_type)
    )
    assert settings.LANGUAGE_COOKIE_NAME not in middleware(request).cookies


@pytest.mark.parametrize(
    "language, expected", [("ar", "مراجعة الطلب"), ("fr", "Vérification"), ("en", "Review")]
)
def test_booking_step_uses_review_of_request_not_guest_review(language, expected):
    with override(language):
        html = Template('{% include "includes/progress_indicator.html" %}').render(
            Context({"step": 3})
        )
    assert expected in html


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_policy_translation_does_not_change_refund_percentages_windows_or_stored_notes(language):
    note = "استرداد كامل للحجوزات الملغاة وفق السياسة المرنة."
    property_obj = make_property(cancellation_policy="flexible")
    make_tier("flexible", 0, "100", note=note)
    with override(language):
        policy = stay_policy_for(property_obj)
        assert policy.refund_tiers[0].percentage == 100
        assert policy.refund_tiers[0].hours_before == 0
        assert policy.refund_tiers[0].note == note
        displayed = localized_policy_copy(note)
        assert (displayed == note) == (language == "ar")
        assert (
            localized_policy_copy("custom terms must be preserved")
            == "custom terms must be preserved"
        )


@pytest.mark.parametrize(
    "language, expected",
    [("ar", "الطابق الثالث"), ("fr", "troisième étage"), ("en", "third floor")],
)
def test_known_addresses_are_localized_without_editing_source_or_inventing_details(
    language, expected
):
    source = "رقم 32 3ed floor"
    with override(language):
        assert expected in localized_public_address(source)
        assert "32" in localized_public_address(source)
        assert localized_public_address("Unknown address 123") == "Unknown address 123"
    assert source == "رقم 32 3ed floor"
