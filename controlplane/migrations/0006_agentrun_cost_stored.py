from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("controlplane", "0005_org_hierarchy"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentrun",
            name="cost_usd",
            field=models.DecimalField(
                decimal_places=6,
                default=0,
                max_digits=12,
                help_text="Stored at run completion via pricing.price_run()",
            ),
        ),
    ]
