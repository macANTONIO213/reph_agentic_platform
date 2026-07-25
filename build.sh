#!/usr/bin/env bash
# Build script for Render deployment
set -o errexit

pip install -r requirements.txt -c constraints.txt
python manage.py collectstatic --noinput
python manage.py migrate
