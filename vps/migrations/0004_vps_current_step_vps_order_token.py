import uuid

from django.db import migrations, models


def populate_order_tokens(apps, schema_editor):
    VPS = apps.get_model("vps", "VPS")
    for vps in VPS.objects.filter(order_token__isnull=True).iterator():
        vps.order_token = uuid.uuid4()
        vps.save(update_fields=["order_token"])


class Migration(migrations.Migration):
    dependencies = [("vps", "0003_vps_provisioning_fields")]

    operations = [
        migrations.AddField(model_name="vps", name="current_step", field=models.CharField(default="Preparing VPS", max_length=255)),
        # Add nullable first so each existing row can receive a distinct UUID.
        migrations.AddField(model_name="vps", name="order_token", field=models.UUIDField(blank=True, editable=False, null=True)),
        migrations.RunPython(populate_order_tokens, migrations.RunPython.noop),
        migrations.AlterField(model_name="vps", name="order_token", field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
    ]
