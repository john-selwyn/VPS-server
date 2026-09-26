"""Database-backed power admission and recoverable, at-most-once submission."""
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import PowerOperation, VPS
from .proxmox import (
    PROXMOX_NODE,
    get_power_task_status,
    get_vm_state,
    resolve_vm_node,
    submit_power_action,
)


POLL_CLAIM_TTL = timedelta(seconds=60)
POLL_RUNNING_DELAY = timedelta(seconds=2)
POLL_RETRY_DELAY = timedelta(seconds=15)
POLL_STATE_DELAY = timedelta(seconds=10)


class AdmissionError(Exception):
    def __init__(self, code, status, active_operation_id=None):
        self.code = code
        self.status = status
        self.active_operation_id = active_operation_id
        super().__init__(code)


def _replay(operation, billing_order_id, action):
    if operation.billing_order_id != billing_order_id or operation.action != action:
        raise AdmissionError("IDEMPOTENCY_CONFLICT", 409)
    return operation


def admit_power(billing_order_id, action, idempotency_key):
    """Only local DB work here. Global key uniqueness covers cross-VPS races."""
    try:
        with transaction.atomic():
            vps = VPS.objects.select_for_update().filter(billing_order_id=billing_order_id).first()
            existing = PowerOperation.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                return _replay(existing, billing_order_id, action)
            if vps is None:
                raise AdmissionError("NOT_FOUND", 404)
            active = PowerOperation.objects.filter(vps=vps, status__in=PowerOperation.UNRESOLVED).first()
            if active:
                raise AdmissionError("OPERATION_IN_PROGRESS", 409, active.pk)
            if vps.status != "Running" or not vps.vmid or vps.vmid < 100:
                raise AdmissionError("VPS_NOT_READY", 409)
            return PowerOperation.objects.create(
                idempotency_key=idempotency_key,
                vps=vps,
                billing_order_id=billing_order_id,
                target_vmid=vps.vmid,
                # This is only an admission-time placeholder. The worker resolves
                # the VM's current Proxmox node before any power side effect.
                target_node=PROXMOX_NODE,
                action=action,
            )
    except IntegrityError:
        # A concurrent request for another VPS can win the global key constraint.
        existing = PowerOperation.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return _replay(existing, billing_order_id, action)
        active = PowerOperation.objects.filter(
            vps__billing_order_id=billing_order_id,
            status__in=PowerOperation.UNRESOLVED,
        ).first()
        if active:
            raise AdmissionError("OPERATION_IN_PROGRESS", 409, active.pk)
        raise


def _update(query, **fields):
    return query.update(updated_at=timezone.now(), **fields)


def recover_interrupted_submissions():
    """Timeout is permission to quarantine, NEVER permission to submit again."""
    cutoff = timezone.now() - timedelta(minutes=2)
    _update(
        PowerOperation.objects.filter(status="running", task_upid="", updated_at__lt=cutoff),
        status="unknown",
        error_code="SUBMISSION_UNKNOWN",
    )


def execute_pending(operation_id):
    if connection.in_atomic_block:
        raise RuntimeError("Power worker requires autocommit.")

    query = PowerOperation.objects.filter(pk=operation_id)

    # A single conditional UPDATE gives exactly one worker submission ownership.
    if not _update(query.filter(status="pending"), status="running", claimed_at=timezone.now()):
        return

    operation = query.select_related("vps").get()
    owned = query.filter(status="running", task_upid="", submission_started_at__isnull=True)
    vps = operation.vps

    if (
        vps.vmid != operation.target_vmid
        or vps.billing_order_id != operation.billing_order_id
        or vps.status != "Running"
    ):
        _update(owned, status="failed", error_code="VPS_NOT_READY")
        return

    # A VM may have migrated since admission. Resolve the current node before
    # observing or mutating it, and persist that node before the side effect.
    try:
        node = resolve_vm_node(operation.target_vmid)
    except Exception:
        _update(owned, status="failed", error_code="RUNTIME_UNAVAILABLE")
        return

    if not _update(owned, target_node=node):
        return
    operation.target_node = node

    try:
        state = get_vm_state(operation.target_vmid, node=node)
        if state not in PowerOperation.STATES:
            raise ValueError("Invalid runtime state.")
    except Exception:
        _update(owned, status="failed", error_code="RUNTIME_UNAVAILABLE")
        return

    observation = {"observed_state": state, "observed_at": timezone.now()}
    if state not in ("running", "stopped") or (state == "stopped" and operation.action == "reboot"):
        _update(owned, status="failed", error_code="INVALID_STATE", **observation)
        return

    if (state, operation.action) in (("running", "start"), ("stopped", "shutdown")):
        _update(owned, status="succeeded", result="noop", **observation)
        return

    # Commit the submission marker BEFORE the external side effect. Recovery may
    # fence a slow checker; its conditional update must then prevent submission.
    if not _update(owned, submission_started_at=timezone.now(), **observation):
        return

    submitted = query.filter(
        status__in=("running", "unknown"),
        task_upid="",
        submission_started_at__isnull=False,
    )
    try:
        upid = submit_power_action(operation.target_vmid, operation.action, node=node)
    except Exception:
        # Even an upstream error might have passed through a proxy after execution.
        _update(submitted, status="unknown", error_code="SUBMISSION_UNKNOWN")
        return

    # A delayed original worker may attach its UPID after recovery quarantines it.
    # This allows observation of the SAME task, never another submission.
    _update(
        submitted,
        status="running",
        task_upid=upid,
        error_code="",
        poll_claimed_at=None,
        next_poll_at=timezone.now(),
    )


def _claim_poll(operation_id):
    """Lease one task poll so multiple workers do not hammer Proxmox."""
    now = timezone.now()
    stale = now - POLL_CLAIM_TTL
    query = (
        PowerOperation.objects.filter(
            pk=operation_id,
            status__in=("running", "unknown"),
        )
        .exclude(task_upid="")
        .exclude(error_code="TASK_FAILED")
        .filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
        .filter(Q(poll_claimed_at__isnull=True) | Q(poll_claimed_at__lte=stale))
    )
    if not _update(query, poll_claimed_at=now):
        return None
    return PowerOperation.objects.get(pk=operation_id)


def poll_operation(operation_id):
    if connection.in_atomic_block:
        raise RuntimeError("Power worker requires autocommit.")

    operation = _claim_poll(operation_id)
    if operation is None:
        return

    query = PowerOperation.objects.filter(
        pk=operation_id,
        status__in=("running", "unknown"),
        task_upid=operation.task_upid,
    )
    now = timezone.now()

    try:
        task_status = get_power_task_status(operation.task_upid, node=operation.target_node)
        if task_status not in ("running", "succeeded", "failed"):
            raise ValueError("Invalid task state.")
    except Exception:
        _update(
            query,
            status="unknown",
            error_code="TASK_STATUS_UNAVAILABLE",
            poll_claimed_at=None,
            next_poll_at=now + POLL_RETRY_DELAY,
        )
        return

    if task_status == "running":
        _update(
            query,
            status="running",
            error_code="",
            poll_claimed_at=None,
            next_poll_at=now + POLL_RUNNING_DELAY,
        )
        return

    if task_status == "failed":
        # Proxmox may report a task failure/timeout after a side effect was
        # already requested. Keep the reservation unresolved until an operator
        # reconciles the real outcome; never make a replacement command eligible.
        _update(
            query,
            status="unknown",
            error_code="TASK_FAILED",
            poll_claimed_at=None,
            next_poll_at=None,
        )
        return

    # The task worker lives on target_node, but the VM itself may have migrated
    # by the time a successful task is verified. Resolve the current VM node
    # again before checking the expected runtime state.
    try:
        current_node = resolve_vm_node(operation.target_vmid)
        state = get_vm_state(operation.target_vmid, node=current_node)
        if state not in PowerOperation.STATES:
            raise ValueError("Invalid runtime state.")
    except Exception:
        _update(
            query,
            status="unknown",
            error_code="STATE_UNCONFIRMED",
            poll_claimed_at=None,
            next_poll_at=now + POLL_STATE_DELAY,
        )
        return

    expected = "stopped" if operation.action == "shutdown" else "running"
    if state != expected:
        _update(
            query,
            status="unknown",
            error_code="STATE_UNCONFIRMED",
            observed_state=state,
            observed_at=timezone.now(),
            poll_claimed_at=None,
            next_poll_at=now + POLL_STATE_DELAY,
        )
        return

    _update(
        query,
        status="succeeded",
        result="executed",
        error_code="",
        observed_state=state,
        observed_at=timezone.now(),
        poll_claimed_at=None,
        next_poll_at=None,
    )


def work_once(batch_size=100):
    recover_interrupted_submissions()

    # Oldest created first so an unresponsive task cannot starve later work.
    pending = list(
        PowerOperation.objects.filter(status="pending")
        .order_by("created_at")
        .values_list("pk", flat=True)[:batch_size]
    )
    for operation_id in pending:
        execute_pending(operation_id)

    now = timezone.now()
    stale = now - POLL_CLAIM_TTL
    tracking = list(
        PowerOperation.objects.filter(status__in=("running", "unknown"))
        .exclude(task_upid="")
        .exclude(error_code="TASK_FAILED")
        .filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
        .filter(Q(poll_claimed_at__isnull=True) | Q(poll_claimed_at__lte=stale))
        .order_by("next_poll_at", "updated_at")
        .values_list("pk", flat=True)[:batch_size]
    )
    for operation_id in tracking:
        poll_operation(operation_id)
