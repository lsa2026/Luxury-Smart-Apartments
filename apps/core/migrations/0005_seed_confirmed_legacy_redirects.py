from django.db import migrations


CONFIRMED_REDIRECTS = (
    ("/about-us/", "/about/", 301),
    ("/contact-us/", "/contact/", 301),
    ("/my-bookings/", "", 410),
    ("/login/", "", 410),
    ("/register/", "", 410),
    ("/compare/", "", 410),
)


def seed_confirmed_redirects(apps, schema_editor) -> None:
    del schema_editor
    LegacyRedirect = apps.get_model("core", "LegacyRedirect")
    for source_path, destination_path, redirect_type in CONFIRMED_REDIRECTS:
        LegacyRedirect.objects.get_or_create(
            source_path=source_path,
            defaults={
                "destination_path": destination_path,
                "redirect_type": redirect_type,
                "is_active": True,
            },
        )


def remove_confirmed_redirects(apps, schema_editor) -> None:
    del schema_editor
    LegacyRedirect = apps.get_model("core", "LegacyRedirect")
    LegacyRedirect.objects.filter(
        source_path__in=[row[0] for row in CONFIRMED_REDIRECTS]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_legacyredirect_marketingeventreceipt"),
    ]

    operations = [
        migrations.RunPython(seed_confirmed_redirects, remove_confirmed_redirects),
    ]
