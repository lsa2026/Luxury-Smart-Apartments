from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("properties", "0018_property_google_maps_cid")]

    operations = [
        migrations.AddField(model_name="property", name="trustindex_widget_id", field=models.CharField(blank=True, max_length=32, verbose_name="Trustindex review widget identifier")),
        migrations.AddField(model_name="property", name="trustindex_rating", field=models.DecimalField(blank=True, decimal_places=1, max_digits=2, null=True, verbose_name="Trustindex rating out of 5")),
        migrations.AddField(model_name="property", name="trustindex_review_count", field=models.PositiveIntegerField(default=0, verbose_name="Trustindex review count")),
        migrations.AddField(model_name="property", name="trustindex_synced_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Trustindex metrics last refreshed")),
        migrations.AddConstraint(model_name="property", constraint=models.CheckConstraint(condition=Q(trustindex_rating__isnull=True) | Q(trustindex_rating__gte=0, trustindex_rating__lte=5), name="property_trustindex_rating_0_5")),
    ]
