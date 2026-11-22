from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("loyalty", "0010_invitation_api_credential")]
    operations = [
        migrations.AddField(
            model_name="joininvitation",
            name="telegram_target_digest",
            field=models.CharField(blank=True, max_length=64),
        )
    ]
