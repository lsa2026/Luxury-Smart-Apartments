from builtins import property as builtin_property

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from .uploads import property_image_upload_to, validate_property_image


class PropertyQuerySet(models.QuerySet["Property"]):
    def public(self) -> "PropertyQuerySet":
        return self.filter(is_visible=True, hostaway_is_active=True)


class Property(models.Model):
    """A bookable property linked to a Hostaway listing."""

    class VisibilityManagement(models.TextChoices):
        AUTOMATIC = "automatic", _("Automatic")
        MANUAL = "manual", _("Manual")

    hostaway_listing_id = models.PositiveBigIntegerField(unique=True)
    hostaway_listing_map_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
    )
    hostaway_name = models.CharField(max_length=255, blank=True)
    hostaway_description = models.TextField(blank=True)
    hostaway_internal_name = models.CharField(max_length=255, blank=True)
    hostaway_property_type_id = models.PositiveIntegerField(null=True, blank=True)
    room_type = models.CharField(max_length=50, blank=True)
    person_capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    bedrooms_number = models.PositiveSmallIntegerField(null=True, blank=True)
    beds_number = models.PositiveSmallIntegerField(null=True, blank=True)
    bathrooms_number = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
    )
    address = models.CharField(max_length=500, blank=True)
    public_address = models.CharField(max_length=500, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=120, blank=True)
    country = models.CharField(max_length=120, blank=True)
    country_code = models.CharField(max_length=2, blank=True)
    zipcode = models.CharField(max_length=30, blank=True)
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    currency_code = models.CharField(max_length=3, blank=True)
    average_review_rating = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        null=True,
        blank=True,
    )
    hostaway_special_status = models.CharField(max_length=100, blank=True)
    hostaway_is_active = models.BooleanField(default=True)
    imported_at = models.DateTimeField(default=timezone.now, editable=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)

    slug = models.SlugField(max_length=180, unique=True)
    name_ar = models.CharField(max_length=200, blank=True)
    name_en = models.CharField(max_length=200, blank=True)
    name_fr = models.CharField(max_length=200, blank=True)
    short_description_ar = models.CharField(max_length=350, blank=True)
    short_description_en = models.CharField(max_length=350, blank=True)
    short_description_fr = models.CharField(max_length=350, blank=True)
    description_ar = models.TextField(blank=True)
    description_en = models.TextField(blank=True)
    description_fr = models.TextField(blank=True)
    city_ar = models.CharField(max_length=120, blank=True)
    city_en = models.CharField(max_length=120, blank=True)
    city_fr = models.CharField(max_length=120, blank=True)
    seo_title_ar = models.CharField(max_length=255, blank=True)
    seo_title_en = models.CharField(max_length=255, blank=True)
    seo_title_fr = models.CharField(max_length=255, blank=True)
    seo_description_ar = models.CharField(max_length=320, blank=True)
    seo_description_en = models.CharField(max_length=320, blank=True)
    seo_description_fr = models.CharField(max_length=320, blank=True)
    is_visible = models.BooleanField(default=True)
    visibility_management = models.CharField(
        max_length=12,
        choices=VisibilityManagement.choices,
        default=VisibilityManagement.MANUAL,
    )
    publish_blockers = models.JSONField(default=list, blank=True)
    source_missing = models.BooleanField(default=False)
    consecutive_missing_syncs = models.PositiveSmallIntegerField(default=0)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    hostaway_listing_map_id_verified_at = models.DateTimeField(null=True, blank=True)
    hostaway_listing_map_id_verification_source = models.CharField(max_length=100, blank=True)
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    content_is_customized = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PropertyQuerySet.as_manager()

    class Meta:
        ordering = ["-is_featured", "sort_order", "name_ar", "id"]
        indexes = [
            models.Index(fields=["is_visible", "hostaway_is_active", "-is_featured"]),
            models.Index(fields=["sort_order", "id"]),
            models.Index(fields=["city", "hostaway_is_active"]),
            models.Index(fields=["country_code", "hostaway_is_active"]),
            models.Index(fields=["last_synced_at"]),
            models.Index(fields=["source_missing", "consecutive_missing_syncs"]),
            models.Index(fields=["visibility_management", "is_visible"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["hostaway_listing_map_id"],
                condition=Q(hostaway_listing_map_id__isnull=False),
                name="unique_property_listing_map_id_when_set",
            ),
            models.CheckConstraint(
                condition=Q(country_code="") | Q(country_code__regex=r"^[A-Za-z]{2}$"),
                name="property_country_code_iso2",
            ),
            models.CheckConstraint(
                condition=Q(currency_code="") | Q(currency_code__regex=r"^[A-Za-z]{3}$"),
                name="property_currency_code_iso3",
            ),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True) | Q(latitude__gte=-90, latitude__lte=90),
                name="property_valid_latitude",
            ),
            models.CheckConstraint(
                condition=Q(longitude__isnull=True) | Q(longitude__gte=-180, longitude__lte=180),
                name="property_valid_longitude",
            ),
            models.CheckConstraint(
                condition=Q(average_review_rating__isnull=True)
                | Q(average_review_rating__gte=0, average_review_rating__lte=10),
                name="property_review_rating_0_10",
            ),
        ]
        verbose_name = _("Property")
        verbose_name_plural = _("Properties")

    def __str__(self) -> str:
        return (
            self.name_ar
            or self.name_en
            or self.name_fr
            or self.hostaway_name
            or str(self.hostaway_listing_id)
        )

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk and not getattr(self, "_sync_managed_visibility", False):
            previous_visibility = (
                type(self).objects.filter(pk=self.pk).values_list("is_visible", flat=True).first()
            )
            if previous_visibility is not None and previous_visibility != self.is_visible:
                self.visibility_management = self.VisibilityManagement.MANUAL
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {"visibility_management"}
        self.hostaway_is_active = self.derive_hostaway_is_active(self.hostaway_special_status)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"hostaway_is_active"}
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("properties:detail", kwargs={"slug": self.slug})

    @staticmethod
    def derive_hostaway_is_active(special_status: str) -> bool:
        """Derive activity without assuming future Hostaway status values."""
        return special_status.strip().casefold() != "archived"

    @builtin_property
    def display_name(self) -> str:
        language = (translation.get_language() or "ar").split("-")[0]
        values = {
            "ar": (self.name_ar, self.name_en, self.hostaway_name, self.name_fr),
            "en": (self.name_en, self.hostaway_name, self.name_fr, self.name_ar),
            "fr": (self.name_fr, self.name_en, self.hostaway_name, self.name_ar),
        }
        return next((value for value in values.get(language, values["ar"]) if value), "")

    @builtin_property
    def display_city(self) -> str:
        language = (translation.get_language() or "ar").split("-")[0]
        values = {
            "ar": (self.city_ar, self.city_en, self.city, self.city_fr),
            "en": (self.city_en, self.city, self.city_fr, self.city_ar),
            "fr": (self.city_fr, self.city_en, self.city, self.city_ar),
        }
        return next((value for value in values.get(language, values["ar"]) if value), "")

    @builtin_property
    def cover_image(self) -> "PropertyImage | None":
        images = getattr(self, "_public_images", None)
        if images is None:
            images = list(self.images.public())
        return next((image for image in images if image.is_cover), images[0] if images else None)

    @builtin_property
    def preview_images(self) -> list["PropertyImage"]:
        """Return the small, already-prefetched image set used by listing cards."""
        images = getattr(self, "_public_images", None)
        if images is None:
            images = list(self.images.public()[:5])
        return list(images)


class PropertyImageQuerySet(models.QuerySet["PropertyImage"]):
    def public(self) -> "PropertyImageQuerySet":
        return self.filter(is_visible=True).filter(
            Q(source=PropertyImage.Source.LOCAL)
            | Q(source=PropertyImage.Source.HOSTAWAY, is_active_at_source=True)
        )

    def city_heroes(self) -> "PropertyImageQuerySet":
        """Public hero images, ordered so the first one per city always wins.

        A city may hold several properties and each may nominate a hero, so the
        order settles the tie the same way the property listing does rather than
        leaving the home page to vary between requests.
        """
        return (
            self.public()
            .filter(is_city_hero=True)
            .filter(property__is_visible=True, property__hostaway_is_active=True)
            .exclude(property__city="")
            .select_related("property")
            .order_by(
                "property__city",
                "-property__is_featured",
                "property__sort_order",
                "property_id",
                "id",
            )
        )


class PropertyImage(models.Model):
    class Source(models.TextChoices):
        HOSTAWAY = "hostaway", "Hostaway"
        LOCAL = "local", pgettext_lazy("PropertyImage", "Local")

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="images")
    hostaway_image_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    hostaway_url = models.URLField(max_length=2048, blank=True)
    hostaway_caption = models.CharField(max_length=500, blank=True)
    sync_key = models.CharField(max_length=72, blank=True)
    image = models.ImageField(
        upload_to=property_image_upload_to,
        validators=[validate_property_image],
        blank=True,
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.LOCAL)
    title_ar = models.CharField(max_length=255, blank=True)
    title_en = models.CharField(max_length=255, blank=True)
    title_fr = models.CharField(max_length=255, blank=True)
    alt_text_ar = models.CharField(max_length=255, blank=True)
    alt_text_en = models.CharField(max_length=255, blank=True)
    alt_text_fr = models.CharField(max_length=255, blank=True)
    caption_ar = models.CharField(max_length=500, blank=True)
    caption_en = models.CharField(max_length=500, blank=True)
    caption_fr = models.CharField(max_length=500, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    hostaway_sort_order = models.PositiveIntegerField(default=0)
    is_cover = models.BooleanField(default=False)
    # Marks the one photo that represents this property's city on the home page.
    # Kept separate from is_cover so the city card and the property card can show
    # different photos: the cover sells the unit, the hero sells the destination.
    is_city_hero = models.BooleanField(
        default=False,
        verbose_name=_("Represents its city on the home page"),
    )
    is_visible = models.BooleanField(default=True)
    is_active_at_source = models.BooleanField(default=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PropertyImageQuerySet.as_manager()

    class Meta:
        ordering = ["sort_order", "hostaway_sort_order", "id"]
        indexes = [
            models.Index(fields=["property", "source", "is_visible", "is_active_at_source"]),
            models.Index(fields=["property", "sort_order"]),
            models.Index(fields=["property", "is_cover"]),
            models.Index(fields=["property", "is_city_hero"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(hostaway_url="") | ~Q(image=""),
                name="property_image_has_url_or_file",
            ),
            models.UniqueConstraint(
                fields=["property"],
                condition=Q(is_cover=True, is_visible=True),
                name="one_visible_cover_per_property",
            ),
            # A city can hold several properties, and "one hero per city" is not
            # expressible here because the city lives on Property. Capping it at
            # one per property keeps the choice unambiguous within a listing;
            # the home page then picks deterministically between properties.
            models.UniqueConstraint(
                fields=["property"],
                condition=Q(is_city_hero=True, is_visible=True),
                name="one_visible_city_hero_per_property",
            ),
            models.UniqueConstraint(
                fields=["property", "sync_key"],
                condition=Q(source="hostaway"),
                name="unique_hostaway_image_sync_key",
            ),
        ]
        verbose_name = _("Property image")
        verbose_name_plural = _("Property images")

    def __str__(self) -> str:
        return (
            self.title_ar
            or self.title_en
            or self.title_fr
            or self.hostaway_caption
            or _("Image %(pk)s") % {"pk": self.pk or ""}
        )

    def clean(self) -> None:
        super().clean()
        if self.source == self.Source.LOCAL and not self.image:
            raise ValidationError({"image": "A local image file is required."})
        if self.source == self.Source.HOSTAWAY and not self.hostaway_url:
            raise ValidationError({"hostaway_url": "A Hostaway image URL is required."})

    @builtin_property
    def display_url(self) -> str:
        if self.source == self.Source.LOCAL and self.image:
            return self.image.url
        return self.hostaway_url

    def alt_text(self, language_code: str = "ar") -> str:
        if language_code == "fr":
            return (
                self.alt_text_fr
                or self.title_fr
                or self.property.name_fr
                or self.alt_text_en
                or self.title_en
                or self.property.name_en
                or self.hostaway_caption
                or self.property.hostaway_name
                or self.alt_text_ar
                or self.title_ar
                or self.property.name_ar
            )
        if language_code == "en":
            return (
                self.alt_text_en
                or self.title_en
                or self.property.name_en
                or self.hostaway_caption
                or self.property.hostaway_name
                or self.alt_text_fr
                or self.title_fr
                or self.property.name_fr
                or self.alt_text_ar
                or self.title_ar
                or self.property.name_ar
            )
        return (
            self.alt_text_ar
            or self.title_ar
            or self.property.name_ar
            or self.alt_text_en
            or self.title_en
            or self.property.name_en
            or self.hostaway_caption
            or self.property.hostaway_name
            or self.alt_text_fr
            or self.title_fr
            or self.property.name_fr
        )


class Amenity(models.Model):
    hostaway_amenity_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    name = models.CharField(max_length=255, blank=True)
    name_ar = models.CharField(max_length=255, blank=True)
    name_en = models.CharField(max_length=255, blank=True)
    name_fr = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=100, blank=True)
    icon_key = models.SlugField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name_ar", "name_en", "name_fr", "name", "id"]
        indexes = [models.Index(fields=["is_active", "category"])]
        verbose_name = _("Amenity")
        verbose_name_plural = _("Amenities")

    def __str__(self) -> str:
        return (
            self.name_ar
            or self.name_en
            or self.name_fr
            or self.name
            or str(self.hostaway_amenity_id)
        )

    @builtin_property
    def display_name(self) -> str:
        language = (translation.get_language() or "ar").split("-")[0]
        values = {
            "ar": (self.name_ar, self.name_en, self.name, self.name_fr),
            "en": (self.name_en, self.name, self.name_fr, self.name_ar),
            "fr": (self.name_fr, self.name_en, self.name, self.name_ar),
        }
        return next((value for value in values.get(language, values["ar"]) if value), "")


class PropertyAmenity(models.Model):
    class Source(models.TextChoices):
        HOSTAWAY = "hostaway", "Hostaway"
        LOCAL = "local", pgettext_lazy("PropertyAmenity", "Local")

    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="property_amenities",
    )
    amenity = models.ForeignKey(
        Amenity,
        on_delete=models.CASCADE,
        related_name="property_links",
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.LOCAL)
    is_visible = models.BooleanField(default=True)
    is_active_at_source = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["property", "amenity"],
                name="unique_property_amenity",
            ),
        ]
        indexes = [
            models.Index(fields=["property", "source", "is_visible", "is_active_at_source"]),
        ]
        verbose_name = _("Property amenity")
        verbose_name_plural = _("Property amenities")

    def __str__(self) -> str:
        return f"{self.property} — {self.amenity}"
