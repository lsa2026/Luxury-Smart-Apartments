from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection
from django.test import Client

from apps.core.forms import ContactForm
from apps.core.models import ContactMessage, FAQItem, SitePage, SiteSetting

pytestmark = pytest.mark.django_db
copy_migration = import_module("apps.core.migrations.0023_public_address_and_destinations")


def data(phone="", country="SA"):
    return {
        "name": "Synthetic guest",
        "email": "guest@example.invalid",
        "phone_country": country,
        "phone": phone,
        "subject": "Stay question",
        "message": "Please help me with a synthetic stay question.",
        "website": "",
    }


@pytest.mark.parametrize(
    ("phone", "country", "expected"),
    [
        ("0501234567", "SA", "+966501234567"),
        ("501234567", "SA", "+966501234567"),
        ("966501234567", "SA", "+966501234567"),
        ("+966 50 123 4567", "SA", "+966501234567"),
        ("00966 50 123 4567", "SA", "+966501234567"),
        ("٠٥٠١٢٣٤٥٦٧", "SA", "+966501234567"),
        ("۰۵۰۱۲۳۴۵۶۷", "SA", "+966501234567"),
        ("０５０１２３４５６７", "SA", "+966501234567"),
        ("\u200e+966 50 123 4567\u200f", "SA", "+966501234567"),
        ("(050) 123-4567", "SA", "+966501234567"),
        ("0033612345678", "SA", "+33612345678"),
        ("0612345678", "FR", "+33612345678"),
        ("0612345678", "MA", "+212612345678"),
        ("2125551234", "US", "+12125551234"),
        ("+1 (212) 555-1234", "SA", "+12125551234"),
        ("02 36618 300", "IT", "+390236618300"),
        ("", "SA", ""),
        ("0501234567", "", "+966501234567"),
    ],
)
def test_contact_phone_uses_shared_parser_without_javascript(phone, country, expected):
    form = ContactForm(data(phone, country))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == expected


def test_old_contact_forms_without_country_keep_working():
    values = data("0501234567")
    values.pop("phone_country")
    form = ContactForm(values)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == "+966501234567"


def test_invalid_country_preserves_contact_fields_and_never_sends():
    form = ContactForm(data("0501234567", "XX"))
    assert not form.is_valid()
    assert "phone_country" in form.errors
    assert form["message"].value() == data()["message"]


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_contact_page_optional_country_and_phone_are_not_duplicated(language):
    response = Client().get(f"/{language}/contact/")
    html = response.content.decode()
    assert response.status_code == 200
    assert html.count('name="phone_country"') == 1
    assert html.count('name="phone"') == 1
    assert 'value="SA" selected' in html
    assert 'id="contact-phone-help"' in html
    assert "css/contact-phone.css" in html
    assert "js/contact-phone.js" in html
    assert "data-checkout-phone" not in html


def test_contact_post_saves_canonical_phone_without_changing_other_fields(monkeypatch):
    monkeypatch.setattr(
        "apps.notifications.services.events.handle_contact_created", lambda *a, **k: None
    )
    response = Client().post("/ar/contact/", data("٠٥٠١٢٣٤٥٦٧"))
    assert response.status_code == 302
    message = ContactMessage.objects.get(email="guest@example.invalid")
    assert message.phone == "+966501234567"
    assert message.message == data()["message"]


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_seeded_copy_displays_address_and_both_destinations(language):
    faq = FAQItem.objects.get(question_en=copy_migration.FAQ["question_en"], property=None)
    assert getattr(faq, f"answer_{language}") == copy_migration.ANSWERS[f"answer_{language}"]
    for slug in ("about", "contact"):
        page = SitePage.objects.get(slug=slug)
        value = getattr(page, f"meta_description_{language}")
        assert ("مراكش" if language == "ar" else "Marrakech") in value
        assert value in Client().get(f"/{language}/{slug}/").content.decode()
    setting = SiteSetting.objects.get()
    assert ("مراكش" if language == "ar" else "Marrakech") in getattr(
        setting, f"footer_text_{language}"
    )


def test_copy_migration_is_reversible_idempotent_and_preserves_customizations():
    editor = SimpleNamespace(connection=connection)
    setting = SiteSetting.objects.get()
    policy = setting.default_cancellation_policy_ar
    office = setting.public_address_ar
    faq = FAQItem.objects.get(question_en=copy_migration.FAQ["question_en"], property=None)
    FAQItem.objects.filter(pk=faq.pk).update(answer_fr="Owner custom answer")
    SitePage.objects.filter(slug="contact").update(meta_description_fr="Owner custom description")
    copy_migration.backwards(apps, editor)
    faq.refresh_from_db()
    assert faq.answer_ar == copy_migration.FAQ["answer_ar"]
    copy_migration.forwards(apps, editor)
    copy_migration.forwards(apps, editor)
    faq.refresh_from_db()
    setting.refresh_from_db()
    assert faq.answer_ar == copy_migration.ANSWERS["answer_ar"]
    assert faq.answer_fr == "Owner custom answer"
    assert SitePage.objects.get(slug="contact").meta_description_fr == "Owner custom description"
    assert setting.default_cancellation_policy_ar == policy
    assert setting.public_address_ar == office
