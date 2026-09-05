from decimal import Decimal

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory

from apps.properties.admin import PropertyAdmin, PropertyAdminForm, PropertyImageAdmin
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


def test_property_editor_uses_focused_asset_screens_and_collapsed_secondary_content() -> None:
    model_admin = PropertyAdmin(Property, AdminSite())
    property_obj = Property.objects.create(
        hostaway_listing_id=550,
        slug="focused-property-editor",
        name_ar="وحدة مرتبة",
    )

    assert model_admin.inlines == ()
    assert "asset_management" in model_admin.readonly_fields
    assert "إدارة الصور" in str(model_admin.asset_management(property_obj))
    collapsed_sections = {
        str(name)
        for name, options in model_admin.fieldsets
        if "collapse" in options.get("classes", ())
    }
    assert "المحتوى الإنجليزي" in collapsed_sections
    assert "المحتوى الفرنسي" in collapsed_sections
    assert "SEO — العربية" in collapsed_sections


def test_property_location_is_selected_on_a_map_without_visible_coordinate_fields() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=551,
        slug="map-property-editor",
        name_ar="وحدة بخريطة",
        latitude=Decimal("24.713552"),
        longitude=Decimal("46.675296"),
    )
    form = PropertyAdminForm(instance=property_obj)
    model_admin = PropertyAdmin(Property, AdminSite())

    assert form.fields["public_location_latitude"].widget.input_type == "hidden"
    assert form.fields["public_location_longitude"].widget.input_type == "hidden"
    assert form.initial["public_location_latitude"] is None
    assert form.initial["public_location_longitude"] is None
    picker = str(model_admin.location_picker(property_obj))
    assert "data-location-picker" in picker
    assert 'data-default-latitude="24.713552"' in picker
    assert 'data-default-longitude="46.675296"' in picker
    assert "vendor/leaflet/leaflet.js" in str(model_admin.media)


def test_enabling_public_location_requires_a_point_chosen_on_the_map() -> None:
    property_obj = Property.objects.create(
        hostaway_listing_id=552,
        slug="map-required-property",
        name_ar="وحدة بلا موقع",
    )
    form = PropertyAdminForm(
        data={
            "slug": property_obj.slug,
            "visibility_management": Property.VisibilityManagement.MANUAL,
            "public_location_enabled": "on",
            "public_location_latitude": "",
            "public_location_longitude": "",
        },
        instance=property_obj,
    )

    assert form.is_valid() is False
    assert "public_location_enabled" in form.errors


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
