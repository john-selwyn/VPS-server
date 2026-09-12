from django.db import models


class VPS(models.Model):
    name = models.CharField(max_length=100)

    vmid = models.IntegerField(unique=True, null=True, blank=True)

    ip_address = models.GenericIPAddressField(
        default="0.0.0.0"
    )

    status = models.CharField(
        max_length=20,
        default="Provisioning"
    )

    cpu = models.IntegerField(default=2)

    ram = models.IntegerField(default=2)

    storage = models.IntegerField(default=32)

    operating_system = models.CharField(
        max_length=100,
        default="Ubuntu 26.04"
    )

    billing_cycle = models.CharField(
        max_length=20,
        default="Monthly"
    )

    plan = models.CharField(
        max_length=50,
        default="Basic"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.name