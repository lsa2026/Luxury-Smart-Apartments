from io import BytesIO

import pytest
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import translation
from PIL import Image

from apps.core.admin import SiteInterfaceImageAdmin
from apps.core.models import SiteInterfaceImage
from apps.core.uploads import site_interface_image_upload_to
from apps.properties.models import Property, PropertyImage

pytestmark = pytest.mark.django_db


def image_upload(name: str = "interface.png") -> SimpleUploadedFile:
    payload = BytesIO()
    Image.new("RGB", (24, 16), color=(42, 31, 73)).save(payload, format="PNG")
    return SimpleUploadedFile(name, payload.getvalue(), content_type="image/png")


def test_active_interface_image_requires_arabic_and_english_alt_text() -> None:
    image = SiteInterfaceImage(
        placement=SiteInterfaceImage.Placement.HOME_HERO,
        image=image_upload(),
        is_active=True,
    )

    with pytest.raises(ValidationError) as exc_info:
        image.full_clean()

    assert {"alt_text_ar", "alt_text_en"} <= exc_info.value.message_dict.keys()


def test_interface_upload_path_does_not_retain_user_directories() -> None:
    image = SiteInterfaceImage(placement=SiteInterfaceImage.Placement.HOME_STORY)

    path = site_interface_image_upload_to(image, "../../unsafe name.PNG")

    assert path.startswith("site-interface/home_story/")
    assert path.endswith(".png")
    assert "unsafe" not in path and ".." not in path


@override_settings(MEDIA_URL="/test-media/")
def english_client() -> Client:
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    return client


def test_home_uses_active_dashboard_image_and_accessible_alt_text(tmp_path) -> None:
    with override_settings(MEDIA_ROOT=tmp_path):
        image = SiteInterfaceImage.objects.create(
            placement=SiteInterfaceImage.Placement.HOME_HERO,
            image=image_upload(),
            alt_text_ar="Arabic hero description",
            alt_text_en="Managed Riyadh hero",
            is_active=True,
        )

        with translation.override("en"):
            response = english_client().get("/")
            content = response.content.decode()

    assert response.status_code == 200
    assert image.image.url in content
    assert "Managed Riyadh hero" in content
    assert f'content="http://testserver{image.image.url}"' in content
    assert "photo-1757774698963-b23f4b273adc?auto=format&amp;fit=crop&amp;w=2400" not in content


def test_home_preserves_stock_fallback_when_slot_is_not_configured() -> None:
    content = Client().get("/").content.decode()

    assert "photo-1757774698963-b23f4b273adc?auto=format&amp;fit=crop&amp;w=2400" in content
    assert "photo-1750859464437-b66433efd869?auto=format&amp;fit=crop&amp;w=900" in content


@override_settings(MEDIA_URL="/test-media/")
def test_real_property_city_hero_keeps_priority_over_interface_fallback(tmp_path) -> None:
    with override_settings(MEDIA_ROOT=tmp_path):
        SiteInterfaceImage.objects.create(
            placement=SiteInterfaceImage.Placement.HOME_RIYADH_DESTINATION,
            image=image_upload("riyadh.png"),
            alt_text_ar="Arabic Riyadh fallback",
            alt_text_en="Riyadh fallback",
            is_active=True,
        )
        property_obj = Property.objects.create(
            hostaway_listing_id=900_001,
            slug="riyadh-city-hero-test",
            hostaway_name="Riyadh test stay",
            city="Riyadh",
        )
        PropertyImage.objects.create(
            property=property_obj,
            source=PropertyImage.Source.HOSTAWAY,
            hostaway_url="https://images.example.com/real-riyadh.jpg",
            sync_key="real-riyadh",
            is_city_hero=True,
            alt_text_ar="Arabic real Riyadh property",
            alt_text_en="A real Riyadh property",
        )

        with translation.override("en"):
            content = english_client().get("/").content.decode()

    assert "https://images.example.com/real-riyadh.jpg" in content
    assert "A real Riyadh property" in content
    assert "Riyadh fallback" not in content


def test_interface_image_admin_exposes_all_managed_fields() -> None:
    model_admin = SiteInterfaceImageAdmin(SiteInterfaceImage, admin.site)
    flattened_fields = {
        field for _, options in model_admin.fieldsets for field in options["fields"]
    }

    assert {
        "placement",
        "image",
        "image_preview",
        "alt_text_ar",
        "alt_text_en",
        "alt_text_fr",
        "is_active",
    } <= flattened_fields
    assert len(SiteInterfaceImage.Placement.choices) == 6
