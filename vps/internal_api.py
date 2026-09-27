"""Authenticated billing integration; provisioning remains owned by views/worker."""
import base64
import binascii
import json
import logging
import re
import uuid
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import VPS, PowerOperation
from .proxmox import get_guest_ipv4, get_vm_state, resolve_vm_node
from .power import AdmissionError, admit_power
from .power_contract import error_response, json_response, operation_response
from .views import _create_and_start_clone

logger = logging.getLogger(__name__)


def authenticated(method, *, versioned=False):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            secret = settings.BILLING_API_SECRET
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            if not secret or scheme.lower() != "bearer" or not constant_time_compare(token, secret):
                response = error_response("UNAUTHORIZED", 401) if versioned else JsonResponse({"error": "Unauthorized"}, status=401)
                response["WWW-Authenticate"] = "Bearer"
                return response
            if request.method != method:
                response = error_response("METHOD_NOT_ALLOWED", 405) if versioned else JsonResponse({"error": "Method not allowed"}, status=405)
                response["Allow"] = method
                return response
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


SSH_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


def _validate_ssh_access(username, public_key):
    if not isinstance(username, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        raise ValueError("ssh_username must be a valid Linux username.")
    if username in {"root", "daemon", "nobody"}:
        raise ValueError("ssh_username is reserved.")
    if not isinstance(public_key, str):
        raise ValueError("ssh_public_key must be a string.")
    public_key = public_key.strip()
    if not public_key or len(public_key) > 4096 or "\n" in public_key or "\r" in public_key:
        raise ValueError("ssh_public_key must be one OpenSSH public key.")
    parts = public_key.split(None, 2)
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        raise ValueError("ssh_public_key uses an unsupported key type.")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("ssh_public_key is not valid base64.") from None
    if len(decoded) < 16 or len(decoded) > 2048:
        raise ValueError("ssh_public_key has an invalid key blob.")
    return username, public_key


def configuration(data):
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object.")
    required = {"order_id", "name", "cpu", "ram", "storage", "os", "billing_cycle", "plan"}
    optional = {"ssh_username", "ssh_public_key"}
    if required - data.keys():
        raise ValueError("Missing required fields: " + ", ".join(sorted(required - data.keys())))
    if data.keys() - required - optional:
        raise ValueError("Unknown fields are not accepted (including pricing fields).")
    if ("ssh_username" in data) != ("ssh_public_key" in data):
        raise ValueError("SSH username and public key must be supplied together.")
    for field in ("order_id", "cpu", "ram", "storage"):
        limit = 9223372036854775807 if field == "order_id" else 2147483647
        if type(data[field]) is not int or not 0 < data[field] <= limit:
            raise ValueError(f"{field} must be a positive integer no greater than {limit}.")
    for field, limit in (("name", 100), ("os", 100), ("billing_cycle", 20), ("plan", 50)):
        if not isinstance(data[field], str) or not data[field].strip() or len(data[field]) > limit:
            raise ValueError(f"{field} must be a nonempty string of at most {limit} characters.")
    result = {"name": data["name"].strip(), "cpu": data["cpu"], "ram": data["ram"],
              "storage": data["storage"], "os": data["os"].strip(),
              "billing": data["billing_cycle"].strip(), "plan": data["plan"].strip()}
    if "ssh_username" in data:
        result["ssh_username"], result["ssh_public_key"] = _validate_ssh_access(
            data["ssh_username"], data["ssh_public_key"]
        )
    return result


@csrf_exempt
@transaction.non_atomic_requests
@authenticated("POST")
def provision(request):
    try:
        data = json.loads(request.body)
        config = configuration(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JsonResponse({"error": "Invalid JSON or provisioning fields."}, status=400)

    # Commit the unique reservation BEFORE any external side effect. Only the
    # creator may submit a clone, even if it crashes or the caller times out.
    vps, created = VPS.objects.get_or_create(
        billing_order_id=data["order_id"],
        defaults={"name": config["name"], "cpu": config["cpu"], "ram": config["ram"],
                  "storage": config["storage"], "operating_system": config["os"],
                  "billing_cycle": config["billing"], "plan": config["plan"]},
    )
    if created:
        try:
            vps = _create_and_start_clone(config, vps.order_token, reserved_vps=vps)
        except Exception:
            logger.exception("Provisioning failed for VPS %s", vps.pk)
            VPS.objects.filter(pk=vps.pk).update(status="Failed", current_step="Provisioning failed.",
                error_message="Provisioning could not complete. Contact the provisioning operator.")
            vps.refresh_from_db()
    return JsonResponse({"success": vps.status != "Failed", "vps_id": vps.pk,
                         "vmid": vps.vmid, "status": vps.status}, status=202 if created else 200)


@authenticated("GET")
def status(request, billing_order_id):
    vps = VPS.objects.filter(billing_order_id=billing_order_id).first()
    if vps is None:
        return JsonResponse({"error": "Order not found"}, status=404)

    if vps.status == "Running" and vps.vmid and vps.ip_address == "0.0.0.0":
        address = get_guest_ipv4(vps.vmid)
        if address:
            VPS.objects.filter(pk=vps.pk, ip_address="0.0.0.0").update(ip_address=address)
            vps.ip_address = address

    return JsonResponse({"billing_order_id": vps.billing_order_id, "vps_id": vps.pk,
        "vmid": vps.vmid, "status": vps.status, "progress": vps.progress,
        "current_step": vps.current_step, "ip_address": vps.ip_address,
        "error_message": "Provisioning failed. Contact the provisioning operator." if vps.error_message else ""})


@csrf_exempt
@authenticated("GET", versioned=True)
def vps_state(request, billing_order_id):
    if not 0 < billing_order_id <= 9223372036854775807 or request.GET:
        return error_response("INVALID_REQUEST", 400)
    vps = VPS.objects.filter(billing_order_id=billing_order_id).first()
    if vps is None:
        return error_response("NOT_FOUND", 404, billing_order_id=billing_order_id)
    if not vps.vmid or vps.status != "Running":
        return error_response("VPS_NOT_READY", 409, billing_order_id=billing_order_id)

    try:
        node = resolve_vm_node(vps.vmid)
        state = get_vm_state(vps.vmid, node=node)
        if state not in PowerOperation.STATES:
            raise ValueError("Invalid runtime state.")
    except Exception:
        return error_response("RUNTIME_UNAVAILABLE", 502, billing_order_id=billing_order_id)

    return json_response({
        "version": 1,
        "billing_order_id": vps.billing_order_id,
        "state": state,
        "observed_at": timezone.now().isoformat(),
        "ip_address": vps.ip_address,
    })


@csrf_exempt
@transaction.non_atomic_requests
@authenticated("POST", versioned=True)
def vps_power(request, billing_order_id):
    if not 0 < billing_order_id <= 9223372036854775807 or request.GET or request.content_type != "application/json":
        return error_response("INVALID_REQUEST", 400)
    try:
        key_text = request.headers.get("Idempotency-Key", "")
        key = uuid.UUID(key_text)
        if str(key) != key_text.lower():
            raise ValueError("Noncanonical UUID.")
        data = json.loads(request.body, object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return error_response("INVALID_REQUEST", 400, billing_order_id=billing_order_id)
    if not isinstance(data, dict) or set(data) != {"action"}:
        return error_response("INVALID_REQUEST", 400, billing_order_id=billing_order_id)
    action = data.get("action")
    if not isinstance(action, str) or action not in PowerOperation.ACTIONS:
        return error_response("INVALID_REQUEST", 400, billing_order_id=billing_order_id)
    try:
        operation = admit_power(billing_order_id, action, key)
    except AdmissionError as exc:
        return error_response(exc.code, exc.status, billing_order_id=billing_order_id,
                              active_operation_id=exc.active_operation_id)
    return operation_response(operation)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


@csrf_exempt
@authenticated("GET", versioned=True)
def power_operation(request, billing_order_id, operation_id):
    if not 0 < billing_order_id <= 9223372036854775807 or request.GET:
        return error_response("INVALID_REQUEST", 400)
    operation = PowerOperation.objects.filter(billing_order_id=billing_order_id, pk=operation_id).first()
    if operation is None:
        return error_response("NOT_FOUND", 404, billing_order_id=billing_order_id)
    return operation_response(operation)
