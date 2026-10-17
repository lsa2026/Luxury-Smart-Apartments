"""Account state that does not belong on Django's own user row."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class VerifiedPhone(models.Model):
    """Separate credential, never inferred from an editable contact field."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    phone = models.CharField(max_length=32, unique=True)
    version = models.UUIDField(default=uuid.uuid4, editable=False)
    verified_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"VerifiedPhone {self.pk}"


class ApprovedPhoneChange(models.Model):
    """Admin-authorised destination; not a credential until WhatsApp proof succeeds."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    phone = models.CharField(max_length=32, unique=True)
    version = models.UUIDField(default=uuid.uuid4, editable=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approved_guest_phones"
    )
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"ApprovedPhoneChange {self.pk}"


class PhoneChallenge(models.Model):
    """One session/purpose-bound OTP. No message or plaintext code is retained."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    phone = models.CharField(max_length=32)
    purpose = models.CharField(max_length=8, choices=[("bind", "bind"), ("login", "login")])
    language = models.CharField(
        max_length=2,
        default="ar",
        choices=[("ar", "Arabic"), ("en", "English"), ("fr", "French")],
    )
    binding_version = models.UUIDField(null=True)
    session_digest = models.CharField(max_length=64)
    code_digest = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=12, default="pending")
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return str(self.pk)


class PhoneAuthLimit(models.Model):
    """Database-shared fixed windows, independent of browser cookies/workers."""

    key = models.CharField(max_length=64, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return self.key


class BookingHistoryImport(models.Model):
    """One user-initiated cursor job; no contact details in task arguments."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    identity_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default="queued")
    cursor = models.PositiveBigIntegerField(null=True)
    seen_cursors = models.JSONField(default=list)
    pages = models.PositiveIntegerField(default=0)
    added = models.PositiveIntegerField(default=0)
    conflicts = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    lease = models.UUIDField(null=True)
    locked_until = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=32, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"BookingHistoryImport {self.pk}"


class GuestBookingHistory(models.Model):
    """Read-only matched Hostaway projection, never an operational reservation."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    hostaway_reservation_id = models.PositiveBigIntegerField(unique=True)
    property = models.ForeignKey("properties.Property", null=True, on_delete=models.SET_NULL)
    property_name = models.CharField(max_length=200, blank=True)
    channel_id = models.PositiveIntegerField(null=True)
    channel_name = models.CharField(max_length=100, blank=True)
    normalized_status = models.CharField(max_length=30)
    check_in = models.DateField()
    check_out = models.DateField()
    guests = models.PositiveSmallIntegerField()
    currency = models.CharField(max_length=3)
    total_price = models.DecimalField(max_digits=14, decimal_places=4)
    last_seen_generation = models.UUIDField()
    identity_digest = models.CharField(max_length=64)
    is_visible = models.BooleanField(default=True)
    checked_at = models.DateTimeField(default=timezone.now)

    history_only = True

    class Meta:
        indexes = [models.Index(fields=["user", "is_visible", "check_out"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(check_out__gt=models.F("check_in")),
                name="guest_history_dates_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(total_price__gte=0), name="guest_history_total_valid"
            ),
        ]

    def __str__(self):
        return f"GuestBookingHistory {self.pk}"


class CustomerProfile(models.Model):
    """Verification state for a customer account.

    Verification is deliberately not modelled as ``User.is_active``. A guest who
    has just paid must be able to reach the booking immediately; blocking the
    account until an inbox is opened would strand them straight after checkout.
    An unverified account works, and is only barred from the actions that need a
    trusted address.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="customer_profile",
        verbose_name=_("Account"),
    )
    email_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Email verified at"),
    )
    auth_version = models.UUIDField(default=uuid.uuid4, editable=False)
    identity_changed_at = models.DateTimeField(null=True, blank=True)
    # Stored so a verification issued for one address cannot confirm another
    # after the customer edits it mid-flight.
    verification_sent_for = models.EmailField(blank=True, verbose_name=_("Address awaiting proof"))
    verification_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Verification sent at"),
    )
    # Contact and residence information belongs to the guest's profile rather
    # than to an individual stay.  This keeps a returning guest in control of
    # their current details and avoids copying personal data across bookings.
    phone = models.CharField(max_length=32, blank=True, verbose_name=_("Phone number"))
    preferred_language = models.CharField(
        max_length=2,
        blank=True,
        choices=[("ar", _("Arabic")), ("en", _("English")), ("fr", _("French"))],
        verbose_name=_("Preferred language"),
    )
    residence_address_line1 = models.CharField(
        max_length=250,
        blank=True,
        verbose_name=_("Residence address"),
    )
    residence_city = models.CharField(max_length=120, blank=True, verbose_name=_("Residence city"))
    residence_region = models.CharField(
        max_length=120, blank=True, verbose_name=_("Residence region")
    )
    residence_postal_code = models.CharField(
        max_length=32,
        blank=True,
        verbose_name=_("Residence postal code"),
    )
    residence_country = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Residence country"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Customer profile")
        verbose_name_plural = _("Customer profiles")

    def __str__(self) -> str:
        return self.user.get_username()

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    def mark_verified(self) -> None:
        from django.db import transaction

        from apps.loyalty.events import record
        from apps.loyalty.models import GuestEvent

        with transaction.atomic():
            self.email_verified_at = timezone.now()
            self.verification_sent_for = ""
            self.save(update_fields=["email_verified_at", "verification_sent_for", "updated_at"])
            record(
                customer=self.user,
                kind=GuestEvent.Kind.EMAIL,
                reference=f"email-proof:{self.user_id}:{self.email_verified_at.isoformat()}",
                source="guest",
                actor=self.user,
                occurred_at=self.email_verified_at,
                after={"email": self.user.email},
            )


def profile_for(user: object) -> CustomerProfile:
    """Return the profile for ``user``, creating it on first need."""
    profile, _created = CustomerProfile.objects.get_or_create(user=user)
    return profile


class OperationsManager(models.Model):
    """Owner-managed allowlist, separate from guest accounts and Django roles."""

    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=150, blank=True)
    is_enabled = models.BooleanField(default=True)
    # Invalidates previously authenticated sessions on revoke/re-enable.
    access_version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="manager_grants",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_google_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-is_enabled", "display_name", "email"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")), name="operations_manager_email_lowercase"
            )
        ]

    def __str__(self):
        return self.display_name or self.email
