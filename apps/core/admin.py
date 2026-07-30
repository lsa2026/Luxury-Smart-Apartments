from django.contrib import admin
from django.http import HttpRequest

from .models import ContactMessage, FAQItem, SitePage, SiteSetting


@admin.register(SitePage)
class SitePageAdmin(admin.ModelAdmin):
    list_display = ("slug", "title_ar", "title_en", "is_published", "updated_at")
    list_filter = ("is_published",)
    search_fields = ("slug", "title_ar", "title_en", "body_ar", "body_en")


@admin.register(FAQItem)
class FAQItemAdmin(admin.ModelAdmin):
    list_display = ("question_ar", "question_en", "sort_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("question_ar", "question_en", "answer_ar", "answer_en")
    list_editable = ("sort_order", "is_active")


@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    fieldsets = (
        ("العلامة", {"fields": ("site_name", "tagline_ar", "tagline_en")}),
        (
            "التواصل",
            {
                "fields": (
                    "contact_email",
                    "contact_phone",
                    "whatsapp_url",
                    "instagram_url",
                    "office_hours_ar",
                    "office_hours_en",
                )
            },
        ),
        ("النظام", {"fields": ("updated_at",)}),
    )
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return not SiteSetting.objects.exists() and super().has_add_permission(request)

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: SiteSetting | None = None,
    ) -> bool:
        return False


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("subject", "status", "language", "created_at")
    list_filter = ("status", "language", "created_at")
    search_fields = ("subject", "name", "email")
    readonly_fields = (
        "name",
        "email",
        "phone",
        "subject",
        "message",
        "language",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields + ("status",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: ContactMessage | None = None,
    ) -> bool:
        return request.user.is_superuser
