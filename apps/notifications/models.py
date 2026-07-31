"""Privacy-conscious operational notification, email, and audit records."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

internal_path_validator = RegexValidator(
    regex=r"^/(?!/)[^\s]*$",
    message="Action URL must be a relative internal path.",
)


class Notification(models.Model):
    class Type(models.TextChoices):
        CONTACT_MESSAGE_RECEIVED = "contact_message_received", "رسالة تواصل"
        BOOKING_INTENT_CREATED = "booking_intent_created", "طلب حجز"
        BOOKING_PRICE_CHANGED = "booking_price_changed", "تغير السعر"
        BOOKING_UNAVAILABLE = "booking_unavailable", "الوحدة غير متاحة"
        MODIFICATION_REQUESTED = "modification_requested", "طلب تعديل"
        CANCELLATION_REQUESTED = "cancellation_requested", "طلب إلغاء"
        HOSTAWAY_SYNC_FAILED = "hostaway_sync_failed", "فشل مزامنة"
        WEBHOOK_FAILED = "webhook_failed", "فشل Webhook"
        RESERVATION_UNKNOWN = "reservation_unknown", "حجز غير مؤكد"
        SYSTEM_WARNING = "system_warning", "تحذير نظام"

    class Audience(models.TextChoices):
        ADMIN = "admin", "الإدارة"
        USER = "user", "مستخدم"

    class Severity(models.TextChoices):
        INFO = "info", "معلومة"
        SUCCESS = "success", "نجاح"
        WARNING = "warning", "تحذير"
        ERROR = "error", "خطأ"

    class Status(models.TextChoices):
        ACTIVE = "active", "نشط"
        ARCHIVED = "archived", "مؤرشف"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification_type = models.CharField(max_length=50, choices=Type.choices)
    audience_type = models.CharField(max_length=20, choices=Audience.choices)
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="operational_notifications",
    )
    title_ar = models.CharField(max_length=200)
    title_en = models.CharField(max_length=200)
    message_ar = models.CharField(max_length=500)
    message_en = models.CharField(max_length=500)
    action_url = models.CharField(
        max_length=500,
        blank=True,
        validators=[internal_path_validator],
    )
    severity = models.CharField(
        max_length=20,
        choices=Severity.choices,
        default=Severity.INFO,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    related_object_type = models.CharField(max_length=80, blank=True)
    related_object_reference = models.CharField(max_length=100, blank=True)
    idempotency_key = models.CharField(max_length=100, unique=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "is_read", "-created_at"]),
            models.Index(fields=["notification_type", "-created_at"]),
            models.Index(fields=["recipient_user", "is_read", "-created_at"]),
            models.Index(fields=["expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(audience_type="admin") | Q(recipient_user__isnull=False),
                name="user_notification_requires_recipient",
            )
        ]
        permissions = [("manage_notification_center", "Can manage notification center")]
        verbose_name = "إشعار"
        verbose_name_plural = "الإشعارات"

    def __str__(self) -> str:
        return self.title_ar or self.title_en


class EmailDelivery(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "في الطابور"
        SENDING = "sending", "قيد الإرسال"
        SENT = "sent", "مرسل"
        FAILED = "failed", "فشل"
        CANCELLED = "cancelled", "ملغى"
        SKIPPED = "skipped", "متجاوز"
        DISABLED = "disabled", "معطل"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message_type = models.CharField(max_length=80)
    recipient_hash = models.CharField(max_length=64, editable=False)
    recipient_masked = models.CharField(max_length=254, editable=False)
    recipient_source = models.CharField(max_length=40, editable=False)
    recipient_reference = models.CharField(max_length=100, editable=False)
    language = models.CharField(
        max_length=10,
        choices=(("ar", "العربية"), ("en", "English"), ("fr", "Français")),
    )
    subject = models.CharField(max_length=250)
    template_name = models.CharField(max_length=160)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    provider = models.CharField(max_length=80)
    provider_message_id = models.CharField(max_length=255, null=True, blank=True)
    idempotency_key = models.CharField(max_length=100, unique=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    last_error_code = models.CharField(max_length=100, blank=True)
    queued_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "queued_at"]),
            models.Index(fields=["message_type", "-created_at"]),
            models.Index(fields=["recipient_hash", "-created_at"]),
        ]
        verbose_name = "تسليم بريد"
        verbose_name_plural = "تسليمات البريد"

    def __str__(self) -> str:
        return f"{self.message_type} — {self.status}"


class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100)
    object_reference = models.CharField(max_length=100)
    summary = models.CharField(max_length=300)
    metadata = models.JSONField(default=dict, blank=True)
    ip_hash = models.CharField(max_length=64, null=True, blank=True, editable=False)
    user_agent_family = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action", "-created_at"]),
            models.Index(fields=["object_type", "object_reference"]),
            models.Index(fields=["actor_user", "-created_at"]),
            models.Index(fields=["created_at"]),
        ]
        verbose_name = "سجل تدقيق"
        verbose_name_plural = "سجلات التدقيق"

    def __str__(self) -> str:
        return f"{self.action} — {self.object_type}"

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Audit logs are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise ValidationError("Audit logs are immutable.")
