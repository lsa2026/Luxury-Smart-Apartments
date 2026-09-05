from django import forms
from django.contrib import admin
from django.core.cache import cache
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.notifications.services.audit import record_audit

from .models import (
    ContactMessage,
    FAQItem,
    LegacyRedirect,
    MarketingEventReceipt,
    SitePage,
    SiteSetting,
)

admin.site.site_header = "Luxury Smart Apartments"
admin.site.site_title = _("Platform administration")
admin.site.index_title = _("Command centre")
admin.site.index_template = "admin/index.html"


@admin.register(SitePage)
class SitePageAdmin(admin.ModelAdmin):
    list_display = (
        "slug",
        "title_ar",
        "title_en",
        "title_fr",
        "is_published",
        "last_reviewed_at",
        "updated_at",
    )
    list_filter = ("is_published",)
    list_editable = ("is_published",)
    readonly_fields = ("updated_at",)
    fieldsets = (
        (
            _("Publishing"),
            {"fields": ("slug", "is_published", "last_reviewed_at", "updated_at")},
        ),
        (
            _("Arabic"),
            {"fields": ("title_ar", "body_ar", "meta_description_ar")},
        ),
        (
            "English",
            {"fields": ("title_en", "body_en", "meta_description_en")},
        ),
        (
            "Français",
            {
                "classes": ("collapse",),
                "fields": ("title_fr", "body_fr", "meta_description_fr"),
            },
        ),
    )
    search_fields = (
        "slug",
        "title_ar",
        "title_en",
        "title_fr",
        "body_ar",
        "body_en",
        "body_fr",
    )

    def save_model(
        self,
        request: HttpRequest,
        obj: SitePage,
        form: object,
        change: bool,
    ) -> None:
        super().save_model(request, obj, form, change)
        cache.delete("seo:sitemap:v1")
        record_audit(
            request=request,
            action="site_content.changed",
            object_type="SitePage",
            object_reference=obj.slug,
            summary="Public site content was updated.",
            metadata={"fields": getattr(form, "changed_data", [])},
        )


@admin.register(FAQItem)
class FAQItemAdmin(admin.ModelAdmin):
    list_display = ("question_ar", "category", "property", "sort_order", "is_active")
    list_filter = ("is_active", "category", "property")
    autocomplete_fields = ("property",)
    search_fields = (
        "question_ar",
        "question_en",
        "question_fr",
        "answer_ar",
        "answer_en",
        "answer_fr",
    )
    list_editable = ("sort_order", "is_active")


class SiteSettingAdminForm(forms.ModelForm):
    class Meta:
        model = SiteSetting
        fields = (
            "site_name",
            "tagline_ar",
            "tagline_en",
            "tagline_fr",
            "footer_text_ar",
            "footer_text_en",
            "footer_text_fr",
            "contact_email",
            "contact_phone",
            "whatsapp_display_number",
            "whatsapp_url",
            "instagram_url",
            "facebook_url",
            "x_url",
            "linkedin_url",
            "office_hours_ar",
            "office_hours_en",
            "office_hours_fr",
            "public_address_ar",
            "public_address_en",
            "public_address_fr",
        )
        labels = {
            "site_name": _("Site name"),
            "tagline_ar": _("Arabic tagline"),
            "tagline_en": _("English tagline"),
            "tagline_fr": _("French tagline"),
            "footer_text_ar": _("Arabic footer text"),
            "footer_text_en": _("English footer text"),
            "footer_text_fr": _("French footer text"),
            "contact_email": _("Contact email"),
            "contact_phone": _("Contact phone"),
            "whatsapp_display_number": _("WhatsApp display number"),
            "whatsapp_url": _("WhatsApp link"),
            "instagram_url": _("Instagram link"),
            "facebook_url": _("Facebook link"),
            "x_url": _("X link"),
            "linkedin_url": _("LinkedIn link"),
            "office_hours_ar": _("Arabic office hours"),
            "office_hours_en": _("English office hours"),
            "office_hours_fr": _("French office hours"),
            "public_address_ar": _("Arabic public address"),
            "public_address_en": _("English public address"),
            "public_address_fr": _("French public address"),
        }


@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    form = SiteSettingAdminForm
    list_display = ("site_name", "contact_readiness", "updated_at")
    fieldsets = (
        (
            _("Configuration readiness"),
            {"fields": ("configuration_readiness",)},
        ),
        (
            _("Brand"),
            {
                "fields": (
                    "site_name",
                    "tagline_ar",
                    "tagline_en",
                    "tagline_fr",
                    "footer_text_ar",
                    "footer_text_en",
                    "footer_text_fr",
                )
            },
        ),
        (
            _("Default stay times"),
            {
                "description": _(
                    "Used only when neither a property override nor a channel-manager time exists."
                ),
                "fields": (
                    "default_check_in_hour",
                    "default_check_out_hour",
                ),
            },
        ),
        (
            _("Default cancellation policy"),
            {
                "description": _(
                    "Managed content shown when a property has no cancellation-policy text."
                ),
                "fields": (
                    "default_cancellation_policy_ar",
                    "default_cancellation_policy_en",
                    "default_cancellation_policy_fr",
                ),
            },
        ),
        (
            _("Default house rules"),
            {
                "description": _("Managed content shown when a property has no local house rules."),
                "fields": (
                    "default_house_rules_ar",
                    "default_house_rules_en",
                    "default_house_rules_fr",
                ),
            },
        ),
        (
            _("Contact"),
            {
                "fields": (
                    "contact_email",
                    "contact_phone",
                    "whatsapp_display_number",
                    "whatsapp_url",
                    "instagram_url",
                    "facebook_url",
                    "x_url",
                    "linkedin_url",
                    "office_hours_ar",
                    "office_hours_en",
                    "office_hours_fr",
                    "public_address_ar",
                    "public_address_en",
                    "public_address_fr",
                )
            },
        ),
        (_("System"), {"fields": ("updated_at",)}),
    )
    readonly_fields = ("site_name", "configuration_readiness", "updated_at")

    @admin.display(boolean=True, description=_("Contact information complete"))
    def contact_readiness(self, obj: SiteSetting) -> bool:
        return bool(obj.contact_email and obj.contact_phone and obj.whatsapp_url)

    @admin.display(description=_("Configuration status"))
    def configuration_readiness(self, obj: SiteSetting) -> str:
        required = (
            ("contact_email", _("Contact email")),
            ("contact_phone", _("Contact phone")),
            ("whatsapp_url", _("WhatsApp link")),
        )
        missing = [str(label) for field, label in required if not getattr(obj, field)]
        if not missing:
            return format_html(
                '<strong class="lsa-admin-ready">{}</strong>',
                _("Essential public contact information is complete."),
            )
        return format_html(
            '<div class="lsa-admin-warning"><strong>{}</strong><p>{}</p></div>',
            _("Complete the essential contact information"),
            _("Missing: %(fields)s") % {"fields": "، ".join(missing)},
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return not SiteSetting.objects.exists() and super().has_add_permission(request)

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: SiteSetting | None = None,
    ) -> bool:
        return False

    def save_model(
        self,
        request: HttpRequest,
        obj: SiteSetting,
        form: object,
        change: bool,
    ) -> None:
        super().save_model(request, obj, form, change)
        cache.delete("site:settings")
        record_audit(
            request=request,
            action="site_settings.changed",
            object_type="SiteSetting",
            object_reference=str(obj.pk),
            summary="Public site settings were updated.",
            metadata={"fields": getattr(form, "changed_data", [])},
        )


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


@admin.register(LegacyRedirect)
class LegacyRedirectAdmin(admin.ModelAdmin):
    list_display = (
        "source_path",
        "destination_path",
        "redirect_type",
        "is_active",
        "hit_count",
        "last_hit_at",
    )
    list_filter = ("redirect_type", "is_active")
    search_fields = ("source_path", "destination_path")
    readonly_fields = ("hit_count", "last_hit_at", "created_at", "updated_at")

    def save_model(
        self,
        request: HttpRequest,
        obj: LegacyRedirect,
        form: object,
        change: bool,
    ) -> None:
        super().save_model(request, obj, form, change)
        record_audit(
            request=request,
            action="legacy_redirect.changed",
            object_type="LegacyRedirect",
            object_reference=obj.source_path,
            summary="A legacy redirect mapping was updated.",
            metadata={
                "fields": getattr(form, "changed_data", []),
                "redirect_type": obj.redirect_type,
            },
        )


@admin.register(MarketingEventReceipt)
class MarketingEventReceiptAdmin(admin.ModelAdmin):
    list_display = ("event_name", "object_type", "status", "emitted_at", "created_at")
    list_filter = ("event_name", "status", "created_at")
    readonly_fields = (
        "id",
        "event_name",
        "object_type",
        "object_reference_hash",
        "status",
        "emitted_at",
        "created_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: MarketingEventReceipt | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: MarketingEventReceipt | None = None,
    ) -> bool:
        return False
