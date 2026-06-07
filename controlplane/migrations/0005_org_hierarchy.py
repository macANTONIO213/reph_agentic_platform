import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("controlplane", "0004_agent_model_id"),
    ]

    operations = [
        # BusinessUnit
        migrations.CreateModel(
            name="BusinessUnit",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=120, unique=True)),
                ("code", models.SlugField(unique=True)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
        # Division
        migrations.CreateModel(
            name="Division",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("business_unit", models.ForeignKey(blank=True, help_text="Leave blank if this division spans all business units.", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="divisions", to="controlplane.businessunit")),
                ("name", models.CharField(max_length=120)),
                ("code", models.SlugField()),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"], "unique_together": {("business_unit", "code")}},
        ),
        # WorkStream
        migrations.CreateModel(
            name="WorkStream",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("division", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="work_streams", to="controlplane.division")),
                ("name", models.CharField(max_length=120)),
                ("code", models.SlugField()),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"], "unique_together": {("division", "code")}},
        ),
        # OrgProcess
        migrations.CreateModel(
            name="OrgProcess",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("work_stream", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="processes", to="controlplane.workstream")),
                ("name", models.CharField(max_length=120)),
                ("code", models.SlugField()),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"], "unique_together": {("work_stream", "code")}, "verbose_name": "Process", "verbose_name_plural": "Processes"},
        ),
        # Agent FK fields
        migrations.AddField(
            model_name="agent",
            name="org_unit",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agents", to="controlplane.businessunit", verbose_name="Business unit"),
        ),
        migrations.AddField(
            model_name="agent",
            name="org_division",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agents", to="controlplane.division", verbose_name="Division"),
        ),
        migrations.AddField(
            model_name="agent",
            name="org_work_stream",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agents", to="controlplane.workstream", verbose_name="Work stream"),
        ),
        migrations.AddField(
            model_name="agent",
            name="org_process",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agents", to="controlplane.orgprocess", verbose_name="Process"),
        ),
    ]
