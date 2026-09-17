from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vps", "0002_vps_billing_cycle_vps_cpu_vps_created_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="vps",
            name="error_message",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vps",
            name="progress",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="vps",
            name="progress_message",
            field=models.CharField(
                default="Waiting to start provisioning...",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="vps",
            name="task_upid",
            field=models.TextField(blank=True, null=True),
        ),
    ]
