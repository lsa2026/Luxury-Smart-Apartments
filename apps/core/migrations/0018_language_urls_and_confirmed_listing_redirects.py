from django.db import migrations


CONFIRMED_REDIRECTS = (
    ("/about-us/", "/ar/about/", 301),
    ("/contact-us/", "/ar/contact/", 301),
    (
        "/listing/darat-safa-luxury-apartment/",
        "/ar/properties/darat-safa-luxury-apartment/",
        301,
    ),
    (
        "/listing/luxury-smart-apartment-a11/",
        "/ar/properties/luxury-smart-apartment-a11/",
        301,
    ),
    (
        "/listing/luxury-smart-apartment-e12/",
        "/ar/properties/luxury-smart-apartment-e12/",
        301,
    ),
    (
        "/listing/luxury-smart-apartment-b12/",
        "/ar/properties/luxury-smart-apartment-b12/",
        301,
    ),
    (
        "/listing/al-hamra-family-villa/",
        "/ar/properties/al-hamra-family-villa/",
        301,
    ),
    (
        "/listing/luxury-smart-apartment-at-nour-prestige-marrakech/",
        "/ar/properties/luxury-smart-apartment-at-nour-prestige-marrakech/",
        301,
    ),
)


def apply_language_redirects(apps, schema_editor) -> None:
    del schema_editor
    LegacyRedirect = apps.get_model("core", "LegacyRedirect")
    for source_path, destination_path, redirect_type in CONFIRMED_REDIRECTS:
        LegacyRedirect.objects.update_or_create(
            source_path=source_path,
            defaults={
                "destination_path": destination_path,
                "redirect_type": redirect_type,
                "is_active": True,
            },
        )


def restore_previous_redirects(apps, schema_editor) -> None:
    del schema_editor
    LegacyRedirect = apps.get_model("core", "LegacyRedirect")
    LegacyRedirect.objects.filter(source_path="/about-us/").update(destination_path="/about/")
    LegacyRedirect.objects.filter(source_path="/contact-us/").update(destination_path="/contact/")
    LegacyRedirect.objects.filter(
        source_path__in=[source_path for source_path, _destination, _status in CONFIRMED_REDIRECTS[2:]]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_merge_20260906_0000"),
    ]

    operations = [
        migrations.RunPython(apply_language_redirects, restore_previous_redirects),
    ]
