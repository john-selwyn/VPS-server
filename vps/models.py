import uuid

from django.db import models


class VPS(models.Model):
    billing_order_id = models.PositiveBigIntegerField(unique=True, null=True, blank=True)
    name = models.CharField(max_length=100)
    vmid = models.IntegerField(unique=True, null=True, blank=True)
    ip_address = models.GenericIPAddressField(default="0.0.0.0")
    status = models.CharField(max_length=20, default="Provisioning")
    cpu = models.IntegerField(default=2)
    ram = models.IntegerField(default=2)
    storage = models.IntegerField(default=32)
    operating_system = models.CharField(max_length=100, default="Ubuntu 26.04")
    billing_cycle = models.CharField(max_length=20, default="Monthly")
    plan = models.CharField(max_length=50, default="Basic")

    # Provisioning progress
    progress = models.PositiveIntegerField(default=0)
    current_step = models.CharField(
        max_length=255,
        default="Preparing VPS",
    )
    progress_message = models.CharField(
        max_length=255,
        default="Waiting to start provisioning...",
    )
    task_upid = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    # This token makes a payment confirmation idempotent if a browser retries it.
    order_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ip_address"],
                condition=~models.Q(ip_address="0.0.0.0"),
                name="unique_allocated_vps_ip",
            ),
        ]

    def __str__(self):
        return self.name


class PowerOperation(models.Model):
    """Durable command reservation. Never delete keys to make a retry executable."""

    ACTIONS = ("start", "shutdown", "reboot")
    STATUSES = ("pending", "running", "succeeded", "failed", "unknown")
    UNRESOLVED = ("pending", "running", "unknown")
    STATES = ("running", "stopped", "paused", "suspended", "unknown")

    operation_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    vps = models.ForeignKey(VPS, on_delete=models.PROTECT, related_name="power_operations")
    # Customer-independent targets resolved by VM100, never supplied by the caller.
    billing_order_id = models.PositiveBigIntegerField()
    target_vmid = models.PositiveIntegerField()
    target_node = models.CharField(max_length=255)
    action = models.CharField(max_length=8, choices=[(x, x) for x in ACTIONS])
    status = models.CharField(max_length=9, default="pending", choices=[(x, x) for x in STATUSES])
    result = models.CharField(max_length=8, null=True, default=None, choices=[("executed", "executed"), ("noop", "noop")])
    observed_state = models.CharField(max_length=9, default="unknown", choices=[(x, x) for x in STATES])
    observed_at = models.DateTimeField(null=True)
    task_upid = models.TextField(blank=True, default="")
    error_code = models.CharField(max_length=40, blank=True, default="")
    claimed_at = models.DateTimeField(null=True)
    submission_started_at = models.DateTimeField(null=True)
    poll_claimed_at = models.DateTimeField(null=True)
    next_poll_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "updated_at"], name="power_work_idx"),
            models.Index(fields=["status", "next_poll_at"], name="power_poll_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["vps"], condition=models.Q(status__in=("pending", "running", "unknown")), name="one_unresolved_power_per_vps"),
            models.CheckConstraint(condition=models.Q(action__in=("start", "shutdown", "reboot")), name="power_valid_action"),
            models.CheckConstraint(condition=models.Q(status__in=("pending", "running", "succeeded", "failed", "unknown")), name="power_valid_status"),
            models.CheckConstraint(condition=models.Q(observed_state__in=("running", "stopped", "paused", "suspended", "unknown")), name="power_valid_state"),
            models.CheckConstraint(condition=(models.Q(status="succeeded", result__isnull=False, result__in=("executed", "noop")) | (~models.Q(status="succeeded") & models.Q(result__isnull=True))), name="power_result_matches_status"),
        ]
