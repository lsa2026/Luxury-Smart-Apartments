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
