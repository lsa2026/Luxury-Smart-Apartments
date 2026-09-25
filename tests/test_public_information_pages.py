import pytest
from django.contrib import admin
from django.test import Client

from apps.core.admin import SitePageAdmin
from apps.core.models import SitePage, SiteSetting
from apps.core.templatetags.presentation import structured_text

pytestmark = pytest.mark.django_db


def test_reference_contact_details_and_public_pages_are_seeded() -> None:
    assert (
        SitePage.objects.filter(
            slug__in=("about", "contact", "terms", "privacy", "cancellation"),
            is_published=True,
        ).count()
        == 5
    )

    setting = SiteSetting.objects.get()
    assert setting.brand_name_ar == "Luxury Smart Apartments"
    assert setting.contact_email == "saeed@luxurysmartapartments.com"
    assert setting.contact_phone == "+966501205651"
    assert setting.public_address_ar == "الرياض، المملكة العربية السعودية"
    assert setting.instagram_url.endswith("/luxury_smart_apartments/")


def test_legal_content_is_structured_and_no_longer_a_placeholder() -> None:
    response = Client().get("/ar/legal/terms/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "نطاق الخدمة" in content
    assert "نسخة أولية للمراجعة القانونية" not in content
    assert '<time datetime="2026-08-01">' in content
    assert "<h2>" in content


def test_contact_copy_and_contact_details_are_dashboard_backed() -> None:
    page = SitePage.objects.get(slug="contact")
    page.body_ar = "رسالة تواصل قابلة للتعديل من لوحة الإدارة."
    page.save(update_fields=["body_ar"])

    content = Client().get("/ar/contact/").content.decode()
    assert "رسالة تواصل قابلة للتعديل من لوحة الإدارة." in content
    assert "saeed@luxurysmartapartments.com" in content
    assert "+966501205651" in content


def test_contact_page_can_be_unpublished_from_dashboard() -> None:
    SitePage.objects.filter(slug="contact").update(is_published=False)
    assert Client().get("/ar/contact/").status_code == 404


def test_structured_text_escapes_editor_input() -> None:
    rendered = str(structured_text("## <script>alert(1)</script>\n\n- <img src=x>"))
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<h2>" in rendered and "<ul>" in rendered


def test_page_content_has_grouped_admin_controls() -> None:
    model_admin = SitePageAdmin(SitePage, admin.site)
    flattened_fields = {
        field for _, options in model_admin.fieldsets for field in options["fields"]
    }
    assert {
        "body_ar",
        "body_en",
        "body_fr",
        "meta_description_ar",
        "is_published",
        "last_reviewed_at",
    } <= flattened_fields


def test_footer_uses_layered_luxury_layout_and_dashboard_contact_details() -> None:
    content = Client().get("/ar/").content.decode()

    assert 'class="footer-invitation"' in content
    assert 'class="container footer-grid"' in content
    assert 'class="footer-contact-bar"' in content
    assert 'class="footer-lower"' in content
    assert "saeed@luxurysmartapartments.com" in content
    assert "+966501205651" in content
    assert "css/site.css?v=45" in content


@pytest.mark.parametrize(
    ("url", "heading", "city_label", "cta"),
    [
        ("/ar/about/", "إقامات ذكية", "وجهتان مختارتان بعناية", "استكشف الشقق"),
        ("/en/about/", "Smart stays", "Two destinations, chosen with care", "Explore apartments"),
        (
            "/fr/about/",
            "Des séjours intelligents",
            "Deux destinations choisies",
            "Découvrir les appartements",
        ),
    ],
)
def test_about_page_uses_the_dedicated_trilingual_experience(
    url: str,
    heading: str,
    city_label: str,
    cta: str,
) -> None:
    content = Client().get(url).content.decode()

    assert heading in content
    assert city_label in content
    assert cta in content
    assert 'class="about-collage"' in content
    assert content.count("images/about/apartment-") >= 12
    assert "9.5/10" in content
    assert "4.7/5" in content
