from django.shortcuts import render, redirect
from .models import VPS

from .proxmox import clone_vps, proxmox, PROXMOX_NODE


def dashboard(request):
    vps_list = VPS.objects.all()

    return render(request, "vps/dashboard.html", {
        "vps_list": vps_list,
    })


def vps_list(request):
    vps = VPS.objects.all()

    return render(request, "vps/list.html", {
        "vps": vps,
    })


def order_vps(request):
    if request.method == "POST":

        name = request.POST.get("name")
        cpu = request.POST.get("cpu")
        ram = request.POST.get("ram")
        storage = request.POST.get("storage")
        os = request.POST.get("os")
        billing = request.POST.get("billing")

        # Payment confirmed
        if request.POST.get("payment") == "success":

            # Create VPS in Proxmox
            result = clone_vps(name)

            vmid = result["vmid"]

            # Remove installer ISO
            proxmox.nodes(PROXMOX_NODE).qemu(vmid).config.set(
                delete="ide2"
            )

            # Configure CPU and RAM
            proxmox.nodes(PROXMOX_NODE).qemu(vmid).config.set(
                cores=int(cpu),
                memory=int(ram) * 1024,
            )

            # Start VPS
            proxmox.nodes(PROXMOX_NODE).qemu(vmid).status.start.post()

            # Save VPS information
            VPS.objects.create(
                name=name,
                vmid=vmid,
                ip_address="0.0.0.0",
                status="Provisioning",
                cpu=int(cpu),
                ram=int(ram),
                storage=int(storage),
                operating_system=os,
                billing_cycle=billing,
                plan=f"{ram}GB VPS",
            )

            return redirect("dashboard")

        # Show billing page
        return render(request, "vps/billing.html", {
            "name": name,
            "cpu": cpu,
            "ram": ram,
            "storage": storage,
            "os": os,
            "billing": billing,
        })

    return render(request, "vps/order.html")