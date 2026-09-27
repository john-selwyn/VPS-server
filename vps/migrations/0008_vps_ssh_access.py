# Generated for per-VPS SSH access metadata
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vps", "0007_unique_allocated_vps_ip"),
    ]

    operations = [
        migrations.AddField(
            model_name="vps",
            name="ssh_public_key",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="vps",
            name="ssh_username",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
