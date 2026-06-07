import django.core.validators
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("controlplane", "0002_alter_agent_risk_tier"),
    ]

    operations = [
        # Agent: add version and endpoint_url
        migrations.AddField(
            model_name="agent",
            name="version",
            field=models.CharField(default="1.0", max_length=20),
        ),
        migrations.AddField(
            model_name="agent",
            name="endpoint_url",
            field=models.URLField(blank=True, default=""),
        ),
        # AgentRun: add token tracking
        migrations.AddField(
            model_name="agentrun",
            name="input_tokens",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="output_tokens",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="model_id",
            field=models.CharField(blank=True, default="", max_length=60),
        ),
        # GovernanceReview
        migrations.CreateModel(
            name="GovernanceReview",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("agent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="governance_reviews", to="controlplane.agent")),
                ("reviewer", models.CharField(max_length=120)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")], default="pending", max_length=20)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        # AgentFeedback
        migrations.CreateModel(
            name="AgentFeedback",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="feedback", to="controlplane.agentrun")),
                ("rating", models.PositiveSmallIntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(5)])),
                ("comment", models.TextField(blank=True)),
                ("submitted_by", models.CharField(max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        # AgentVersion
        migrations.CreateModel(
            name="AgentVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("agent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="versions", to="controlplane.agent")),
                ("version", models.CharField(max_length=20)),
                ("system_prompt", models.TextField()),
                ("tool_names", models.JSONField(default=list)),
                ("model_id", models.CharField(blank=True, default="", max_length=60)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        # ConversationSession
        migrations.CreateModel(
            name="ConversationSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("agent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sessions", to="controlplane.agent")),
                ("user_label", models.CharField(max_length=120)),
                ("messages", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-updated_at"]},
        ),
    ]
