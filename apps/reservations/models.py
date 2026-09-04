"""Local pre-booking records. Neither model is a confirmed reservation."""

import secrets
import uuid
from builtins import property as builtin_property
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


def booking_intent_reference() -> str:
    return secrets.token_urlsafe(18)


def reservation_reference() -> str:
    return secrets.token_urlsafe(18)


def modification_request_reference() -> str:
    return secrets.token_urlsafe(18)


def refund_obligation_reference() -> str:
    return secrets.token_urlsafe(18)


class BookingQuote(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        EXPIRED = "expired", pgettext_lazy("BookingQuote", "Expired")
        CONSUMED = "consumed", _("Used")
        INVALIDATED = "invalidated", pgettext_lazy("BookingQuote", "Cancelled")
        PRICE_CHANGED = "price_changed", _("Price changed")
        UNAVAILABLE = "unavailable", _("Unavailable")

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
        verbose_name = _("Booking quote")
        verbose_name_plural = _("Booking quotes")

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
        DRAFT = "draft", _("Draft")
        PENDING_REVALIDATION = "pending_revalidation", _("Awaiting re-verification")
        AWAITING_PAYMENT = "awaiting_payment", _("Ready for payment")
        PAYMENT_VERIFIED = "payment_verified", _("Payment verified")
        PRICE_CHANGED = "price_changed", _("Price changed")
        UNAVAILABLE = "unavailable", _("Unavailable")
        EXPIRED = "expired", pgettext_lazy("BookingIntent", "Expired")
        CANCELLED = "cancelled", pgettext_lazy("BookingIntent", "Cancelled")
        COMPLETED = "completed", _("Completed")

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
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="booking_intents",
        null=True,
        blank=True,
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
    billing_street1 = models.CharField(max_length=100, default="")
    billing_city = models.CharField(max_length=80, default="")
    billing_state = models.CharField(max_length=50, default="")
    billing_country = models.CharField(max_length=2, default="SA")
    billing_postcode = models.CharField(max_length=16, default="")
    language = models.CharField(
        max_length=5,
        choices=(("ar", "العربية"), ("en", "English"), ("fr", "Français")),
        default="ar",
    )
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
        verbose_name = _("Booking intent")
        verbose_name_plural = _("Booking intents")

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
        from apps.payments.countries import normalize_country_code

        try:
            self.billing_country = normalize_country_code(self.billing_country)
        except ValueError:
            errors["billing_country"] = "Billing country must be an ISO alpha-2 code."
        for field_name in (
            "billing_street1",
            "billing_city",
            "billing_state",
            "billing_postcode",
        ):
            if not getattr(self, field_name).strip():
                errors[field_name] = "This billing field is required."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def default_expiry(cls) -> datetime:
        return timezone.now() + timedelta(seconds=settings.BOOKING_INTENT_TTL_SECONDS)


class Reservation(models.Model):
    """Sanitized local reservation state; live confirmation requires a Hostaway ID."""

    class SourceType(models.TextChoices):
        DIRECT_WEBSITE = "direct_website", _("Live site")
        HOSTAWAY_MANUAL = "hostaway_manual", _("Hostaway manual")
        EXTERNAL_CHANNEL = "external_channel", _("External channel")
        UNKNOWN = "unknown", pgettext_lazy("Reservation", "Unknown")

    class Status(models.TextChoices):
        AWAITING_PAYMENT = "awaiting_payment", _("Awaiting payment")
        READY_FOR_HOSTAWAY = "ready_for_hostaway", _("Ready for Hostaway")
        CREATE_PENDING = "create_pending", _("Creation pending")
        CREATING = "creating", _("Creating")
        CONFIRMED = "confirmed", _("Confirmed")
        CREATE_FAILED = "create_failed", _("Creation failed")
        CREATE_UNKNOWN = "create_unknown", _("Creation result unconfirmed")
        SYNC_PENDING = "sync_pending", _("Sync pending")
        MODIFIED = "modified", _("Modified")
        CANCELLED = "cancelled", pgettext_lazy("Reservation", "Cancelled")
        # Hostaway also reports stays that never became a booking. Keeping them
        # apart from UNKNOWN leaves that value meaning "a status we do not know".
        PENDING = "pending", pgettext_lazy("Reservation", "Pending confirmation")
        INQUIRY = "inquiry", pgettext_lazy("Reservation", "Inquiry")
        DECLINED = "declined", pgettext_lazy("Reservation", "Declined")
        EXPIRED = "expired", pgettext_lazy("Reservation", "Expired")
        UNKNOWN = "unknown", pgettext_lazy("Reservation", "Unknown")

    # Grouped once so every consumer agrees on what "still counts as a stay" means.
    ACTIVE_STATUSES = frozenset({Status.CONFIRMED, Status.MODIFIED})
    CLOSED_STATUSES = frozenset({Status.CANCELLED, Status.DECLINED, Status.EXPIRED})

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
    is_test = models.BooleanField(default=False, editable=False)
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
                | Q(hostaway_reservation_id__isnull=False)
                | Q(is_test=True),
                name="reservation_confirmed_requires_hostaway_id",
            ),
            models.CheckConstraint(
                condition=Q(confirmed_at__isnull=True) | Q(cancelled_at__isnull=True),
                name="reservation_not_confirmed_and_cancelled",
            ),
        ]
        verbose_name = _("Local reservation")
        verbose_name_plural = _("Local reservations")

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
        if (
            self.normalized_status == self.Status.CONFIRMED
            and self.hostaway_reservation_id is None
            and not self.is_test
        ):
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
        CREATE_RESERVATION = "create_reservation", _("Create booking")
        RETRIEVE_RESERVATION = "retrieve_reservation", _("Fetch booking")
        RECONCILE_CREATION = "reconcile_creation", _("Creation reconciliation")

    class Status(models.TextChoices):
        PREPARED = "prepared", pgettext_lazy("HostawayReservationOperation", "Prepared")
        IN_PROGRESS = "in_progress", _("In progress")
        SUCCEEDED = "succeeded", _("Successful")
        FAILED = "failed", pgettext_lazy("HostawayReservationOperation", "Failed")
        UNKNOWN = "unknown", _("Unconfirmed")
        BLOCKED = "blocked", pgettext_lazy("HostawayReservationOperation", "Blocked")

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
        verbose_name = _("Hostaway booking operation")
        verbose_name_plural = _("Hostaway booking operations")

    def __str__(self) -> str:
        return f"{self.operation_type} — {self.status}"


class BookingModificationRequest(models.Model):
    """A session-owned local request; it never implies a Hostaway change."""

    class RequestType(models.TextChoices):
        EXTEND_STAY = "extend_stay", _("Extend stay")
        CHANGE_DATES = "change_dates", _("Change dates")
        CHANGE_GUESTS = "change_guests", _("Change guest count")
        CANCEL_RESERVATION = "cancel_reservation", _("Cancellation request")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PENDING_REVALIDATION = "pending_revalidation", _("Awaiting re-verification")
        AWAITING_CUSTOMER_APPROVAL = (
            "awaiting_customer_approval",
            _("Awaiting customer approval"),
        )
        AWAITING_PAYMENT = "awaiting_payment", _("Awaiting payment")
        PENDING_ADMIN_APPROVAL = "pending_admin_approval", _("Awaiting administration")
        READY_FOR_HOSTAWAY = "ready_for_hostaway", _("Ready for Hostaway")
        PROCESSING = "processing", _("In progress")
        COMPLETED = "completed", _("Completed")
        REJECTED = "rejected", pgettext_lazy("BookingModificationRequest", "Rejected")
        EXPIRED = "expired", pgettext_lazy("BookingModificationRequest", "Expired")
        PRICE_CHANGED = "price_changed", _("Price changed")
        UNAVAILABLE = "unavailable", _("Unavailable")
        FAILED = "failed", _("Failure")
        UNKNOWN = "unknown", _("Result unconfirmed")

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
        verbose_name = _("Booking modification request")
        verbose_name_plural = _("Booking modification requests")

    def __str__(self) -> str:
        return self.public_reference

    @builtin_property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    @builtin_property
    def refund_amount(self) -> Decimal:
        """The decrease as money owed, so no template has to negate a number."""
        if self.price_difference >= 0:
            return Decimal("0")
        return -self.price_difference

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
        UPDATE_DATES = "update_dates", _("Update dates")
        EXTEND_STAY = "extend_stay", _("Extend stay")
        UPDATE_GUESTS = "update_guests", _("Update guests")
        CANCEL_RESERVATION = "cancel_reservation", _("Cancel booking")
        RECONCILE_MODIFICATION = "reconcile_modification", _("Modification reconciliation")

    class Status(models.TextChoices):
        PREPARED = "prepared", pgettext_lazy("HostawayModificationOperation", "Prepared")
        IN_PROGRESS = "in_progress", _("In progress")
        SUCCEEDED = "succeeded", _("Successful")
        FAILED = "failed", pgettext_lazy("HostawayModificationOperation", "Failed")
        UNKNOWN = "unknown", _("Unconfirmed")
        BLOCKED = "blocked", pgettext_lazy("HostawayModificationOperation", "Blocked")

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
        verbose_name = _("Hostaway modification operation")
        verbose_name_plural = _("Hostaway modification operations")

    def __str__(self) -> str:
        return f"{self.operation_type} — {self.status}"


class CancellationPolicyTier(models.Model):
    """What the host returns to a guest who cancels, by how early they cancel.

    Hostaway names the policy ("flexible") but publishes no percentages, so the
    numbers live here and are owned by the administration. A policy with no tier
    refunds nothing automatically: an unconfigured table must never be read as
    "refund everything".
    """

    policy_code = models.CharField(
        max_length=60,
        help_text=_("Hostaway policy name, exactly as synced onto the property."),
    )
    min_hours_before_check_in = models.PositiveIntegerField(
        help_text=_("Applies when the cancellation happens at least this long before arrival."),
    )
    refund_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=_("Percentage of the stay total returned to the guest."),
    )
    refunds_cleaning_fee = models.BooleanField(
        default=True,
        help_text=_("Uncheck to keep the cleaning fee when this tier applies."),
    )
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Widest window first: the first tier a cancellation reaches is the one.
        ordering = ["policy_code", "-min_hours_before_check_in"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy_code", "min_hours_before_check_in"],
                name="one_tier_per_policy_window",
            ),
        ]
        verbose_name = _("Cancellation policy tier")
        verbose_name_plural = _("Cancellation policy tiers")

    def __str__(self) -> str:
        return f"{self.policy_code} ≥ {self.min_hours_before_check_in}h → {self.refund_percentage}%"


class RefundObligation(models.Model):
    """Money the platform owes a guest after an automatic change or cancellation.

    The transfer itself happens in the bank, outside this system. This record is
    what makes the debt visible, deducts it from reported income until it is
    settled, and keeps the audit trail of who settled it.
    """

    class Reason(models.TextChoices):
        MODIFICATION_DECREASE = "modification_decrease", _("Price decreased after a change")
        CANCELLATION = "cancellation", _("Cancellation refund")

    class Status(models.TextChoices):
        DUE = "due", _("Due to the guest")
        TRANSFERRED = "transferred", _("Transferred")
        CANCELLED = "cancelled", pgettext_lazy("RefundObligation", "Cancelled")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_reference = models.CharField(
        max_length=32,
        unique=True,
        default=refund_obligation_reference,
        editable=False,
    )
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.PROTECT,
        related_name="refund_obligations",
    )
    modification_request = models.ForeignKey(
        "BookingModificationRequest",
        on_delete=models.PROTECT,
        related_name="refund_obligations",
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=30, choices=Reason.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DUE)
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0"))],
    )
    currency = models.CharField(max_length=3)
    # How the amount was reached, so a later dispute can be answered without
    # recomputing against a policy table that may have changed since.
    calculation = models.JSONField(default=dict, blank=True, editable=False)
    transfer_reference = models.CharField(max_length=100, blank=True)
    transferred_at = models.DateTimeField(null=True, blank=True)
    transferred_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="settled_refund_obligations",
        null=True,
        blank=True,
    )
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["reservation", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="refund_obligation_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=~Q(status="transferred") | Q(transferred_at__isnull=False),
                name="transferred_refund_records_when",
            ),
        ]
        verbose_name = _("Refund owed to a guest")
        verbose_name_plural = _("Refunds owed to guests")

    def __str__(self) -> str:
        return f"{self.public_reference} — {self.amount} {self.currency}"

    @builtin_property
    def guest_email(self) -> str:
        intent = self.reservation.booking_intent
        return intent.guest_email if intent else ""

    @builtin_property
    def guest_phone(self) -> str:
        intent = self.reservation.booking_intent
        return intent.guest_phone if intent else ""

    @builtin_property
    def guest_name(self) -> str:
        intent = self.reservation.booking_intent
        if intent is None:
            return ""
        return f"{intent.guest_first_name} {intent.guest_last_name}".strip()
