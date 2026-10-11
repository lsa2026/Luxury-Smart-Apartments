from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reservations", "0020_manualbookingdraft_guest_language")]
    operations = [migrations.AddField(model_name="reservation", name="loyalty_state",
        field=models.JSONField(blank=True, default=dict, editable=False))]
