"""Keep allauth's security flow, but render its mail with the approved identity."""

from allauth.account.adapter import DefaultAccountAdapter
from django.utils.translation import get_language

from apps.notifications.services.email import _email_brand_context


class LuxuryAccountAdapter(DefaultAccountAdapter):
    def render_mail(self, template_prefix, email, context, headers=None):
        language = (get_language() or "ar").split("-")[0]
        brand = _email_brand_context(language)
        values = {**context, **brand}
        if template_prefix in {
            "account/email/email_confirmation",
            "account/email/email_confirmation_signup",
        }:
            values.update(
                heading=brand["email_copy"]["confirmation_heading"],
                message=brand["email_copy"]["confirmation_message"],
            )
        message = super().render_mail(template_prefix, email, values, headers=headers)
        if not message.reply_to and brand["support_email"]:
            message.reply_to = [brand["support_email"]]
        return message
