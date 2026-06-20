"""
Migration 0014 — Agent Factory: AgentFactoryPackage

Adds the canonical handoff package model exported by the Process Intelligence
Platform / Agent Blueprint Factory.  (The AlterField operations are help-text
only — no schema change.)
"""
import django.core.validators
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('controlplane', '0013_agent_factory'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agentblueprint',
            name='complexity_score',
            field=models.PositiveSmallIntegerField(default=0, help_text='Higher = more complex (lower is better for automation).', validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10)]),
        ),
        migrations.AlterField(
            model_name='agentblueprint',
            name='human_approval_points',
            field=models.JSONField(blank=True, default=list, help_text='Steps where the agent must pause for human review.'),
        ),
        migrations.AlterField(
            model_name='agentblueprint',
            name='inputs',
            field=models.JSONField(blank=True, default=list, help_text='Required inputs: process data, documents, system records.'),
        ),
        migrations.AlterField(
            model_name='agentblueprint',
            name='opportunity_score',
            field=models.FloatField(default=0.0, help_text='Composite score: (business_value*0.35 + automation_fit*0.35 + (10-complexity)*0.2 + (10-risk)*0.1).'),
        ),
        migrations.AlterField(
            model_name='agentblueprint',
            name='outputs',
            field=models.JSONField(blank=True, default=list, help_text='Expected outputs: artifacts, actions, recommendations.'),
        ),
        migrations.AlterField(
            model_name='agentblueprint',
            name='trigger',
            field=models.CharField(blank=True, help_text='What causes the agent to run: schedule, event, API call, etc.', max_length=200),
        ),
        migrations.CreateModel(
            name='AgentFactoryPackage',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('package_id', models.CharField(help_text='Stable package identifier from the exporter — used for dedup.', max_length=200, unique=True)),
                ('external_blueprint_id', models.CharField(blank=True, help_text='blueprint_id linking back to the Agent Blueprint Factory record.', max_length=200)),
                ('package_version', models.CharField(default='agent-factory-package-v1', max_length=60)),
                ('package_type', models.CharField(default='sandbox_agent_build', max_length=60)),
                ('source', models.JSONField(blank=True, default=dict, help_text='process_insight + process_intelligence_output.')),
                ('agent_blueprint', models.JSONField(blank=True, default=dict)),
                ('agent_build_manifest', models.JSONField(blank=True, default=dict, help_text='Runtime/build instructions for creating the sandbox agent.')),
                ('tool_binding_plan', models.JSONField(blank=True, default=list, help_text='Proposed systems/tools/data bindings — never live.')),
                ('decision_policy', models.JSONField(blank=True, default=dict)),
                ('evaluation_pack', models.JSONField(blank=True, default=dict)),
                ('approval_route', models.JSONField(blank=True, default=dict)),
                ('approval_progress', models.JSONField(blank=True, default=dict)),
                ('telemetry_contract', models.JSONField(blank=True, default=dict)),
                ('telemetry_feedback_plan', models.JSONField(blank=True, default=dict)),
                ('safety_boundary', models.JSONField(blank=True, default=dict, help_text='Hard controls on what the Agent Factory may do.')),
                ('validation_report', models.JSONField(blank=True, default=dict, help_text='{ok: bool, errors: [], warnings: [], missing_sections: []}')),
                ('raw_package', models.JSONField(blank=True, default=dict, help_text='Full original package as received, for traceability.')),
                ('status', models.CharField(choices=[('received', 'Received'), ('invalid', 'Invalid'), ('sandbox_created', 'Sandbox Agent Created'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='received', max_length=20)),
                ('risk_tier', models.PositiveSmallIntegerField(default=1, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(4)])),
                ('ingested_by', models.CharField(blank=True, max_length=120)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('blueprint', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='factory_packages', to='controlplane.agentblueprint')),
                ('insight', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='factory_packages', to='controlplane.processinsight')),
                ('sandbox_agent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='source_packages', to='controlplane.agent')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
