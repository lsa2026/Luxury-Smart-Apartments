"""Local pre-booking records. Neither model is a confirmed reservation."""

import secrets
import uuid
from builtins import property as builtin_property
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


def booking_intent_reference() -> str:
    return secrets.token_urlsafe(18)


def reservation_reference() -> str:
    return secrets.token_urlsafe(18)


def modification_request_reference() -> str:
    return secrets.token_urlsafe(18)


class BookingQuote(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "نشط"
        EXPIRED = "expired", "منتهي"
        CONSUMED = "consumed", "مستخدم"
        INVALIDATED = "invalidated", "ملغى"
        PRICE_CHANGED = "price_changed", "تغير السعر"
        UNAVAILABLE = "unavailable", "غير متاح"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.PROTECT,
        related_name="booking_quotes",
    )
    hostaway_listing_id = models.PositiveBigIntegerField(editable=False)
    check_in = models.DateField()
    check_out = models.DateField()
    nights = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    guests = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3)
    total_price = models.DecimalField(max_digits=14, decimal_places=4)
    components = models.JSONField(default=list)
    price_version = models.PositiveSmallIntegerField(default=2, editable=False)
    signature = models.CharField(max_length=64, editable=False)
    session_key_hash = models.CharField(max_length=64, editable=False)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    expires_at = models.DateTimeField()
    calculated_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["property", "status"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_price__gte=0),
                name="booking_quote_total_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(check_out__gt=F("check_in")),
                name="booking_quote_checkout_after_checkin",
            ),
        ]
        verbose_name = "عرض سعر"
        verbose_name_plural = "عروض الأسعار"

    def __str__(self) -> str:
        return f"{self.property} — {self.check_in} / {self.check_out}"

    @builtin_property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.property_id:
            if self.hostaway_listing_id != self.property.hostaway_listing_id:
                errors["hostaway_listing_id"] = "Listing ID must come from the property."
            if self.property.person_capacity and self.guests > self.property.person_capacity:
                errors["guests"] = "Guest count exceeds property capacity."
        if self.check_in and self.check_out:
            expected_nights = (self.check_out - self.check_in).days
            if expected_nights < 1:
                errors["check_out"] = "Check-out must be after check-in."
            if self.nights != expected_nights:
                errors["nights"] = "Nights must match the date interval."
        if self.currency and (
            len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isalpha()
        ):
            errors["currency"] = "Currency must be a three-letter code."
        if self.price_version != 2:
            errors["price_version"] = "Only priceDetails version 2 is supported."
        if not isinstance(self.components, list):
            errors["components"] = "Components must be a list."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def default_expiry(cls) -> datetime:
        return timezone.now() + timedelta(seconds=settings.BOOKING_QUOTE_TTL_SECONDS)


class BookingIntent(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        PENDING_REVALIDATION = "pending_revalidation", "بانتظار إعادة التحقق"
        AWAITING_PAYMENT = "awaiting_payment", "جاهز للدفع"
        PRICE_CHANGED = "price_changed", "تغير السعر"
        UNAVAILABLE = "unavailable", "غير متاح"
        EXPIRED = "expired", "منتهي"
        CANCELLED = "cancelled", "ملغى"
        COMPLETED = "completed", "مكتمل"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_reference = models.CharField(
        max_length=32,
        unique=True,
        default=booking_intent_reference,
        editable=False,
    )
    quote = models.OneToOneField(
        BookingQuote,
        on_delete=models.PROTECT,
        related_name="booking_intent",
    )
    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.PROTECT,
        related_name="booking_intents",
    )
    check_in = models.DateField()
    check_out = models.DateField()
    nights = models.PositiveSmallIntegerField()
    guests = models.PositiveSmallIntegerField()
    currency = models.CharField(max_length=3)
    total_price = models.DecimalField(max_digits=14, decimal_places=4)
    guest_first_name = models.CharField(max_length=100)
    guest_last_name = models.CharField(max_length=100)
    guest_email = models.EmailField(max_length=254)
    guest_phone = models.CharField(max_length=20)
    guest_country_code = models.CharField(max_length=2)
    special_requests = models.TextField(max_length=1000, blank=True)
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    idempotency_key = models.CharField(max_length=64, unique=True, editable=False)
    session_key_hash = models.CharField(max_length=64, editable=False)
    terms_accepted_at = models.DateTimeField()
    privacy_accepted_at = models.DateTimeField()
    marketing_consent = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["property", "status"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_price__gte=0),
                name="booking_intent_total_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(check_out__gt=F("check_in")),
                name="booking_intent_checkout_after_checkin",
            ),
        ]
        permissions = [
            ("view_bookingintent_pii", "Can view booking intent guest data"),
            ("cancel_bookingintent", "Can cancel booking intents"),
        ]
        verbose_name = "طلب حجز مبدئي"
        verbose_name_plural = "طلبات الحجز المبدئية"

    def __str__(self) -> str:
        return self.public_reference

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.property_id and self.property.person_capacity:
            if self.guests > self.property.person_capacity:
                errors["guests"] = "Guest count exceeds property capacity."
        if self.check_in and self.check_out:
            expected_nights = (self.check_out - self.check_in).days
            if expected_nights < 1:
                errors["check_out"] = "Check-out must be after check-in."
            if self.nights != expected_nights:
                errors["nights"] = "Nights must match the date interval."
        if len(self.special_requests) > 1000:
            errors["special_requests"] = "Special requests exceed 1000 characters."
        if self.currency and (
            len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isalpha()
        ):
            errors["currency"] = "Currency must be a three-letter code."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def default_expiry(cls) -> datetime:
        return timezone.now() + timedelta(seconds=settings.BOOKING_INTENT_TTL_SECONDS)


class Reservation(models.Model):
    """Sanitized local reservation state; confirmation requires a Hostaway ID."""

    class SourceType(models.TextChoices):
        DIRECT_WEBSITE = "direct_website", "الموقع المباشر"
        HOSTAWAY_MANUAL = "hostaway_manual", "Hostaway يدوي"
        EXTERNAL_CHANNEL = "external_channel", "قناة خارجية"
        UNKNOWN = "unknown", "غير معروف"

    class Status(models.TextChoices):
        AWAITING_PAYMENT = "awaiting_payment", "بانتظار الدفع"
        READY_FOR_HOSTAWAY = "ready_for_hostaway", "جاهز لـHostaway"
        CREATE_PENDING = "create_pending", "إنشاء معلق"
        CREATING = "creating", "جارٍ الإنشاء"
        CONFIRMED = "confirmed", "مؤكد"
        CREATE_FAILED = "create_failed", "فشل الإنشاء"
        CREATE_UNKNOWN = "create_unknown", "نتيجة الإنشاء غير مؤكدة"
        SYNC_PENDING = "sync_pending", "مزامنة معلقة"
        MODIFIED = "modified", "معدل"
        CANCELLED = "cancelled", "ملغى"
        UNKNOWN = "unknown", "غير معروف"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_reference = models.CharField(
        max_length=32,
        unique=True,
        default=reservation_reference,
        editable=False,
    )
    booking_intent = models.OneToOneField(
        BookingIntent,
        on_delete=models.PROTECT,
        related_name="reservation",
        null=True,
        blank=True,
    )
    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.PROTECT,
        related_name="reservations",
        null=True,
        blank=True,
    )
    hostaway_reservation_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    hostaway_listing_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    hostaway_listing_map_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        editable=False,
    )
    channel_id = models.PositiveIntegerField(null=True, blank=True, editable=False)
    source_type = models.CharField(
        max_length=30,
        choices=SourceType.choices,
        default=SourceType.UNKNOWN,
    )
    normalized_status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.UNKNOWN,
    )
    hostaway_status = models.CharField(max_length=100, blank=True, editable=False)
    payment_status = models.CharField(max_length=100, blank=True, editable=False)
    check_in = models.DateField()
    check_out = models.DateField()
    nights = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    guests = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3)
    total_price = models.DecimalField(max_digits=14, decimal_places=4)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["normalized_status", "-created_at"]),
            models.Index(fields=["property", "check_in"]),
            models.Index(fields=["source_type", "-created_at"]),
            models.Index(fields=["last_synced_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(check_out__gt=F("check_in")),
                name="reservation_checkout_after_checkin",
            ),
            models.CheckConstraint(
                condition=Q(total_price__gte=0),
                name="reservation_total_nonnegative",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_status="confirmed")
                | Q(hostaway_reservation_id__isnull=False),
                name="reservation_confirmed_requires_hostaway_id",
            ),
            models.CheckConstraint(
                condition=Q(confirmed_at__isnull=True) | Q(cancelled_at__isnull=True),
                name="reservation_not_confirmed_and_cancelled",
            ),
        ]
        verbose_name = "حجز محلي"
        verbose_name_plural = "الحجوزات المحلية"

    def __str__(self) -> str:
        return self.public_reference

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.check_in and self.check_out:
            expected_nights = (self.check_out - self.check_in).days
            if expected_nights < 1:
                errors["check_out"] = "Check-out must be after check-in."
            if self.nights != expected_nights:
                errors["nights"] = "Nights must match the date interval."
        if self.currency:
            self.currency = self.currency.upper()
            if (
                len(self.currency) != 3
                or not self.currency.isascii()
                or not self.currency.isalpha()
            ):
                errors["currency"] = "Currency must be an ISO three-letter code."
        if self.normalized_status == self.Status.CONFIRMED and self.hostaway_reservation_id is None:
            errors["hostaway_reservation_id"] = "Confirmed reservations require a Hostaway ID."
        if self.confirmed_at and self.cancelled_at:
            errors["cancelled_at"] = "A reservation cannot be confirmed and cancelled together."
        if self.booking_intent_id and self.property_id:
            if self.booking_intent.property_id != self.property_id:
                errors["property"] = "Property must match the booking intent."
        if errors:
            raise ValidationError(errors)


class HostawayReservationOperation(models.Model):
    """Idempotency and uncertainty ledger for outbound Hostaway reservation calls."""

    class OperationType(models.TextChoices):
        CREATE_RESERVATION = "create_reservation", "إنشاء حجز"
        RETRIEVE_RESERVATION = "retrieve_reservation", "جلب حجز"
        RECONCILE_CREATION = "reconcile_creation", "مصالحـة إنشاء"

    class Status(models.TextChoices):
        PREPARED = "prepared", "مجهزة"
        IN_PROGRESS = "in_progress", "قيد التنفيذ"
        SUCCEEDED = "succeeded", "ناجحة"
        FAILED = "failed", "فشلت"
        UNKNOWN = "unknown", "غير مؤكدة"
        BLOCKED = "blocked", "محظورة"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.PROTECT,
        related_name="hostaway_operations",
    )
    operation_type = models.CharField(max_length=30, choices=OperationType.choices)
    idempotency_key = models.CharField(max_length=64, unique=True, editable=False)
    request_fingerprint = models.CharField(max_length=64, editable=False)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PREPARED,
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    hostaway_reservation_id = models.PositiveBigIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["reservation", "operation_type"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reservation"],
                condition=Q(operation_type="create_reservation"),
                name="one_create_operation_per_reservation",
            ),
        ]
        verbose_name = "عملية حجز Hostaway"
        verbose_name_plural = "عمليات حجوزات Hostaway"

    def __str__(self) -> str:
        return f"{self.operation_type} — {self.status}"


class BookingModificationRequest(models.Model):
    """A session-owned local request; it never implies a Hostaway change."""

    class RequestType(models.TextChoices):
        EXTEND_STAY = "extend_stay", "تمديد الإقامة"
        CHANGE_DATES = "change_dates", "تغيير التواريخ"
        CHANGE_GUESTS = "change_guests", "تغيير عدد الضيوف"
        CANCEL_RESERVATION = "cancel_reservation", "طلب إلغاء"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        PENDING_REVALIDATION = "pending_revalidation", "بانتظار إعادة التحقق"
        AWAITING_CUSTOMER_APPROVAL = (
            "awaiting_customer_approval",
            "بانتظار موافقة العميل",
        )
        AWAITING_PAYMENT = "awaiting_payment", "بانتظار الدفع"
        PENDING_ADMIN_APPROVAL = "pending_admin_approval", "بانتظار الإدارة"
        READY_FOR_HOSTAWAY = "ready_for_hostaway", "جاهز لـHostaway"
        PROCESSING = "processing", "قيد التنفيذ"
        COMPLETED = "completed", "مكتمل"
        REJECTED = "rejected", "مرفوض"
        EXPIRED = "expired", "منتهي"
        PRICE_CHANGED = "price_changed", "تغير السعر"
        UNAVAILABLE = "unavailable", "غير متاح"
        FAILED = "failed", "فشل"
        UNKNOWN = "unknown", "نتيجة غير مؤكدة"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_reference = models.CharField(
        max_length=32,
        unique=True,
        default=modification_request_reference,
        editable=False,
    )
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.PROTECT,
        related_name="modification_requests",
    )
    request_type = models.CharField(max_length=30, choices=RequestType.choices)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.DRAFT)
    old_check_in = models.DateField()
    old_check_out = models.DateField()
    new_check_in = models.DateField(null=True, blank=True)
    new_check_out = models.DateField(null=True, blank=True)
    old_guests = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    new_guests = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
        null=True,
        blank=True,
    )
    old_total = models.DecimalField(max_digits=14, decimal_places=4)
    new_total = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    price_difference = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    currency = models.CharField(max_length=3)
    reason = models.CharField(max_length=1000, blank=True)
    quote_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    idempotency_key = models.CharField(max_length=64, unique=True, editable=False)
    session_key_hash = models.CharField(max_length=64, editable=False)
    requested_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["reservation", "status"]),
            models.Index(fields=["request_type", "-requested_at"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(old_check_out__gt=F("old_check_in")),
                name="modification_old_checkout_after_checkin",
            ),
            models.CheckConstraint(
                condition=Q(new_check_in__isnull=True)
                | Q(new_check_out__isnull=True)
                | Q(new_check_out__gt=F("new_check_in")),
                name="modification_new_checkout_after_checkin",
            ),
            models.CheckConstraint(
                condition=Q(old_total__gte=0),
                name="modification_old_total_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(new_total__isnull=True) | Q(new_total__gte=0),
                name="modification_new_total_nonnegative",
            ),
        ]
        permissions = [
            ("approve_bookingmodificationrequest", "Can approve modification requests"),
            ("reject_bookingmodificationrequest", "Can reject modification requests"),
        ]
        verbose_name = "طلب تعديل حجز"
        verbose_name_plural = "طلبات تعديل الحجوزات"

    def __str__(self) -> str:
        return self.public_reference

    @builtin_property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.old_check_out <= self.old_check_in:
            errors["old_check_out"] = "Old check-out must be after check-in."
        if self.new_check_in and self.new_check_out and self.new_check_out <= self.new_check_in:
            errors["new_check_out"] = "New check-out must be after check-in."
        if bool(self.new_check_in) != bool(self.new_check_out):
            errors["new_check_out"] = "Both new dates are required together."
        if self.new_guests and self.reservation.property_id:
            capacity = self.reservation.property.person_capacity
            if capacity and self.new_guests > capacity:
                errors["new_guests"] = "Guest count exceeds property capacity."
        if self.currency:
            self.currency = self.currency.upper()
            if (
                len(self.currency) != 3
                or not self.currency.isascii()
                or not self.currency.isalpha()
            ):
                errors["currency"] = "Currency must be an ISO three-letter code."
        if len(self.reason) > 1000:
            errors["reason"] = "Reason exceeds 1000 characters."
        if not isinstance(self.quote_snapshot, dict):
            errors["quote_snapshot"] = "Quote snapshot must be an object."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def default_expiry(cls) -> datetime:
        return timezone.now() + timedelta(seconds=settings.BOOKING_MODIFICATION_REQUEST_TTL_SECONDS)


class HostawayModificationOperation(models.Model):
    """Idempotency ledger for future Hostaway modification writes."""

    class OperationType(models.TextChoices):
        UPDATE_DATES = "update_dates", "تحديث التواريخ"
        EXTEND_STAY = "extend_stay", "تمديد الإقامة"
        UPDATE_GUESTS = "update_guests", "تحديث الضيوف"
        CANCEL_RESERVATION = "cancel_reservation", "إلغاء الحجز"
        RECONCILE_MODIFICATION = "reconcile_modification", "مصالحـة التعديل"

    class Status(models.TextChoices):
        PREPARED = "prepared", "مجهزة"
        IN_PROGRESS = "in_progress", "قيد التنفيذ"
        SUCCEEDED = "succeeded", "ناجحة"
        FAILED = "failed", "فشلت"
        UNKNOWN = "unknown", "غير مؤكدة"
        BLOCKED = "blocked", "محظورة"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    modification_request = models.ForeignKey(
        BookingModificationRequest,
        on_delete=models.PROTECT,
        related_name="hostaway_operations",
    )
    operation_type = models.CharField(max_length=35, choices=OperationType.choices)
    idempotency_key = models.CharField(max_length=64, unique=True, editable=False)
    request_fingerprint = models.CharField(max_length=64, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PREPARED)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    hostaway_reservation_id = models.PositiveBigIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["modification_request", "operation_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["modification_request", "operation_type"],
                name="one_modification_operation_per_type",
            ),
        ]
        verbose_name = "عملية تعديل Hostaway"
        verbose_name_plural = "عمليات تعديل Hostaway"

    def __str__(self) -> str:
        return f"{self.operation_type} — {self.status}"
