"""Reserve public ports and request narrowly scoped, idempotent Proxmox relays."""
import ipaddress
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import PublicSSHAccess, VPS


class PublicSSHError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Config:
    host: str
    start: int
    end: int
    relay: str
    key: str
    known_hosts: str

    @classmethod
    def load(cls):
        label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        try:
            host = settings.PUBLIC_SSH_HOST
            if (not isinstance(host, str) or len(host) > 253
                    or not re.fullmatch(rf"{label}(?:\.{label})*", host)):
                raise ValueError()
            start, end = int(settings.PUBLIC_SSH_PORT_START), int(settings.PUBLIC_SSH_PORT_END)
            if not 1024 <= start <= end <= 65535 or end - start >= 4096:
                raise ValueError()
            relay = str(ipaddress.IPv4Address(settings.PUBLIC_SSH_RELAY_HOST))
            key, known = settings.PUBLIC_SSH_RELAY_KEY, settings.PUBLIC_SSH_RELAY_KNOWN_HOSTS
            if not all(isinstance(p, str) and Path(p).is_absolute() and Path(p).is_file()
                       for p in (key, known)):
                raise ValueError()
            if Path(key).stat().st_mode & 0o077:
                raise ValueError()
        except (AttributeError, OSError, TypeError, ValueError):
            raise PublicSSHError("SSH_ACCESS_NOT_CONFIGURED") from None
        return cls(host, start, end, relay, key, known)


def _eligible(vps):
    try:
        ip = ipaddress.IPv4Address(vps.ip_address)
    except (ValueError, TypeError):
        raise PublicSSHError("SSH_ACCESS_NOT_READY") from None
    if (vps.status != "Running" or not vps.vmid or not vps.billing_order_id
            or (vps.operating_system == "Windows 11" and not vps.windows_password_encrypted)
            or (vps.operating_system != "Windows 11"
                and (not vps.ssh_username or not vps.ssh_public_key))
            or ip not in settings.VPS_IP_NETWORK
            or ip in {settings.VPS_IP_NETWORK.network_address,
                      settings.VPS_IP_NETWORK.broadcast_address, settings.VPS_IP_GATEWAY}):
        raise PublicSSHError("SSH_ACCESS_NOT_READY")


def reserve_access(vps_id, *, port=None):
    config = Config.load()
    if port is not None and (type(port) is not int or not config.start <= port <= config.end):
        raise PublicSSHError("SSH_PORT_OUT_OF_RANGE")
    # A uniqueness constraint protects simultaneous reservations by different VMs.
    for _ in range(8):
        try:
            with transaction.atomic():
                vps = VPS.objects.select_for_update().get(pk=vps_id)
                _eligible(vps)
                existing = PublicSSHAccess.objects.filter(vps=vps).first()
                if existing:
                    if port is not None and existing.port != port:
                        raise PublicSSHError("SSH_PORT_ALREADY_ASSIGNED")
                    return existing
                used = set(PublicSSHAccess.objects.values_list("port", flat=True))
                selected = port if port is not None else next(
                    (p for p in range(config.start, config.end + 1) if p not in used), None
                )
                if selected is None:
                    raise PublicSSHError("SSH_PORT_POOL_EXHAUSTED")
                if selected in used:
                    raise PublicSSHError("SSH_PORT_IN_USE")
                return PublicSSHAccess.objects.create(vps=vps, host=config.host, port=selected)
        except IntegrityError:
            continue
    raise PublicSSHError("SSH_RESERVATION_BUSY")


def _request_relay(access, config):
    vps = access.vps
    request = {"vmid": vps.vmid, "order_id": vps.billing_order_id,
               "ip": str(vps.ip_address), "port": access.port,
               "username": "Admin" if vps.operating_system == "Windows 11" else vps.ssh_username}
    if vps.operating_system == "Windows 11":
        request["protocol"] = "rdp"
    argv = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-i", config.key,
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={config.known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
            "-o", "ConnectionAttempts=1", "-o", "ClearAllForwardings=yes",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            f"root@{config.relay}", "vps-ssh-relay"]
    try:
        result = subprocess.run(argv, input=json.dumps(request), capture_output=True,
                                text=True, timeout=45, check=False)
        if result.returncode or len(result.stdout) > 8192:
            raise PublicSSHError("SSH_RELAY_FAILED")
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("ready") is not True or response != {**request, "ready": True}:
            raise PublicSSHError("SSH_RELAY_INVALID_RESPONSE")
    except subprocess.TimeoutExpired:
        raise PublicSSHError("SSH_RELAY_TIMEOUT") from None
    except (OSError, ValueError, TypeError):
        raise PublicSSHError("SSH_RELAY_INVALID_RESPONSE") from None


def sync_access(vps_id):
    if not settings.PUBLIC_SSH_ENABLED:
        return None
    config = Config.load()
    access = reserve_access(vps_id)
    now, lease = timezone.now(), uuid.uuid4()
    if access.status == "ready" or (access.retry_at and access.retry_at > now):
        return access
    available = Q(lease__isnull=True) | Q(claimed_at__lte=now - timedelta(minutes=2))
    if not PublicSSHAccess.objects.filter(pk=access.pk).filter(available).update(
            lease=lease, claimed_at=now, status="pending", updated_at=now):
        return access
    error = ""
    try:
        _request_relay(access, config)
    except PublicSSHError as exc:
        error = exc.code
    PublicSSHAccess.objects.filter(pk=access.pk, lease=lease).update(
        status="failed" if error else "ready", error_code=error, lease=None,
        retry_at=timezone.now() + timedelta(seconds=60) if error else None,
        updated_at=timezone.now(),
    )
    access.refresh_from_db()
    if error:
        raise PublicSSHError(error)
    return access


def endpoint_fields(vps):
    if not settings.PUBLIC_SSH_ENABLED:
        return {}  # Backwards-compatible with deployed manually assigned endpoints.
    access = PublicSSHAccess.objects.filter(vps=vps, vps__status="Running", status="ready").first()
    if vps.operating_system == "Windows 11":
        return {"ssh_access": None, "rdp_access":
            {"host": access.host, "port": access.port, "username": "Admin"} if access else None}
    return {"ssh_access": {"host": access.host, "port": access.port} if access else None}
