import subprocess
import sys
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .models import VPS
from .networking import next_available_ip
from .proxmox import clone_vps, get_next_vmid


def dashboard(request):
    return render(request, "vps/dashboard.html", {"vps_list": VPS.objects.all().order_by("-created_at")})


def _pending_orders(request):
    return request.session.setdefault("pending_vps_orders", {})


def _configuration_from_request(request):
    values = {key: request.POST.get(key, "").strip() for key in ("name", "cpu", "ram", "storage", "os", "billing")}
    if not all(values.values()):
        raise ValueError("Please complete all VPS configuration fields.")
    try:
        values["cpu"] = int(values["cpu"])
        values["ram"] = int(values["ram"])
        values["storage"] = int(values["storage"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid VPS configuration.") from exc
    if values["cpu"] <= 0 or values["ram"] <= 0 or values["storage"] <= 0:
        raise ValueError("VPS resources must be positive values.")
    return values


def _start_worker(vps_id):
    command = [sys.executable, str(settings.BASE_DIR / "manage.py"), "provision_vps", str(vps_id)]
    subprocess.Popen(command, cwd=settings.BASE_DIR, close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _create_and_start_clone(configuration, order_token, *, reserved_vps=None):
    """Durably reserve VMID/IP first, then submit one Proxmox clone request."""
    vps = None
    for _ in range(3):
        try:
            with transaction.atomic():
                reserved_vmids = VPS.objects.exclude(vmid__isnull=True).values_list("vmid", flat=True)
                vmid = get_next_vmid(reserved_vmids)

                if reserved_vps is None:
                    reserved_ips = VPS.objects.exclude(ip_address="0.0.0.0").values_list("ip_address", flat=True)
                    assigned_ip = next_available_ip(reserved_ips)
                else:
                    vps = VPS.objects.select_for_update().get(pk=reserved_vps.pk)
                    if str(vps.ip_address) != "0.0.0.0":
                        assigned_ip = str(vps.ip_address)
                    else:
                        reserved_ips = VPS.objects.exclude(ip_address="0.0.0.0").values_list("ip_address", flat=True)
                        assigned_ip = next_available_ip(reserved_ips)

                fields = dict(
                    name=configuration["name"], vmid=vmid, ip_address=assigned_ip,
                    status="Provisioning", progress=1,
                    current_step="Preparing VPS", progress_message="Preparing VPS",
                    cpu=configuration["cpu"], ram=configuration["ram"], storage=configuration["storage"],
                    operating_system=configuration["os"], billing_cycle=configuration["billing"],
                    plan=configuration.get("plan", f'{configuration["ram"]}GB VPS'), order_token=order_token,
                )

                if reserved_vps is None:
                    vps = VPS.objects.create(**fields)
                else:
                    for field, value in fields.items():
                        setattr(vps, field, value)
                    vps.save(update_fields=list(fields))
            break
        except IntegrityError:
            winner = VPS.objects.filter(order_token=order_token).first() if reserved_vps is None else None
            if winner:
                return winner
            vps = None
            continue

    if vps is None:
        raise RuntimeError("Could not reserve an available VMID/IP; please try again.")

    # The VMID and customer IP are committed before any external side effect.
    try:
        result = clone_vps(vps.name, vps.vmid)
    except Exception as exc:
        VPS.objects.filter(pk=vps.pk).update(
            status="Failed",
            current_step="Provisioning failed.",
            progress_message="Provisioning failed.",
            error_message=str(exc),
        )
        vps.refresh_from_db()
        return vps

    VPS.objects.filter(pk=vps.pk).update(
        task_upid=result["task"],
        progress=5,
        current_step="Cloning template",
        progress_message="Cloning template",
    )
    _start_worker(vps.id)
    vps.refresh_from_db()
    return vps


def order_vps(request):
    if request.method != "POST":
        return render(request, "vps/order.html")
    if request.POST.get("payment") != "success":
        try:
            configuration = _configuration_from_request(request)
        except ValueError as exc:
            return render(request, "vps/order.html", {"error": str(exc)})
        order_token = str(uuid.uuid4())
        pending = _pending_orders(request)
        pending[order_token] = configuration
        request.session.modified = True
        return render(request, "vps/billing.html", {**configuration, "order_token": order_token})

    order_token = request.POST.get("order_token", "")
    try:
        token_uuid = uuid.UUID(order_token)
    except (ValueError, TypeError):
        return render(request, "vps/order.html", {"error": "Your payment session is invalid. Please place the order again."})
    existing = VPS.objects.filter(order_token=token_uuid).first()
    if existing:
        return redirect("provisioning", vps_id=existing.id)
    configuration = _pending_orders(request).get(order_token)
    if not configuration:
        return render(request, "vps/order.html", {"error": "Your payment session has expired. Please place the order again."})
    try:
        vps = _create_and_start_clone(configuration, token_uuid)
    except Exception as exc:
        return render(request, "vps/billing.html", {**configuration, "order_token": order_token, "error": str(exc)})
    _pending_orders(request).pop(order_token, None)
    request.session.modified = True
    return redirect("provisioning", vps_id=vps.id)


def provisioning(request, vps_id):
    return render(request, "vps/provisioning.html", {"vps": get_object_or_404(VPS, id=vps_id)})


def provisioning_status(request, vps_id):
    """Read-only: the background worker owns all Proxmox mutations."""
    vps = get_object_or_404(VPS, id=vps_id)
    failed = vps.status == "Failed"
    ready = vps.status == "Running"
    return JsonResponse({"status": "failed" if failed else "completed" if ready else "processing",
                         "progress": vps.progress, "message": vps.current_step or vps.progress_message,
                         "current_step": vps.current_step, "vmid": vps.vmid,
                         "error": vps.error_message or "", "failed": failed, "ready": ready})
