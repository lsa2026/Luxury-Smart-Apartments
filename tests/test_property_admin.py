import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory

from apps.properties.admin import PropertyAdmin, PropertyImageAdmin
from apps.properties.models import Property, PropertyImage

pytestmark = pytest.mark.django_db


def test_property_source_fields_are_read_only_in_admin() -> None:
    user = get_user_model().objects.create_user(
        username="property-editor",
        password="test-password",
        is_staff=True,
    )
    user.user_permissions.add(Permission.objects.get(codename="change_property"))
    request = RequestFactory().get("/admin/properties/property/")
    request.user = user
    model_admin = PropertyAdmin(Property, AdminSite())

    readonly = model_admin.get_readonly_fields(request)
    assert "hostaway_listing_id" in readonly
    assert "hostaway_listing_map_id" in readonly
    assert "hostaway_name" in readonly
    assert "hostaway_property_type_id" in readonly
    assert "hostaway_special_status" in readonly
    assert "person_capacity" in readonly
    assert "name_ar" not in readonly
    assert "seo_title_ar" not in readonly
    assert "is_visible" not in readonly


def test_only_local_images_can_be_deleted_in_admin() -> None:
    user = get_user_model().objects.create_user(
        username="image-editor",
        password="test-password",
        is_staff=True,
    )
    user.user_permissions.add(Permission.objects.get(codename="delete_propertyimage"))
    property_obj = Property.objects.create(
        hostaway_listing_id=500,
        slug="admin-property",
        name_en="Admin property",
    )
    local = PropertyImage.objects.create(
        property=property_obj,
        image="properties/500/local.jpg",
        source=PropertyImage.Source.LOCAL,
    )
    hostaway = PropertyImage.objects.create(
        property=property_obj,
        hostaway_image_id=501,
        hostaway_url="https://hostaway.example/501.jpg",
        sync_key="id:501",
        source=PropertyImage.Source.HOSTAWAY,
    )
    request = RequestFactory().get("/admin/properties/propertyimage/")
    request.user = user
    model_admin = PropertyImageAdmin(PropertyImage, AdminSite())

    assert model_admin.has_delete_permission(request, local) is True
    assert model_admin.has_delete_permission(request, hostaway) is False
    assert model_admin.has_delete_permission(request) is False
