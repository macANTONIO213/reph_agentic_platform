"""
Migration 0013 — Agent Factory: ProcessInsight + AgentBlueprint
"""
from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("controlplane", "0012_phase_e_orchestration"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProcessInsight",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_reference", models.CharField(
                    help_text="Stable external ID — used for deduplication.",
                    max_length=200,
                    unique=True,
                )),
                ("process_name", models.CharField(max_length=200)),
                ("finding_type", models.CharField(
                    choices=[
                        ("bottleneck", "Bottleneck"),
                        ("exception", "Exception Pattern"),
                        ("control_gap", "Control Gap"),
                        ("automation_opportunity", "Automation Opportunity"),
                        ("rework_pattern", "Rework Pattern"),
                        ("other", "Other"),
                    ],
                    default="other",
                    max_length=40,
                )),
                ("summary", models.TextField()),
                ("evidence", models.JSONField(blank=True, default=dict,
                    help_text="Supporting metrics, examples, or observations.")),
                ("impact", models.TextField(blank=True)),
                ("frequency", models.CharField(blank=True, max_length=200)),
                ("systems_involved", models.JSONField(blank=True, default=list,
                    help_text="List of application/API/dataset names involved.")),
                ("recommended_action", models.TextField(blank=True)),
                ("risk_notes", models.TextField(blank=True)),
                ("business_unit", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="process_insights",
                    to="controlplane.businessunit",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="AgentBlueprint",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version", models.PositiveSmallIntegerField(default=1)),
                ("agent_name", models.CharField(max_length=200)),
                ("mission", models.TextField()),
                ("trigger", models.CharField(blank=True, max_length=200,
                    help_text="What causes the agent to run.")),
                ("inputs", models.JSONField(blank=True, default=list,
                    help_text="Required inputs.")),
                ("outputs", models.JSONField(blank=True, default=list,
                    help_text="Expected outputs.")),
                ("tools", models.JSONField(blank=True, default=list,
                    help_text="Systems or APIs the agent needs.")),
                ("workflow_steps", models.JSONField(blank=True, default=list,
                    help_text="Step-by-step operating logic.")),
                ("guardrails", models.JSONField(blank=True, default=list,
                    help_text="Rules, permissions, and escalation conditions.")),
                ("human_approval_points", models.JSONField(blank=True, default=list,
                    help_text="Steps where agent must pause for human review.")),
                ("success_metrics", models.JSONField(blank=True, default=list,
                    help_text="Cycle-time reduction, quality score, cost reduction, etc.")),
                ("business_value_score", models.PositiveSmallIntegerField(
                    default=0,
                    validators=[django.core.validators.MinValueValidator(0),
                                django.core.validators.MaxValueValidator(10)],
                )),
                ("automation_fit_score", models.PositiveSmallIntegerField(
                    default=0,
                    validators=[django.core.validators.MinValueValidator(0),
                                django.core.validators.MaxValueValidator(10)],
                )),
                ("complexity_score", models.PositiveSmallIntegerField(
                    default=0,
                    help_text="Higher = more complex.",
                    validators=[django.core.validators.MinValueValidator(0),
                                django.core.validators.MaxValueValidator(10)],
                )),
                ("risk_score", models.PositiveSmallIntegerField(
                    default=0,
                    validators=[django.core.validators.MinValueValidator(0),
                                django.core.validators.MaxValueValidator(10)],
                )),
                ("opportunity_score", models.FloatField(
                    default=0.0,
                    help_text="Composite score.",
                )),
                ("status", models.CharField(
                    choices=[
                        ("draft", "Draft"),
                        ("needs_data", "Needs Data"),
                        ("needs_tool", "Needs Tool"),
                        ("approved", "Approved"),
                        ("built", "Built"),
                        ("deployed", "Deployed"),
                        ("retired", "Retired"),
                    ],
                    default="draft",
                    max_length=20,
                )),
                ("risk_level", models.CharField(
                    choices=[
                        ("low", "Low"),
                        ("medium", "Medium"),
                        ("high", "High"),
                        ("blocked", "Blocked"),
                    ],
                    default="low",
                    max_length=10,
                )),
                ("missing_tools", models.JSONField(blank=True, default=list,
                    help_text="Tools that must be available before build.")),
                ("missing_data", models.JSONField(blank=True, default=list,
                    help_text="Data sources that must be accessible before build.")),
                ("approval_notes", models.TextField(blank=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("insight", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="blueprints",
                    to="controlplane.processinsight",
                )),
                ("approved_by", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="approved_blueprints",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("built_agent", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="source_blueprints",
                    to="controlplane.agent",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-opportunity_score", "-created_at"],
            },
        ),
    ]
