from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_customerprofile_auth_version_and_more")]
    operations = [
        migrations.AddField(
            model_name="phonechallenge",
            name="workgroup_nonce",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.AddField(
            model_name="phonechallenge",
            name="workgroup_authorized_at",
            field=models.DateTimeField(null=True, editable=False),
        ),
    ]
