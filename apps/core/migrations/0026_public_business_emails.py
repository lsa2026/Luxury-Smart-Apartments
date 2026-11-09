"""Publish the owner's approved aliases without touching mailbox identities or history."""

import re

from django.db import migrations


OLD_EMAIL = "saeed@luxurysmartapartments.com"
INFO_EMAIL = "info@luxurysmartapartments.com"


def publish_business_emails(apps, schema_editor):
    alias = schema_editor.connection.alias
    settings_model = apps.get_model("core", "SiteSetting")
    settings_model.objects.using(alias).filter(contact_email__iexact=OLD_EMAIL).update(
        contact_email=INFO_EMAIL
    )
    # Public editable copy only; guest records and frozen consent snapshots are excluded.
    for model_name, prefixes in (
        ("SitePage", ("title_", "body_", "meta_description_")),
        ("FAQItem", ("question_", "answer_")),
        ("SiteSetting", ("footer_text_", "default_house_rules_", "default_cancellation_policy_")),
    ):
        model = apps.get_model("core", model_name)
        fields = [f.name for f in model._meta.fields if f.name.startswith(prefixes)]
        for obj in model.objects.using(alias).only("pk", *fields).iterator():
            changes = {}
            for field in fields:
                current = getattr(obj, field)
                updated = re.sub(re.escape(OLD_EMAIL), INFO_EMAIL, current or "", flags=re.I)
                if updated != current:
                    changes[field] = updated
            if changes:
                model.objects.using(alias).filter(pk=obj.pk).update(**changes)


class Migration(migrations.Migration):
    dependencies = [("core", "0025_contact_read_and_sama_drafts")]

    operations = [migrations.RunPython(publish_business_emails, migrations.RunPython.noop)]
