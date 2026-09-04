"""Small bilingual content, privacy-safe inbox, and SEO infrastructure."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from .branding import BRAND_NAME


class SitePage(models.Model):
    slug = models.SlugField(max_length=80, unique=True)
    title_ar = models.CharField(max_length=200)
    title_en = models.CharField(max_length=200)
    title_fr = models.CharField(max_length=200, blank=True)
    body_ar = models.TextField(blank=True)
    body_en = models.TextField(blank=True)
    body_fr = models.TextField(blank=True)
    meta_description_ar = models.CharField(max_length=320, blank=True)
    meta_description_en = models.CharField(max_length=320, blank=True)
    meta_description_fr = models.CharField(max_length=320, blank=True)
    is_published = models.BooleanField(default=True)
    last_reviewed_at = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]
        verbose_name = _("Content page")
        verbose_name_plural = _("Content pages")

    def __str__(self) -> str:
        return self.title_ar or self.title_en or self.title_fr


class FAQItem(models.Model):
    class Category(models.TextChoices):
        BOOKING = "booking", _("Booking and payment")
        STAY = "stay", _("During your stay")
        POLICY = "policy", _("Policies and cancellation")
        PROPERTY = "property", _("About the property")

    # A question tied to one property appears on that property's page as well as
    # in the general list; a question with no property is site-wide.
    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.CASCADE,
        related_name="faq_items",
        null=True,
        blank=True,
        verbose_name=_("Specific property"),
        help_text=_("Leave empty for a question that applies to every stay."),
    )
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.BOOKING,
        verbose_name=_("Category"),
    )
    question_ar = models.CharField(max_length=300)
    question_en = models.CharField(max_length=300)
    question_fr = models.CharField(max_length=300, blank=True)
    answer_ar = models.TextField()
    answer_en = models.TextField()
    answer_fr = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "sort_order", "id"]
        indexes = [
            models.Index(fields=["is_active", "sort_order"]),
            models.Index(fields=["property", "is_active"]),
            models.Index(fields=["category", "sort_order"]),
        ]
        verbose_name = _("FAQ item")
        verbose_name_plural = _("FAQ items")

    def __str__(self) -> str:
        return self.question_ar or self.question_en or self.question_fr


class SiteSetting(models.Model):
    site_name = models.CharField(max_length=120, default=BRAND_NAME)
    brand_name_ar = models.CharField(max_length=120, blank=True)
    brand_name_en = models.CharField(max_length=120, blank=True)
    brand_name_fr = models.CharField(max_length=120, blank=True)
    tagline_ar = models.CharField(max_length=240, blank=True)
    tagline_en = models.CharField(max_length=240, blank=True)
    tagline_fr = models.CharField(max_length=240, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)
    whatsapp_display_number = models.CharField(max_length=30, blank=True)
    whatsapp_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    facebook_url = models.URLField(blank=True)
    x_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    office_hours_ar = models.CharField(max_length=200, blank=True)
    office_hours_en = models.CharField(max_length=200, blank=True)
    office_hours_fr = models.CharField(max_length=200, blank=True)
    public_address_ar = models.CharField(max_length=240, blank=True)
    public_address_en = models.CharField(max_length=240, blank=True)
    public_address_fr = models.CharField(max_length=240, blank=True)
    footer_text_ar = models.CharField(max_length=320, blank=True)
    footer_text_en = models.CharField(max_length=320, blank=True)
    footer_text_fr = models.CharField(max_length=320, blank=True)
    # Shown only when Hostaway reports no hour for a listing. Null keeps the
    # display honest: an unknown time says so rather than inventing 15:00.
    default_check_in_hour = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(24)],
        verbose_name=_("Default check-in hour"),
        help_text=_("Used when Hostaway reports no check-in time for a property."),
    )
    default_check_out_hour = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(24)],
        verbose_name=_("Default check-out hour"),
        help_text=_("Used when Hostaway reports no check-out time for a property."),
    )
    default_house_rules_ar = models.TextField(blank=True, verbose_name=_("Default house rules"))
    default_house_rules_en = models.TextField(blank=True, verbose_name=_("Default house rules"))
    default_house_rules_fr = models.TextField(blank=True, verbose_name=_("Default house rules"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Site setting")
        verbose_name_plural = _("Site settings")

    def __str__(self) -> str:
        return self.site_name

    def save(self, *args: object, **kwargs: object) -> None:
        self.site_name = BRAND_NAME
        self.brand_name_ar = BRAND_NAME
        self.brand_name_en = BRAND_NAME
        self.brand_name_fr = BRAND_NAME
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "site_name",
                "brand_name_ar",
                "brand_name_en",
                "brand_name_fr",
            }
        super().save(*args, **kwargs)


class ContactMessage(models.Model):
    class Status(models.TextChoices):
        NEW = "new", _("New")
        IN_PROGRESS = "in_progress", _("Being followed up")
        CLOSED = "closed", _("Closed")
        SPAM = "spam", _("Spam")

    name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    subject = models.CharField(max_length=200)
    message = models.TextField(validators=[MaxLengthValidator(2000)])
    language = models.CharField(
        max_length=10,
        choices=(("ar", "العربية"), ("en", "English"), ("fr", "Français")),
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["-created_at"]),
        ]
        verbose_name = _("Contact message")
        verbose_name_plural = _("Contact messages")

    def __str__(self) -> str:
        return f"{self.subject} — {self.created_at:%Y-%m-%d}"


class LegacyRedirect(models.Model):
    class RedirectType(models.IntegerChoices):
        PERMANENT = 301, "301 Permanent"
        TEMPORARY = 302, "302 Temporary"
        GONE = 410, "410 Gone"

    PROTECTED_PREFIXES = (
        "/admin/",
        "/integrations/",
        "/health/",
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_path = models.CharField(max_length=500, unique=True)
    destination_path = models.CharField(max_length=500, blank=True)
    redirect_type = models.PositiveSmallIntegerField(
        choices=RedirectType.choices,
        default=RedirectType.PERMANENT,
    )
    is_active = models.BooleanField(default=True)
    hit_count = models.PositiveBigIntegerField(default=0, editable=False)
    last_hit_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_path"]
        indexes = [
            models.Index(fields=["is_active", "source_path"]),
            models.Index(fields=["redirect_type", "is_active"]),
        ]
        verbose_name = _("Legacy redirect")
        verbose_name_plural = _("Legacy redirects")

    def __str__(self) -> str:
        return f"{self.source_path} → {self.destination_path or '410'}"

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        for field_name in ("source_path", "destination_path"):
            value = getattr(self, field_name)
            if value and (
                not value.startswith("/")
                or value.startswith("//")
                or "://" in value
                or "?" in value
                or "#" in value
            ):
                errors[field_name] = (
                    "Only an exact internal path without query or fragment is allowed."
                )
        if any(self.source_path.startswith(prefix) for prefix in self.PROTECTED_PREFIXES):
            errors["source_path"] = (
                "Protected administrative and integration paths cannot redirect."
            )
        if self.destination_path and any(
            self.destination_path.startswith(prefix) for prefix in self.PROTECTED_PREFIXES
        ):
            errors["destination_path"] = "A redirect cannot target a protected path."
        if self.redirect_type == self.RedirectType.GONE:
            if self.destination_path:
                errors["destination_path"] = "A 410 response cannot have a destination."
        elif not self.destination_path:
            errors["destination_path"] = "A destination is required for redirects."
        if self.destination_path and self.source_path == self.destination_path:
            errors["destination_path"] = "Source and destination cannot be identical."
        if self.destination_path:
            visited = {self.source_path}
            next_path = self.destination_path
            while next_path:
                if next_path in visited:
                    errors["destination_path"] = "This redirect would create a loop."
                    break
                visited.add(next_path)
                next_path = (
                    type(self)
                    .objects.filter(source_path=next_path, is_active=True)
                    .exclude(pk=self.pk)
                    .values_list("destination_path", flat=True)
                    .first()
                )
        if errors:
            raise ValidationError(errors)


class MarketingEventReceipt(models.Model):
    class Status(models.TextChoices):
        PREPARED = "prepared", pgettext_lazy("MarketingEventReceipt", "Prepared")
        EMITTED = "emitted", pgettext_lazy("MarketingEventReceipt", "Sent")
        BLOCKED = "blocked", pgettext_lazy("MarketingEventReceipt", "Blocked")

    FINANCIAL_EVENTS = (("purchase", "Purchase"), ("refund", "Refund"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_name = models.CharField(max_length=20, choices=FINANCIAL_EVENTS)
    object_type = models.CharField(max_length=80)
    object_reference_hash = models.CharField(max_length=64, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PREPARED)
    emitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event_name", "object_reference_hash"],
                name="unique_financial_marketing_event",
            ),
            models.CheckConstraint(
                condition=Q(event_name__in=("purchase", "refund")),
                name="marketing_receipt_financial_events_only",
            ),
        ]
        indexes = [models.Index(fields=["status", "-created_at"])]
        verbose_name = _("Marketing event receipt")
        verbose_name_plural = _("Marketing event receipts")
        permissions = [
            ("view_marketing_diagnostics", "Can view marketing diagnostics"),
            ("view_seo_dashboard", "Can view SEO dashboard"),
        ]

    def __str__(self) -> str:
        return f"{self.event_name} — {self.status}"

    @classmethod
    def reference_hmac(cls, value: str) -> str:
        return salted_hmac(
            "marketing-financial-event.v1",
            value,
            secret=settings.SECRET_KEY,
        ).hexdigest()

    def mark_emitted(self) -> None:
        self.status = self.Status.EMITTED
        self.emitted_at = timezone.now()
