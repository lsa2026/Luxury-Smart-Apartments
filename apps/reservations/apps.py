from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ReservationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reservations"
    verbose_name = _("Bookings")

    def ready(self):
        from django.db.models.signals import post_migrate

        from .telegram_refund_copy_requests import after_migrate

        post_migrate.connect(after_migrate, sender=self, dispatch_uid="site01_explicit_refund_copy")
