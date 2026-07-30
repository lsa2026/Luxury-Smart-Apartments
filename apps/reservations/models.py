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
