"""WSGI config for the Agentic Platform."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agentic_platform.settings")

application = get_wsgi_application()
