from django.core.management import call_command
from django.test import TestCase

from apps.properties.models import Property, PropertyImage


class SeedStagingDemoDataTests(TestCase):
    def test_seed_is_idempotent_and_keeps_morocco_in_mad(self) -> None:
        call_command("seed_staging_demo_data")
        call_command("seed_staging_demo_data")

        self.assertEqual(Property.objects.count(), 7)
        self.assertEqual(PropertyImage.objects.count(), 7)
        morocco = Property.objects.get(hostaway_listing_id=511786)
        self.assertEqual(morocco.currency_code, "MAD")
        self.assertTrue(morocco.images.get().is_cover)
