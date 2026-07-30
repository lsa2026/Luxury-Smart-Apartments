from django.contrib import admin
from django.db import models, transaction
from django.http import HttpRequest
from django.utils.html import format_html

from .models import Amenity, Property, PropertyAmenity, PropertyImage

PROPERTY_SOURCE_FIELDS = (
    "hostaway_listing_map_id",
    "hostaway_name",
    "hostaway_description",
    "hostaway_internal_name",
    "hostaway_property_type_id",
    "room_type",
    "person_capacity",
    "bedrooms_number",
    "beds_number",
    "bathrooms_number",
    "address",
    "public_address",
    "city",
    "state",
    "country",
    "country_code",
    "zipcode",
    "latitude",
    "longitude",
    "currency_code",
    "average_review_rating",
    "hostaway_special_status",
    "hostaway_is_active",
    "imported_at",
    "last_synced_at",
    "source_updated_at",
    "created_at",
    "updated_at",
)

PROPERTY_LOCAL_FIELDS = {
    "slug",
    "name_ar",
    "name_en",
    "short_description_ar",
    "short_description_en",
    "description_ar",
    "description_en",
    "city_ar",
    "city_en",
    "seo_title_ar",
    "seo_title_en",
    "seo_description_ar",
    "seo_description_en",
    "is_visible",
    "is_featured",
    "sort_order",
}


class PropertyImageInline(admin.TabularInline):
    model = PropertyImage
    extra = 0
    can_delete = False
    fields = (
        "preview",
        "source",
        "image",
        "hostaway_image_id",
        "title_ar",
        "title_en",
        "alt_text_ar",
        "alt_text_en",
        "caption_ar",
        "caption_en",
        "sort_order",
        "hostaway_sort_order",
        "is_cover",
        "is_visible",
        "is_active_at_source",
    )
    readonly_fields = (
        "preview",
        "source",
        "hostaway_image_id",
        "hostaway_sort_order",
        "is_active_at_source",
    )

    @admin.display(description="معاينة")
    def preview(self, obj: PropertyImage) -> str:
        if not obj.pk or not obj.display_url:
            return "—"
        return format_html(
            '<img src="{}" alt="" width="120" height="80" '
            'style="object-fit:cover;border-radius:6px">',
            obj.display_url,
        )


class PropertyAmenityInline(admin.TabularInline):
    model = PropertyAmenity
    extra = 0
    fields = ("amenity", "source", "is_visible", "sort_order", "is_active_at_source")
    readonly_fields = ("amenity", "source", "is_active_at_source")
    can_delete = False


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = (
        "local_name",
        "hostaway_name",
        "city",
        "person_capacity",
        "bedrooms_number",
        "hostaway_is_active",
        "is_visible",
        "is_featured",
        "last_synced_at",
        "image_count",
    )
    list_filter = (
        "city",
        "country_code",
        "hostaway_is_active",
        "is_visible",
        "is_featured",
        "last_synced_at",
    )
    search_fields = (
        "name_ar",
        "name_en",
        "hostaway_name",
        "address",
        "=hostaway_listing_map_id",
    )
    readonly_fields = PROPERTY_SOURCE_FIELDS
    inlines = (PropertyImageInline, PropertyAmenityInline)
    fieldsets = (
        (
            "المحتوى المحلي",
            {
                "fields": (
                    "slug",
                    ("name_ar", "name_en"),
                    ("short_description_ar", "short_description_en"),
                    ("description_ar", "description_en"),
                    ("city_ar", "city_en"),
                    ("is_visible", "is_featured", "sort_order"),
                    "content_is_customized",
                )
            },
        ),
        (
            "SEO",
            {
                "fields": (
                    ("seo_title_ar", "seo_title_en"),
                    ("seo_description_ar", "seo_description_en"),
                )
            },
        ),
        (
            "بيانات Hostaway التشغيلية",
            {
                "classes": ("collapse",),
                "fields": PROPERTY_SOURCE_FIELDS,
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[Property]:
        return super().get_queryset(request).annotate(_image_count=models.Count("images"))

    @admin.display(description="الاسم المحلي", ordering="name_ar")
    def local_name(self, obj: Property) -> str:
        return obj.name_ar or obj.name_en or "—"

    @admin.display(description="الصور", ordering="_image_count")
    def image_count(self, obj: Property) -> int:
        return obj._image_count

    def save_model(
        self,
        request: HttpRequest,
        obj: Property,
        form: object,
        change: bool,
    ) -> None:
        changed_data = set(getattr(form, "changed_data", []))
        if change and changed_data.intersection(PROPERTY_LOCAL_FIELDS):
            obj.content_is_customized = True
        super().save_model(request, obj, form, change)

    def save_formset(
        self,
        request: HttpRequest,
        form: object,
        formset: object,
        change: bool,
    ) -> None:
        with transaction.atomic():
            instances = formset.save(commit=False)
            for instance in instances:
                if isinstance(instance, PropertyImage) and instance.pk is None:
                    instance.source = PropertyImage.Source.LOCAL
                if (
                    isinstance(instance, PropertyImage)
                    and instance.is_cover
                    and instance.is_visible
                ):
                    PropertyImage.objects.filter(
                        property=instance.property,
                        is_cover=True,
                        is_visible=True,
                    ).exclude(pk=instance.pk).update(is_cover=False)
                instance.save()
            formset.save_m2m()


@admin.register(PropertyImage)
class PropertyImageAdmin(admin.ModelAdmin):
    list_display = (
        "preview",
        "property",
        "source",
        "is_visible",
        "is_cover",
        "sort_order",
        "is_active_at_source",
    )
    list_filter = ("source", "is_visible", "is_cover", "is_active_at_source")
    search_fields = ("property__name_ar", "property__name_en", "title_ar", "title_en")
    readonly_fields = (
        "preview",
        "source",
        "hostaway_image_id",
        "hostaway_url",
        "hostaway_caption",
        "sync_key",
        "hostaway_sort_order",
        "is_active_at_source",
        "source_updated_at",
        "created_at",
        "updated_at",
    )

    @admin.display(description="معاينة")
    def preview(self, obj: PropertyImage) -> str:
        if not obj.pk or not obj.display_url:
            return "—"
        return format_html(
            '<img src="{}" alt="" width="140" height="90" '
            'style="object-fit:cover;border-radius:6px">',
            obj.display_url,
        )

    def save_model(
        self,
        request: HttpRequest,
        obj: PropertyImage,
        form: object,
        change: bool,
    ) -> None:
        if not change:
            obj.source = PropertyImage.Source.LOCAL
        if obj.is_cover and obj.is_visible:
            PropertyImage.objects.filter(
                property=obj.property,
                is_cover=True,
                is_visible=True,
            ).exclude(pk=obj.pk).update(is_cover=False)
        super().save_model(request, obj, form, change)

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: PropertyImage | None = None,
    ) -> bool:
        return bool(
            obj
            and obj.source == PropertyImage.Source.LOCAL
            and super().has_delete_permission(request, obj)
        )

    def get_actions(self, request: HttpRequest) -> dict[str, object]:
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(Amenity)
class AmenityAdmin(admin.ModelAdmin):
    list_display = ("display_name", "hostaway_amenity_id", "category", "is_active")
    list_filter = ("is_active", "category")
    search_fields = ("name", "name_ar", "name_en")
    readonly_fields = ("hostaway_amenity_id", "name", "created_at", "updated_at")


@admin.register(PropertyAmenity)
class PropertyAmenityAdmin(admin.ModelAdmin):
    list_display = (
        "property",
        "amenity",
        "source",
        "is_visible",
        "is_active_at_source",
        "sort_order",
    )
    list_filter = ("source", "is_visible", "is_active_at_source")
    search_fields = ("property__name_ar", "property__name_en", "amenity__name")
    readonly_fields = ("amenity", "source", "is_active_at_source")
