"""Account state that does not belong on Django's own user row."""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


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
        self.email_verified_at = timezone.now()
        self.verification_sent_for = ""
        self.save(update_fields=["email_verified_at", "verification_sent_for", "updated_at"])


def profile_for(user: object) -> CustomerProfile:
    """Return the profile for ``user``, creating it on first need."""
    profile, _created = CustomerProfile.objects.get_or_create(user=user)
    return profile
