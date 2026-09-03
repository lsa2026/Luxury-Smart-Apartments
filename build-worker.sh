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
