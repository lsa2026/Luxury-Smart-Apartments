from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0003_bookingmodificationrequest_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingintent",
            name="language",
            field=models.CharField(
                choices=[
                    ("ar", "العربية"),
                    ("en", "English"),
                    ("fr", "Français"),
                ],
                default="ar",
                max_length=5,
            ),
        ),
    ]
