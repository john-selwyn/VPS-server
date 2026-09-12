from django.db import models


class VPS(models.Model):
    name = models.CharField(max_length=100)
    ip_address = models.GenericIPAddressField()
    status = models.CharField(max_length=20, default="Running")
    plan = models.CharField(max_length=50, default="Basic")

    def __str__(self):
        return self.name