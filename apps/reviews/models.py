from builtins import property as builtin_property
from decimal import ROUND_HALF_UP, Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext

from apps.properties.models import Property


class ReviewQuerySet(models.QuerySet["Review"]):
    def public(self) -> "ReviewQuerySet":
        """Return reviews safe and eligible for public display."""
        return self.filter(
            review_type=Review.Type.GUEST_TO_HOST,
            status=Review.Status.PUBLISHED,
            is_visible=True,
            rating__isnull=False,
        ).exclude(
            public_review="",
            public_review_ar="",
            public_review_en="",
            public_review_fr="",
        )


class Review(models.Model):
    """A Hostaway-sourced review. Hostaway data is immutable in Django admin."""

    class Type(models.TextChoices):
        GUEST_TO_HOST = "guest-to-host", "ضيف إلى مضيف"
        HOST_TO_GUEST = "host-to-guest", "مضيف إلى ضيف"

    class Status(models.TextChoices):
        PUBLISHED = "published", "منشورة"
        AWAITING = "awaiting", "قيد الانتظار"
        DECLINED = "declined", "مرفوضة"
        OTHER = "other", "أخرى"

    hostaway_review_id = models.PositiveBigIntegerField(unique=True)
    property = models.ForeignKey(
        Property,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviews",
    )
    hostaway_listing_map_id = models.PositiveBigIntegerField(db_index=True)
    hostaway_reservation_id = models.CharField(max_length=100, blank=True)
    external_review_id = models.CharField(max_length=150, blank=True)
    channel_id = models.CharField(max_length=100, blank=True)
    review_type = models.CharField(max_length=30, choices=Type.choices)
    status = models.CharField(max_length=30, choices=Status.choices)
    guest_name = models.CharField(max_length=200, blank=True)
    rating = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("10"))],
    )
    public_review = models.TextField()
    public_review_ar = models.TextField(blank=True)
    public_review_en = models.TextField(blank=True)
    public_review_fr = models.TextField(blank=True)
    reviewee_response = models.TextField(blank=True)
    arrival_date = models.DateField(null=True, blank=True)
    departure_date = models.DateField(null=True, blank=True)
    is_visible = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ReviewQuerySet.as_manager()

    class Meta:
        ordering = ["-is_featured", "-departure_date", "-id"]
        indexes = [
            models.Index(fields=["property", "-departure_date"]),
            models.Index(fields=["status", "review_type"]),
            models.Index(fields=["rating"]),
            models.Index(fields=["is_visible", "is_featured"]),
            models.Index(fields=["-departure_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__gte=0, rating__lte=10),
                name="review_rating_between_0_and_10",
            ),
        ]
        verbose_name = "مراجعة"
        verbose_name_plural = "المراجعات"

    def __str__(self) -> str:
        return f"{self.guest_name or 'ضيف'} — {self.rating}"

    @builtin_property
    def display_guest_name(self) -> str:
        """Return a privacy-preserving first name."""
        first_name = self.guest_name.strip().split(maxsplit=1)[0] if self.guest_name else ""
        looks_sensitive = (
            "@" in first_name or sum(character.isdigit() for character in first_name) >= 4
        )
        return gettext("Guest") if looks_sensitive or not first_name else first_name

    @builtin_property
    def star_display(self) -> str:
        """Render the ten-point rating as five accessible visual stars."""
        filled = int(
            (self.rating / Decimal("2")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        filled = min(5, max(0, filled))
        return ("★" * filled) + ("☆" * (5 - filled))
