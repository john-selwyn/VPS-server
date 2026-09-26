# Generated for durable static customer VPS IP allocation
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vps", "0006_poweroperation"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="vps",
            constraint=models.UniqueConstraint(
                fields=("ip_address",),
                condition=~models.Q(ip_address="0.0.0.0"),
                name="unique_allocated_vps_ip",
            ),
        ),
    ]
