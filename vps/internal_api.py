"""Authenticated billing integration; provisioning remains owned by views/worker."""
import json
import logging
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt

from .models import VPS
from .proxmox import get_guest_ipv4, get_vm_state, perform_power_action
from .views import _create_and_start_clone

logger = logging.getLogger(__name__)


def authenticated(method):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            secret = settings.BILLING_API_SECRET
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            if not secret or scheme.lower() != "bearer" or not constant_time_compare(token, secret):
                response = JsonResponse({"error": "Unauthorized"}, status=401)
                response["WWW-Authenticate"] = "Bearer"
                return response
            if request.method != method:
                response = JsonResponse({"error": "Method not allowed"}, status=405)
                response["Allow"] = method
                return response
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


def configuration(data):
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object.")
    required = {"order_id", "name", "cpu", "ram", "storage", "os", "billing_cycle", "plan"}
    if required - data.keys():
        raise ValueError("Missing required fields: " + ", ".join(sorted(required - data.keys())))
    if data.keys() - required:
        raise ValueError("Unknown fields are not accepted (including pricing fields).")
    for field in ("order_id", "cpu", "ram", "storage"):
        limit = 9223372036854775807 if field == "order_id" else 2147483647
        if type(data[field]) is not int or not 0 < data[field] <= limit:
            raise ValueError(f"{field} must be a positive integer no greater than {limit}.")
    for field, limit in (("name", 100), ("os", 100), ("billing_cycle", 20), ("plan", 50)):
        if not isinstance(data[field], str) or not data[field].strip() or len(data[field]) > limit:
            raise ValueError(f"{field} must be a nonempty string of at most {limit} characters.")
    return {"name": data["name"].strip(), "cpu": data["cpu"], "ram": data["ram"],
            "storage": data["storage"], "os": data["os"].strip(),
            "billing": data["billing_cycle"].strip(), "plan": data["plan"].strip()}


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


@authenticated("GET")
def vps_state(request, billing_order_id):
    vps = VPS.objects.filter(billing_order_id=billing_order_id).first()
    if vps is None:
        return JsonResponse({"error": "Order not found"}, status=404)
    if not vps.vmid or vps.status != "Running":
        return JsonResponse({"error": "VPS not ready"}, status=409)

    try:
        state = get_vm_state(vps.vmid)
        if state == "running" and vps.ip_address == "0.0.0.0":
            address = get_guest_ipv4(vps.vmid)
            if address:
                VPS.objects.filter(pk=vps.pk, ip_address="0.0.0.0").update(ip_address=address)
                vps.ip_address = address
    except Exception:
        logger.exception("Runtime status failed for VPS %s", vps.pk)
        return JsonResponse({"error": "Runtime status unavailable"}, status=502)

    return JsonResponse({
        "billing_order_id": vps.billing_order_id,
        "vmid": vps.vmid,
        "state": state,
        "ip_address": vps.ip_address,
    })


@csrf_exempt
@transaction.non_atomic_requests
@authenticated("POST")
def vps_power(request, billing_order_id):
    vps = VPS.objects.filter(billing_order_id=billing_order_id).first()
    if vps is None:
        return JsonResponse({"error": "Order not found"}, status=404)
    if not vps.vmid or vps.status != "Running":
        return JsonResponse({"error": "VPS not ready"}, status=409)

    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JsonResponse({"error": "Invalid request"}, status=400)
    if not isinstance(data, dict) or set(data) != {"action"}:
        return JsonResponse({"error": "Invalid request"}, status=400)

    action = data.get("action")
    if action not in {"start", "shutdown", "reboot"}:
        return JsonResponse({"error": "Invalid power action"}, status=400)

    try:
        result = perform_power_action(vps.vmid, action)
    except RuntimeError:
        return JsonResponse({"error": "Power action conflicts with current state"}, status=409)
    except Exception:
        logger.exception("Power action failed for VPS %s", vps.pk)
        return JsonResponse({"error": "Power action failed"}, status=502)

    return JsonResponse({
        "accepted": True,
        "action": action,
        "vmid": vps.vmid,
        "state": result["state"],
        "changed": bool(result["changed"]),
    }, status=202 if result["changed"] else 200)
