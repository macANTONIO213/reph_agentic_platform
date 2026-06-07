import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("controlplane", "0006_agentrun_cost_stored"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Agent: kind + integration_mode
        migrations.AddField(
            model_name="agent",
            name="kind",
            field=models.CharField(
                choices=[("custom", "Custom (first-party)"), ("external", "External")],
                default="external",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="agent",
            name="integration_mode",
            field=models.CharField(
                choices=[
                    ("sdk",         "SDK / Callback (full governance)"),
                    ("proxy",       "Proxy / Endpoint (medium governance)"),
                    ("attestation", "Attestation-only (registered)"),
                ],
                default="proxy",
                help_text="How this agent connects to the control plane (determines governance fidelity).",
                max_length=20,
            ),
        ),
        # Approval model
        migrations.CreateModel(
            name="Approval",
            fields=[
                ("id",                   models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)),
                ("agent",                models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="approvals", to="controlplane.agent")),
                ("approved_by",          models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="approvals_granted", to=settings.AUTH_USER_MODEL)),
                ("approved_by_username", models.CharField(max_length=120)),
                ("scope",                models.CharField(default="tier4_execution", max_length=120)),
                ("notes",                models.TextField(blank=True)),
                ("expires_at",           models.DateTimeField(help_text="Approval is invalid after this time.")),
                ("is_consumed",          models.BooleanField(default=False, help_text="Set to true once used for a run.")),
                ("created_at",           models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        # AuditLog model
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id",            models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)),
                ("actor",         models.CharField(max_length=120)),
                ("action",        models.CharField(max_length=80)),
                ("resource_type", models.CharField(blank=True, max_length=60)),
                ("resource_id",   models.CharField(blank=True, max_length=60)),
                ("payload",       models.JSONField(blank=True, default=dict)),
                ("ip_address",    models.GenericIPAddressField(blank=True, null=True)),
                ("created_at",    models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        # Data migration: advisor → custom/sdk; others stay external/proxy
        migrations.RunSQL(
            sql="UPDATE controlplane_agent SET kind='custom', integration_mode='sdk' WHERE slug='agent-deployment-advisor';",
            reverse_sql="UPDATE controlplane_agent SET kind='external', integration_mode='proxy' WHERE slug='agent-deployment-advisor';",
        ),
    ]
