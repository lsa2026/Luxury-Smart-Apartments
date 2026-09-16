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
fi

# Render's free plan has no web shell or one-off jobs.  This opt-in switch is
# therefore a deliberately temporary escape hatch for a single, reviewed,
# read-only Hostaway catalogue import into the staging database.  It must stay
# unset/False except for that one deploy; recurring sync remains disabled in
# the staging blueprint.
if [ "${HOSTAWAY_INITIAL_SYNC_ON_DEPLOY:-False}" = "True" ]; then
  python manage.py sync_hostaway_properties --force
fi
