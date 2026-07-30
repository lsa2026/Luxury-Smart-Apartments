from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    verbose_name = "الإشعارات والتشغيل"

    def ready(self) -> None:
        from . import checks  # noqa: F401
