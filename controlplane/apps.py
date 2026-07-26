from django.apps import AppConfig


class ControlplaneConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "controlplane"
    verbose_name = "Agentic Platform Control Plane"

    def ready(self):
        # SSRF hardening: re-validate every HTTP redirect hop platform-wide so a
        # validated destination cannot bounce an outbound call to an internal/
        # metadata address (audit S-01).
        from controlplane.services.interop.net_guard import install_safe_opener

        install_safe_opener()
