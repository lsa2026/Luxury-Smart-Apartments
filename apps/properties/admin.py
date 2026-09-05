from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.cache import cache
from django.db import models, transaction
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.notifications.services.audit import record_audit

from .models import Amenity, NearbyPlace, Property, PropertyAmenity, PropertyImage


def _demote_other_city_heroes(image: PropertyImage) -> None:
    """Leave one hero per city after ``image`` claims that city.

    The database can only cap this per property, because the city lives on
    Property. Clearing the rest here keeps the home page unambiguous: choosing a
    new photograph for a city releases the previous one rather than relying on
    tie-breaking.
    """
    if not (image.is_city_hero and image.is_visible):
        return
    city = getattr(image.property, "city", "")
    if not city:
        return
    PropertyImage.objects.filter(
        property__city=city,
        is_city_hero=True,
    ).exclude(pk=image.pk).update(is_city_hero=False)


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
    "min_nights",
    "max_nights",
    "cancellation_policy",
    "allow_same_day_booking",
    "same_day_booking_lead_time_hours",
    "check_in_time_start",
    "check_out_time",
    "instant_bookable",
    "time_zone_name",
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
    "house_rules_ar",
    "house_rules_en",
    "house_rules_fr",
    "public_location_enabled",
    "public_location_latitude",
    "public_location_longitude",
    "public_location_radius_m",
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
    "house_rules_ar",
    "house_rules_en",
    "house_rules_fr",
    "public_location_enabled",
    "public_location_latitude",
    "public_location_longitude",
    "public_location_radius_m",
    "content_is_customized",
)


class PropertyAdminForm(forms.ModelForm):
    class Meta:
        model = Property
        fields = PROPERTY_FORM_FIELDS
        # The fieldset heading states which content language a group belongs to,
        # so the three per-language variants of a field share one label.
        labels = {
            "slug": _("Short link"),
            "name_ar": _("Property name"),
            "short_description_ar": _("Short description"),
            "description_ar": _("Full description"),
            "city_ar": _("City"),
            "name_en": _("Property name"),
            "short_description_en": _("Short description"),
            "description_en": _("Full description"),
            "city_en": _("City"),
            "name_fr": _("Property name"),
            "short_description_fr": _("Short description"),
            "description_fr": _("Full description"),
            "city_fr": _("City"),
            "seo_title_ar": _("SEO title"),
            "seo_description_ar": _("SEO description"),
            "seo_title_en": _("SEO title"),
            "seo_description_en": _("SEO description"),
            "seo_title_fr": _("SEO title"),
            "seo_description_fr": _("SEO description"),
            "is_visible": _("Visible on the platform"),
            "visibility_management": _("Visibility management"),
            "is_featured": _("Featured property"),
            "sort_order": _("Display order"),
            "content_is_customized": _("Local content is customised"),
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
        "is_city_hero",
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

    @admin.display(description=_("Preview"))
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


class NearbyPlaceInline(admin.TabularInline):
    model = NearbyPlace
    extra = 1
    fields = (
        "name_ar",
        "distance_ar",
        "name_en",
        "distance_en",
        "name_fr",
        "distance_fr",
        "is_active",
        "sort_order",
    )


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
        "public_image_ready",
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
    readonly_fields = PROPERTY_SOURCE_FIELDS + (
        "public_image_warning",
        "asset_management",
    )
    actions = ("queue_selected_property_sync",)
    # Large Hostaway listings can contain dozens of images and amenities. They
    # stay fully manageable on their dedicated screens instead of making the
    # main property form several pages long.
    inlines = (NearbyPlaceInline,)
    list_per_page = 25
    fieldsets = (
        (
            _("Publishing and display"),
            {
                "fields": (
                    "slug",
                    ("is_visible", "visibility_management"),
                    "public_image_warning",
                    ("is_featured", "sort_order"),
                    "content_is_customized",
                )
            },
        ),
        (
            _("Approximate public location"),
            {
                "fields": (
                    "public_location_enabled",
                    ("public_location_latitude", "public_location_longitude"),
                    "public_location_radius_m",
                ),
                "description": _(
                    "Choose a neighbourhood centre, not the exact property point. "
                    "The public circle must contain the source location while keeping "
                    "its centre at least 100 metres away."
                ),
            },
        ),
        (
            _("Arabic content"),
            {
                "fields": (
                    "name_ar",
                    "short_description_ar",
                    "description_ar",
                    "city_ar",
                    "house_rules_ar",
                )
            },
        ),
        (
            _("English content"),
            {
                "classes": ("collapse",),
                "fields": (
                    "name_en",
                    "short_description_en",
                    "description_en",
                    "city_en",
                    "house_rules_en",
                ),
            },
        ),
        (
            _("French content"),
            {
                "classes": ("collapse",),
                "fields": (
                    "name_fr",
                    "short_description_fr",
                    "description_fr",
                    "city_fr",
                    "house_rules_fr",
                ),
            },
        ),
        (
            _("SEO — Arabic"),
            {
                "classes": ("collapse",),
                "fields": ("seo_title_ar", "seo_description_ar"),
            },
        ),
        (
            _("SEO — English"),
            {
                "classes": ("collapse",),
                "fields": ("seo_title_en", "seo_description_en"),
            },
        ),
        (
            _("SEO — French"),
            {
                "classes": ("collapse",),
                "fields": ("seo_title_fr", "seo_description_fr"),
            },
        ),
        (
            _("Images and amenities"),
            {
                "fields": ("asset_management",),
                "description": _("Manage large image and amenity collections on focused screens."),
            },
        ),
        (
            _("Hostaway operational data"),
            {
                "classes": ("collapse",),
                "fields": PROPERTY_SOURCE_FIELDS,
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[Property]:
        return (
            super()
            .get_queryset(request)
            .annotate(
                _image_count=models.Count("images", distinct=True),
                _public_image_count=models.Count(
                    "images",
                    filter=models.Q(images__is_visible=True)
                    & (
                        models.Q(images__source=PropertyImage.Source.LOCAL)
                        | models.Q(
                            images__source=PropertyImage.Source.HOSTAWAY,
                            images__is_active_at_source=True,
                        )
                    ),
                    distinct=True,
                ),
                _amenity_count=models.Count("property_amenities", distinct=True),
            )
        )

    @admin.display(description=_("Published image warning"))
    def public_image_warning(self, obj: Property) -> str:
        if not obj.pk or not obj.is_visible or not obj.hostaway_is_active:
            return "—"
        count = getattr(obj, "_public_image_count", None)
        if count is None:
            count = obj.images.public().count()
        if count:
            return str(_("A public image is available."))
        return format_html(
            '<p class="errornote">{}</p>',
            _(
                "Warning: this published property has no visible image. Guests will "
                "see the branded fallback until a public image is restored."
            ),
        )

    @admin.display(description=_("Media and amenities management"))
    def asset_management(self, obj: Property) -> str:
        if not obj.pk:
            return str(_("Save the property first, then manage images and amenities."))
        images_url = reverse("admin:properties_propertyimage_changelist")
        images_url = f"{images_url}?property__id__exact={obj.pk}"
        amenities_url = reverse("admin:properties_propertyamenity_changelist")
        amenities_url = f"{amenities_url}?property__id__exact={obj.pk}"
        alt_text_url = reverse("properties_admin:image_alt_text")
        alt_text_url = f"{alt_text_url}?property={obj.pk}"
        image_count = getattr(obj, "_image_count", None)
        if image_count is None:
            image_count = obj.images.count()
        amenity_count = getattr(obj, "_amenity_count", None)
        if amenity_count is None:
            amenity_count = obj.property_amenities.count()
        return format_html(
            '<div class="lsa-admin-action-hub">'
            '<p>{}</p><a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a></div>',
            _("%(images)d images · %(amenities)d amenities")
            % {"images": image_count, "amenities": amenity_count},
            images_url,
            _("Manage images"),
            alt_text_url,
            _("Edit alternative text"),
            amenities_url,
            _("Manage amenities"),
        )

    @admin.display(description=_("Local name"), ordering="name_ar")
    def local_name(self, obj: Property) -> str:
        return obj.name_ar or obj.name_en or obj.name_fr or "—"

    @admin.display(description=_("City"), ordering="city")
    def local_city(self, obj: Property) -> str:
        return obj.city_ar or obj.city_en or obj.city_fr or obj.city or "—"

    @admin.display(description=_("Guests"), ordering="person_capacity")
    def capacity_display(self, obj: Property) -> int | str:
        return obj.person_capacity or "—"

    @admin.display(description=_("Bedrooms"), ordering="bedrooms_number")
    def bedrooms_display(self, obj: Property) -> int | str:
        return obj.bedrooms_number or "—"

    @admin.display(boolean=True, description=_("Active in Hostaway"), ordering="hostaway_is_active")
    def source_active(self, obj: Property) -> bool:
        return obj.hostaway_is_active

    @admin.display(boolean=True, description=_("Visible on the platform"), ordering="is_visible")
    def platform_visible(self, obj: Property) -> bool:
        return obj.is_visible

    @admin.display(boolean=True, description=_("Featured"), ordering="is_featured")
    def featured_display(self, obj: Property) -> bool:
        return obj.is_featured

    @admin.display(description=_("Images"), ordering="_image_count")
    def image_count(self, obj: Property) -> int:
        return obj._image_count

    @admin.display(
        boolean=True,
        description=_("Public image"),
        ordering="_public_image_count",
    )
    def public_image_ready(self, obj: Property) -> bool:
        return bool(obj._public_image_count)

    @admin.display(description=_("Language completeness"))
    def content_languages(self, obj: Property) -> str:
        values = (
            ("AR", self.arabic_content_complete(obj)),
            ("EN", self.english_content_complete(obj)),
            ("FR", self.french_content_complete(obj)),
        )
        return " · ".join(f"{code} {'✓' if complete else '—'}" for code, complete in values)

    @admin.display(description=_("Last sync"), ordering="last_synced_at")
    def last_sync(self, obj: Property) -> object:
        return obj.last_synced_at

    @admin.display(boolean=True, description=_("Arabic content"))
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

    @admin.action(description=_("Sync the selected Hostaway properties"))
    def queue_selected_property_sync(
        self,
        request: HttpRequest,
        queryset: models.QuerySet[Property],
    ) -> None:
        if not request.user.is_superuser:
            self.message_user(
                request,
                _("This action requires elevated permissions."),
                messages.ERROR,
            )
            return
        listing_ids = list(queryset.values_list("hostaway_listing_id", flat=True)[:100])
        if not settings.CELERY_SYNC_DISPATCH_ENABLED:
            commands = "; ".join(
                f"python manage.py sync_hostaway_properties --listing-id {listing_id}"
                for listing_id in listing_ids
            )
            self.message_user(
                request,
                _("The task worker is not running. Start it with: %(commands)s")
                % {"commands": commands},
                messages.WARNING,
            )
            return
        from apps.integrations.tasks import sync_hostaway_properties_task

        for listing_id in listing_ids:
            sync_hostaway_properties_task.delay(listing_id=listing_id)
        self.message_user(
            request,
            _("Added %(count)d property to the sync queue.") % {"count": len(listing_ids)},
        )

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
                if isinstance(instance, PropertyImage):
                    _demote_other_city_heroes(instance)
            formset.save_m2m()


@admin.register(PropertyImage)
class PropertyImageAdmin(admin.ModelAdmin):
    list_display = (
        "preview",
        "property",
        "source",
        "is_visible",
        "is_cover",
        "is_city_hero",
        "sort_order",
        "is_active_at_source",
    )
    list_filter = ("source", "is_visible", "is_cover", "is_city_hero", "is_active_at_source")
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

    @admin.display(description=_("Preview"))
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
        _demote_other_city_heroes(obj)
        changed_data = set(getattr(form, "changed_data", []))
        if changed_data.intersection({"is_visible", "is_cover", "is_city_hero", "sort_order"}):
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
