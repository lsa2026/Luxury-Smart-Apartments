import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.properties.models import Property, PropertyImage


@pytest.mark.django_db
def test_alt_editor_lists_and_saves(client):
    User = get_user_model()
    staff = User.objects.create_superuser(username="boss", email="b@x.com", password="pw12345!")
    prop = Property.objects.create(
        slug="p1",
        hostaway_listing_id=99001,
        name_ar="وحدة",
        name_en="Unit",
        name_fr="Unite",
    )
    img = PropertyImage.objects.create(
        property=prop,
        source=PropertyImage.Source.HOSTAWAY,
        hostaway_url="https://example.com/a.jpg",
        hostaway_image_id=1,
    )
    client.force_login(staff)
    url = reverse("properties_admin:image_alt_text")

    r = client.get(url)
    assert r.status_code == 200
    assert b"alt_text_ar-" in r.content

    r = client.post(
        url,
        {
            "property": str(prop.pk),
            f"alt_text_ar-{img.pk}": "صورة الصالة",
            f"alt_text_en-{img.pk}": "Living room",
            f"alt_text_fr-{img.pk}": "Salon",
        },
    )
    assert r.status_code == 302
    img.refresh_from_db()
    assert (img.alt_text_ar, img.alt_text_en, img.alt_text_fr) == (
        "صورة الصالة",
        "Living room",
        "Salon",
    )

    # now complete, so the "only missing" filter should hide it
    r = client.get(url, {"property": prop.pk, "missing": "1"})
    assert f"alt_text_ar-{img.pk}".encode() not in r.content


@pytest.mark.django_db
def test_alt_editor_rejects_non_staff(client):
    User = get_user_model()
    User.objects.create_user(username="guest", email="g@x.com", password="pw12345!")
    client.login(username="guest", password="pw12345!")
    r = client.get(reverse("properties_admin:image_alt_text"))
    assert r.status_code in (302, 403)
