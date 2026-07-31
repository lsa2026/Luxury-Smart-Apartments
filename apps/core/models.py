"""Small bilingual content, privacy-safe inbox, and SEO infrastructure."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac


class SitePage(models.Model):
    slug = models.SlugField(max_length=80, unique=True)
    title_ar = models.CharField(max_length=200)
    title_en = models.CharField(max_length=200)
    body_ar = models.TextField(blank=True)
    body_en = models.TextField(blank=True)
    meta_description_ar = models.CharField(max_length=320, blank=True)
    meta_description_en = models.CharField(max_length=320, blank=True)
    is_published = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]
        verbose_name = "صفحة محتوى"
        verbose_name_plural = "صفحات المحتوى"

    def __str__(self) -> str:
        return self.title_ar or self.title_en


class FAQItem(models.Model):
    question_ar = models.CharField(max_length=300)
    question_en = models.CharField(max_length=300)
    answer_ar = models.TextField()
    answer_en = models.TextField()
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [models.Index(fields=["is_active", "sort_order"])]
        verbose_name = "سؤال شائع"
        verbose_name_plural = "الأسئلة الشائعة"

    def __str__(self) -> str:
        return self.question_ar or self.question_en


class SiteSetting(models.Model):
    site_name = models.CharField(max_length=120, default="Luxury Smart Apartments")
    brand_name_ar = models.CharField(max_length=120, blank=True)
    brand_name_en = models.CharField(max_length=120, blank=True)
    tagline_ar = models.CharField(max_length=240, blank=True)
    tagline_en = models.CharField(max_length=240, blank=True)
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
    public_address_ar = models.CharField(max_length=240, blank=True)
    public_address_en = models.CharField(max_length=240, blank=True)
    footer_text_ar = models.CharField(max_length=320, blank=True)
    footer_text_en = models.CharField(max_length=320, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "إعداد الموقع"
        verbose_name_plural = "إعدادات الموقع"

    def __str__(self) -> str:
        return self.site_name


class ContactMessage(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "جديدة"
        IN_PROGRESS = "in_progress", "قيد المتابعة"
        CLOSED = "closed", "مغلقة"
        SPAM = "spam", "مزعجة"

    name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    subject = models.CharField(max_length=200)
    message = models.TextField(validators=[MaxLengthValidator(2000)])
    language = models.CharField(max_length=10, choices=(("ar", "العربية"), ("en", "English")))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["-created_at"]),
        ]
        verbose_name = "رسالة تواصل"
        verbose_name_plural = "رسائل التواصل"

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
        verbose_name = "تحويل رابط قديم"
        verbose_name_plural = "تحويلات الروابط القديمة"

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
        PREPARED = "prepared", "مجهز"
        EMITTED = "emitted", "أرسل"
        BLOCKED = "blocked", "محظور"

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
        verbose_name = "إيصال حدث تسويقي"
        verbose_name_plural = "إيصالات الأحداث التسويقية"
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
