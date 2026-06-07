import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("controlplane", "0003_governance_feedback_sessions"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="model_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Override the adapter's default model (e.g. gpt-4o, claude-opus-4-8)",
                max_length=80,
            ),
        ),
        migrations.AlterField(
            model_name="agent",
            name="platform",
            field=models.CharField(
                choices=[
                    ("django_runtime", "Django Runtime"),
                    ("azure_ai_foundry", "Azure AI Foundry / Azure OpenAI"),
                    ("copilot_studio", "Microsoft Copilot Studio"),
                    ("bedrock", "AWS Bedrock"),
                    ("custom_api", "Custom API Agent"),
                    ("vendor", "Vendor Platform"),
                    ("embedded", "Internal App Embed"),
                ],
                max_length=40,
            ),
        ),
    ]
