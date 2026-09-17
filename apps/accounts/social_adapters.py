"""Social sign-in rules which keep identity separate from authorization."""

from __future__ import annotations

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth import get_user_model
from django.db import transaction

from .access import normalize_email
from .models import profile_for
from .services import claim_reservations_for_verified_email


class LuxurySocialAccountAdapter(DefaultSocialAccountAdapter):
    """Create ordinary guest accounts, with one tightly scoped owner exception."""

    def populate_user(self, request, sociallogin, data):  # type: ignore[no-untyped-def]
        user = super().populate_user(request, sociallogin, data)
        # This project uses the email as the username for its existing local
        # accounts. Keep new social accounts consistent with that convention.
        if user.email:
            user.username = normalize_email(user.email)[:150]
        return user

    def pre_social_login(self, request, sociallogin):  # type: ignore[no-untyped-def]
        """Attach verified Google identity to the one operations owner only.

        Google supplies an explicit verified-email claim. Neither a claimed
        email value nor an Apple identity is sufficient to create staff access.
        """

        super().pre_social_login(request, sociallogin)
        if sociallogin.account.provider != "google":
            return

        owner_email = self._verified_owner_email(sociallogin)
        if not owner_email:
            return

        with transaction.atomic():
            owner = self._get_or_create_owner(owner_email)
            if not sociallogin.is_existing:
                sociallogin.connect(request, owner)

    def save_user(self, request, sociallogin, form=None):  # type: ignore[no-untyped-def]
        """Provider-verified guest emails unlock the same booking dashboard."""
        user = super().save_user(request, sociallogin, form)
        normalized_email = normalize_email(user.email)
        verified = any(
            address.verified and normalize_email(address.email) == normalized_email
            for address in sociallogin.email_addresses
        )
        if verified:
            profile_for(user).mark_verified()
            claim_reservations_for_verified_email(user)
        return user

    @staticmethod
    def _verified_owner_email(sociallogin):  # type: ignore[no-untyped-def]
        from django.conf import settings

        for address in sociallogin.email_addresses:
            if (
                address.verified
                and normalize_email(address.email) == settings.OPERATIONS_OWNER_EMAIL
            ):
                return settings.OPERATIONS_OWNER_EMAIL
        return ""

    @staticmethod
    def _get_or_create_owner(email: str):
        """Provision the owner only after Google verifies the exact address."""

        user_model = get_user_model()
        owner = user_model.objects.filter(email__iexact=email).first()
        if owner is None:
            owner = user_model(username=email[:150], email=email)
            owner.set_unusable_password()

        owner.username = email[:150]
        owner.email = email
        owner.is_active = True
        owner.is_staff = True
        owner.is_superuser = True
        owner.save()
        # ``SocialLogin.connect()`` deliberately skips allauth's normal email
        # persistence.  Record the address here because this branch only runs
        # after Google has supplied its verified claim for the one owner email.
        from allauth.account.models import EmailAddress

        EmailAddress.objects.update_or_create(
            user=owner,
            email=email,
            defaults={"verified": True, "primary": True},
        )
        profile_for(owner).mark_verified()
        return owner
