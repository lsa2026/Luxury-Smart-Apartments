#!/usr/bin/env bash
# Render build step. Any failure must abort the deploy rather than ship a
# half-built image, so the script exits on the first error.
set -o errexit
set -o nounset
set -o pipefail

pip install --upgrade pip
pip install .

# Compiled catalogs (.mo) are committed, so gettext is not needed at build time.
python manage.py collectstatic --no-input
python manage.py migrate --no-input
# Hostaway names arrive in English.  Fill the curated Arabic/French labels for
# existing and newly-synced amenities without overwriting dashboard wording.
python manage.py seed_amenity_arabic_names

# The owned Render preview starts with an empty database.  Its small, curated
# catalogue lets us exercise public pages and the operations area without
# granting the preview any Hostaway, payment, or messaging credentials.
if [ "${STAGING_DEMO_DATA_ENABLED:-False}" = "True" ]; then
  python manage.py seed_staging_demo_data
  python manage.py publish_hostaway_property_locations
  python manage.py apply_trustindex_widgets
  python manage.py sync_trustindex_review_metrics
fi
