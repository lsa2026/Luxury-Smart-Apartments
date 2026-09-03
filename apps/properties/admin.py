from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.cache import cache
from django.db import models, transaction
from django.http import HttpRequest
from django.utils.html import format_html

from apps.notifications.services.audit import record_audit

from .models import Amenity, Property, PropertyAmenity, PropertyImage

PROPERTY_SOURCE_FIELDS = (
    "hostaway_listing_id",
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
    "source_missing",
    "consecutive_missing_syncs",
    "last_seen_at",
    "publish_blockers",
    "hostaway_listing_map_id_verified_at",
    "hostaway_listing_map_id_verification_source",
    "created_at",
    "updated_at",
)

PROPERTY_LOCAL_FIELDS = {
    "slug",
    "name_ar",
    "name_en",
    "name_fr",
    "short_description_ar",
    "short_description_en",
    "short_description_fr",
    "description_ar",
    "description_en",
    "description_fr",
    "city_ar",
    "city_en",
    "city_fr",
    "seo_title_ar",
    "seo_title_en",
    "seo_title_fr",
    "seo_description_ar",
    "seo_description_en",
    "seo_description_fr",
    "is_visible",
    "is_featured",
    "sort_order",
}

PROPERTY_FORM_FIELDS = (
    "slug",
    "name_ar",
    "name_en",
    "name_fr",
    "short_description_ar",
    "short_description_en",
    "short_description_fr",
    "description_ar",
    "description_en",
    "description_fr",
    "city_ar",
    "city_en",
    "city_fr",
    "seo_title_ar",
    "seo_title_en",
    "seo_title_fr",
    "seo_description_ar",
    "seo_description_en",
    "seo_description_fr",
    "is_visible",
    "visibility_management",
    "is_featured",
    "sort_order",
    "content_is_customized",
)


class PropertyAdminForm(forms.ModelForm):
    class Meta:
        model = Property
        fields = PROPERTY_FORM_FIELDS
        labels = {
            "slug": "الرابط المختصر",
            "name_ar": "اسم الوحدة",
            "short_description_ar": "الوصف المختصر",
            "description_ar": "الوصف الكامل",
            "city_ar": "المدينة",
            "name_en": "Property name",
            "short_description_en": "Short description",
            "description_en": "Full description",
            "city_en": "City",
            "name_fr": "Nom du logement",
            "short_description_fr": "Description courte",
            "description_fr": "Description complète",
            "city_fr": "Ville",
            "seo_title_ar": "عنوان SEO",
            "seo_description_ar": "وصف SEO",
            "seo_title_en": "SEO title",
            "seo_description_en": "SEO description",
            "seo_title_fr": "Titre SEO",
            "seo_description_fr": "Description SEO",
            "is_visible": "ظاهرة في المنصة",
            "visibility_management": "إدارة الظهور",
            "is_featured": "وحدة مميّزة",
            "sort_order": "ترتيب الظهور",
            "content_is_customized": "المحتوى المحلي مخصّص",
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
        "title_fr",
        "alt_text_ar",
        "alt_text_en",
        "alt_text_fr",
        "caption_ar",
        "caption_en",
        "caption_fr",
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
    form = PropertyAdminForm
    list_display = (
        "local_name",
        "local_city",
        "capacity_display",
        "bedrooms_display",
        "source_active",
        "platform_visible",
        "featured_display",
        "image_count",
        "content_languages",
        "seo_complete",
        "last_sync",
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
        "name_fr",
        "hostaway_name",
        "address",
        "=hostaway_listing_id",
        "=hostaway_listing_map_id",
    )
    readonly_fields = PROPERTY_SOURCE_FIELDS
    actions = ("queue_selected_property_sync",)
    inlines = (PropertyImageInline, PropertyAmenityInline)
    list_per_page = 25
    fieldsets = (
        (
            "النشر والعرض",
            {
                "fields": (
                    "slug",
                    ("is_visible", "visibility_management"),
                    ("is_featured", "sort_order"),
                    "content_is_customized",
                )
            },
        ),
        (
            "المحتوى العربي",
            {
                "fields": (
                    "name_ar",
                    "short_description_ar",
                    "description_ar",
                    "city_ar",
                )
            },
        ),
        (
            "English content",
            {"fields": ("name_en", "short_description_en", "description_en", "city_en")},
        ),
        (
            "Contenu français",
            {"fields": ("name_fr", "short_description_fr", "description_fr", "city_fr")},
        ),
        (
            "SEO — العربية",
            {"fields": ("seo_title_ar", "seo_description_ar")},
        ),
        (
            "SEO — English",
            {"fields": ("seo_title_en", "seo_description_en")},
        ),
        (
            "SEO — Français",
            {"fields": ("seo_title_fr", "seo_description_fr")},
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
        return obj.name_ar or obj.name_en or obj.name_fr or "—"

    @admin.display(description="المدينة", ordering="city")
    def local_city(self, obj: Property) -> str:
        return obj.city_ar or obj.city_en or obj.city_fr or obj.city or "—"

    @admin.display(description="الضيوف", ordering="person_capacity")
    def capacity_display(self, obj: Property) -> int | str:
        return obj.person_capacity or "—"

    @admin.display(description="الغرف", ordering="bedrooms_number")
    def bedrooms_display(self, obj: Property) -> int | str:
        return obj.bedrooms_number or "—"

    @admin.display(boolean=True, description="نشط في Hostaway", ordering="hostaway_is_active")
    def source_active(self, obj: Property) -> bool:
        return obj.hostaway_is_active

    @admin.display(boolean=True, description="ظاهر في المنصة", ordering="is_visible")
    def platform_visible(self, obj: Property) -> bool:
        return obj.is_visible

    @admin.display(boolean=True, description="مميّز", ordering="is_featured")
    def featured_display(self, obj: Property) -> bool:
        return obj.is_featured

    @admin.display(description="الصور", ordering="_image_count")
    def image_count(self, obj: Property) -> int:
        return obj._image_count

    @admin.display(description="اكتمال اللغات")
    def content_languages(self, obj: Property) -> str:
        values = (
            ("AR", self.arabic_content_complete(obj)),
            ("EN", self.english_content_complete(obj)),
            ("FR", self.french_content_complete(obj)),
        )
        return " · ".join(f"{code} {'✓' if complete else '—'}" for code, complete in values)

    @admin.display(description="آخر مزامنة", ordering="last_synced_at")
    def last_sync(self, obj: Property) -> object:
        return obj.last_synced_at

    @admin.display(boolean=True, description="المحتوى العربي")
    def arabic_content_complete(self, obj: Property) -> bool:
        return bool(obj.name_ar and obj.description_ar and obj.city_ar)

    @admin.display(boolean=True, description="English content")
    def english_content_complete(self, obj: Property) -> bool:
        return bool(
            (obj.name_en or obj.hostaway_name)
            and (obj.description_en or obj.hostaway_description)
            and (obj.city_en or obj.city)
        )

    @admin.display(boolean=True, description="Contenu français")
    def french_content_complete(self, obj: Property) -> bool:
        return bool(obj.name_fr and obj.description_fr and obj.city_fr)

    @admin.display(boolean=True, description="SEO")
    def seo_complete(self, obj: Property) -> bool:
        return bool(
            obj.seo_title_ar
            and obj.seo_description_ar
            and obj.seo_title_en
            and obj.seo_description_en
            and obj.seo_title_fr
            and obj.seo_description_fr
        )

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
        if change and "is_visible" in changed_data:
            obj.visibility_management = Property.VisibilityManagement.MANUAL
        super().save_model(request, obj, form, change)
        cache.delete("seo:sitemap:v1")
        if change and changed_data:
            record_audit(
                request=request,
                action="property.content_changed",
                object_type="Property",
                object_reference=str(obj.pk),
                summary="Local property presentation was updated.",
                metadata={"fields": sorted(changed_data.intersection(PROPERTY_LOCAL_FIELDS))},
            )

    @admin.action(description="مزامنة وحدات Hostaway المحددة")
    def queue_selected_property_sync(
        self,
        request: HttpRequest,
        queryset: models.QuerySet[Property],
    ) -> None:
        if not request.user.is_superuser:
            self.message_user(request, "يتطلب الإجراء صلاحية عليا.", messages.ERROR)
            return
        listing_ids = list(queryset.values_list("hostaway_listing_id", flat=True)[:100])
        if not settings.CELERY_SYNC_DISPATCH_ENABLED:
            commands = "; ".join(
                f"python manage.py sync_hostaway_properties --listing-id {listing_id}"
                for listing_id in listing_ids
            )
            self.message_user(
                request,
                f"عامل المهام غير مفعّل. شغّل: {commands}",
                messages.WARNING,
            )
            return
        from apps.integrations.tasks import sync_hostaway_properties_task

        for listing_id in listing_ids:
            sync_hostaway_properties_task.delay(listing_id=listing_id)
        self.message_user(request, f"أضيفت {len(listing_ids)} وحدة إلى طابور المزامنة.")

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
    search_fields = (
        "property__name_ar",
        "property__name_en",
        "property__name_fr",
        "title_ar",
        "title_en",
        "title_fr",
    )
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
        changed_data = set(getattr(form, "changed_data", []))
        if changed_data.intersection({"is_visible", "is_cover", "sort_order"}):
            record_audit(
                request=request,
                action="property_image.presentation_changed",
                object_type="PropertyImage",
                object_reference=str(obj.pk),
                summary="Property image presentation was updated.",
                metadata={
                    "fields": sorted(
                        changed_data.intersection({"is_visible", "is_cover", "sort_order"})
                    )
                },
            )

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
    search_fields = ("name", "name_ar", "name_en", "name_fr")
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
    search_fields = (
        "property__name_ar",
        "property__name_en",
        "property__name_fr",
        "amenity__name",
    )
    readonly_fields = ("amenity", "source", "is_active_at_source")
