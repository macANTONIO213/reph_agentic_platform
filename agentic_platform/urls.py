from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from controlplane.api import health_views

urlpatterns = [
    # Probe endpoints — unauthenticated, side-effect-free (for LB/k8s/Render).
    path("healthz", health_views.livez, name="livez"),
    path("readyz", health_views.readyz, name="readyz"),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("api/v1/", include("controlplane.api.urls")),
    path("a2a/", include("controlplane.api.a2a_urls")),
    path("", include("controlplane.urls")),
]

# UX-3: OIDC SSO endpoints (only when an IdP is configured via env).
if settings.OIDC_ENABLED:
    urlpatterns.insert(3, path("oidc/", include("mozilla_django_oidc.urls")))
