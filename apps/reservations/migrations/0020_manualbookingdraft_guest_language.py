from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0019_guest_document_acceptance")]

    operations = [
        migrations.AddField(
            model_name="manualbookingdraft",
            name="guest_language",
            field=models.CharField(
                blank=True,
                choices=[("ar", "العربية"), ("en", "الإنجليزية"), ("fr", "الفرنسية")],
                db_default="",
                default="",
                max_length=2,
            ),
        ),
    ]
