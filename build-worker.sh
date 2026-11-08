#!/usr/bin/env bash
# Build step for the Celery worker and beat services.
#
# Deliberately narrower than build.sh: only the web service runs migrations and
# collectstatic. Three services deploying at once must not race on the same
# schema, and background processes serve no static files.
set -o errexit
set -o nounset
set -o pipefail

pip install --upgrade pip
pip install .

# Only the production delivery worker needs the additive application schema.
# Beat stays dispatch-only: never import Django or require database credentials.
# A failed wait leaves the old worker running; retry after the web is ready.
if [[ "${RENDER_SERVICE_ID:-}" == "srv-dal662tbedkc73bdlghg" ]]; then
    python manage.py check_day_use08_readiness --wait-for-schema
fi
