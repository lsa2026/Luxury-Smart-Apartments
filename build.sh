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
