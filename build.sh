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

# Apply reviewed guest-facing data after migrations.  Each command is
# idempotent: it changes only a record that is not already current.  Keeping
# this in the web build makes a fresh Render database match the release code
# without relying on an undocumented manual step.
python manage.py apply_property_localizations
python manage.py publish_hostaway_property_locations
python manage.py apply_trustindex_widgets

# Hostaway names arrive in English.  Fill the curated Arabic/French labels for
# existing and newly-synced amenities without overwriting dashboard wording.
python manage.py seed_amenity_arabic_names
