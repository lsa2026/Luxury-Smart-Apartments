"""Regression coverage for the guest's French account and booking journey."""

import ast
import gettext
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils.html import escape
from django.utils.translation import gettext as translate
from django.utils.translation import override

from apps.accounts.forms import CustomerProfileForm, EmailVerificationCodeForm
from apps.accounts.models import profile_for
from apps.reservations.forms import ReservationAccessForm
from apps.reservations.models import Reservation
from apps.reservations.views import MODIFICATION_REFUSAL_MESSAGES
from tests.test_account_booking_claim import client_holding, make_reservation


def test_compiled_french_catalog_covers_guest_account_and_management_copy():
    """Check the shipped .mo, not just the editable .po, including error branches."""
    root = Path(settings.BASE_DIR)
    with (root / "locale/fr/LC_MESSAGES/django.mo").open("rb") as source:
        catalog = gettext.GNUTranslations(source)._catalog
    paths = list((root / "templates/accounts").glob("*.html"))
    paths += [
        root / "templates/reservations" / name
        for name in (
            "reservation_access.html",
            "_reservation_access_form.html",
            "manage_reservation.html",
            "modification_detail.html",
        )
    ]
    paths += [
        root / name
        for name in (
            "apps/accounts/forms.py",
            "apps/accounts/views.py",
            "apps/accounts/password_reset.py",
            "apps/reservations/forms.py",
            "apps/reservations/views.py",
            "apps/reservations/modification_forms.py",
        )
    ]
    missing = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            messages = [
                node.args[0].value
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"_", "gettext_lazy"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ]
        else:
            messages = [
                match[2]
                for match in re.finditer(r"""{%\s*trans(?:late)?\s+(["'])(.*?)\1""", source)
            ]
            messages += [
                re.sub(r"{{\s*(\w+)\s*}}", r"%(\1)s", match[1])
                for match in re.finditer(
                    r"{%\s*blocktrans[^%]*%}(.*?){%\s*endblocktrans\s*%}",
                    source,
                    re.S,
                )
            ]
        missing.extend(
            (str(path.relative_to(root)), message)
            for message in messages
            if not catalog.get(message)
        )
    assert not missing, missing


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/login/", "/accounts/login/", "/register/"])
@override_settings(
    GOOGLE_SIGN_IN_ENABLED=True,
    APPLE_SIGN_IN_ENABLED=True,
    SOCIALACCOUNT_PROVIDERS={
        "google": {"APP": {"client_id": "test-only", "secret": "test-only"}},
        "apple": {"APP": {"client_id": "test-only", "secret": "test-only", "key": "test"}},
    },
)
def test_social_sign_in_and_registration_are_french(path):
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
    response = client.get(path)
    assert response.status_code == 200
    content = response.content.decode()
    assert 'lang="fr"' in content
    for text in (
        "Autres options de connexion sécurisée",
        "Continuer avec Google",
        "Continuer avec Apple",
    ):
        assert text in content
    for text in ("Other secure sign-in options", "Continue with Google", "Continue with Apple"):
        assert text not in content
    if path == "/register/":
        assert "code à six chiffres" in content
    else:
        assert "nom de famille et votre numéro de mobile" in content


@pytest.mark.django_db
def test_language_switch_preserves_unprefixed_routes_and_french_placeholders():
    client = Client()
    response = client.post("/i18n/setlang/", {"language": "fr", "next": "/reservations/manage/"})
    assert response.status_code == 302
    assert response.url == "/reservations/manage/"
    assert client.cookies[settings.LANGUAGE_COOKIE_NAME].value == "fr"
    for path in ("/reservations/manage/", "/login/", "/register/", "/account/reset/"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'lang="fr"' in response.content.decode()
    response = client.get("/reservations/manage/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "Tel qu’indiqué lors de la réservation" in content
    assert "Par exemple : +966 50 000 0000" in content
    assert "As entered when booking" not in content
    assert "For example" not in content
    for path in ("/fr/", "/login/", "/reservations/manage/"):
        content = client.get(path).content.decode()
        assert "nom de famille et votre numéro de mobile" in content
        assert "booking number and" not in content


@pytest.mark.django_db
def test_invalid_access_post_returns_validation_page_not_server_error():
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
    response = client.post("/reservations/manage/", {"last_name": "Test", "phone": "invalid"})
    assert response.status_code == 400
    assert "Saisissez un numéro de téléphone international valide." in response.content.decode()


@pytest.mark.django_db
def test_dashboard_profile_empty_states_and_saved_message_are_french():
    user = get_user_model().objects.create_user(
        username="french@example.invalid",
        email="french@example.invalid",
        first_name="Camille",
    )
    client = Client()
    client.force_login(user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
    response = client.get("/login/", follow=True)
    assert response.redirect_chain == [("/my-bookings/", 302)]
    content = response.content.decode()
    for text in (
        "Profil du voyageur",
        "Vos coordonnées et votre adresse",
        "Adresse de résidence",
        "Pays de résidence",
        "Enregistrer mes coordonnées",
        "Programme de fidélité",
        "Réservations à venir et en cours",
        "Séjours passés",
        "Réservations annulées",
        "Aucun séjour terminé pour le moment.",
        "Aucune réservation annulée.",
    ):
        assert text in content
    assert "ملف الضيف" not in content
    assert "Residence address" not in content
    response = client.post("/my-bookings/", {"first_name": "Camille"}, follow=True)
    assert "Votre profil voyageur a été enregistré." in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "language, expected",
    [
        ("ar", "ملف الضيف"),
        ("en", "Guest profile"),
        ("fr", "Profil du voyageur"),
    ],
)
def test_dashboard_keeps_all_three_languages(language, expected):
    user = get_user_model().objects.create_user(username="guest", email="guest@example.invalid")
    client = Client()
    client.force_login(user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get("/my-bookings/")
    assert response.status_code == 200
    assert expected in response.content.decode()


@pytest.mark.django_db
@override_settings(
    BOOKING_AUTOMATIC_CANCELLATION_ENABLED=True,
    BOOKING_AUTOMATIC_REFUND_ENABLED=True,
    BOOKING_LAUNCH_FLEXIBLE_CANCELLATION_ENABLED=True,
    HYPERPAY_ENVIRONMENT="production",
)
def test_confirmed_booking_management_and_cancellation_copy_are_french():
    # Fixture only: GET the page, never call a provider or cancel a reservation.
    reservation = make_reservation("LSA-FRENCH")
    reservation.normalized_status = Reservation.Status.CONFIRMED
    reservation.source_type = Reservation.SourceType.DIRECT_WEBSITE
    reservation.hostaway_reservation_id = 123456
    reservation.save()
    client = client_holding(reservation)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
    response = client.get(f"/reservations/manage/{reservation.public_reference}/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Annuler la réservation et procéder au remboursement" in content
    assert "Pendant cette période de lancement" in content
    assert "Je comprends que la réservation sera annulée" in content
    assert "Cancel booking and process refund" not in content


@pytest.mark.django_db
def test_profile_and_email_code_validation_are_french():
    user = get_user_model().objects.create_user(username="form-guest")
    with override("fr"):
        form = CustomerProfileForm(
            {"phone": "invalid"},
            user=user,
            profile=profile_for(user),
        )
        assert not form.is_valid()
        assert "Saisissez un numéro de mobile valide" in str(form.errors)
        code = EmailVerificationCodeForm({"code": "abcdef"})
        assert not code.is_valid()
        assert "Saisissez le code à six chiffres" in str(code.errors)
        assert str(ReservationAccessForm()["phone"].label) != "Phone number"
        access = ReservationAccessForm({"last_name": "Test", "phone": "invalid"})
        assert not access.is_valid()
        assert "Saisissez un numéro de téléphone international valide." in str(access.errors)


def test_refusal_messages_follow_each_request_language_not_process_startup():
    message = MODIFICATION_REFUSAL_MESSAGES["capacity_exceeded"]
    with override("fr"):
        assert str(message) == "Le nombre de voyageurs dépasse la capacité de ce logement."
    with override("en"):
        assert str(message) == "The guest count exceeds this property's capacity."
    with override("fr"):
        assert str(message) == "Le nombre de voyageurs dépasse la capacité de ce logement."


@pytest.mark.parametrize(
    "source, expected",
    [
        ("Your email address is confirmed.", "Votre adresse e-mail est confirmée."),
        (
            "This confirmation link is no longer valid. Request a new one.",
            "Ce lien de confirmation n’est plus valide. Demandez-en un nouveau.",
        ),
        ("Password", "Mot de passe"),
        ("New password", "Nouveau mot de passe"),
        ("Confirm the new password", "Confirmer le nouveau mot de passe"),
    ],
)
def test_previously_fuzzy_account_translations_are_accurate(source, expected):
    with override("fr"):
        assert escape(translate(source)) == escape(expected)
