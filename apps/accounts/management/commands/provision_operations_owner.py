"""Provision the one private operations owner without handling a password."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Create or repair the designated operations owner. New accounts receive "
        "no password and must sign in through verified Google identity."
    )

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--revoke-other-admin-access",
            action="store_true",
            help="Remove staff and superuser flags from every other account.",
        )

    def handle(self, *args, **options) -> str:  # type: ignore[no-untyped-def]
        del args
        user_model = get_user_model()
        owner = user_model.objects.filter(
            email__iexact=settings.OPERATIONS_OWNER_EMAIL
        ).first()
        created = owner is None
        if owner is None:
            owner = user_model(
                username=settings.OPERATIONS_OWNER_EMAIL[:150],
                email=settings.OPERATIONS_OWNER_EMAIL,
            )
            owner.set_unusable_password()

        owner.username = settings.OPERATIONS_OWNER_EMAIL[:150]
        owner.email = settings.OPERATIONS_OWNER_EMAIL
        owner.is_active = True
        owner.is_staff = True
        owner.is_superuser = True
        owner.save()

        revoked = 0
        if options["revoke_other_admin_access"]:
            revoked = user_model.objects.exclude(pk=owner.pk).filter(
                is_staff=True
            ).update(is_staff=False, is_superuser=False)

        status = "created" if created else "repaired"
        message = f"Operations owner {status}."
        if options["revoke_other_admin_access"]:
            message += f" Removed administrative flags from {revoked} other account(s)."
        self.stdout.write(self.style.SUCCESS(message))
        return message
