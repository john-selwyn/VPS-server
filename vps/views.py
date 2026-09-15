from django.shortcuts import render, redirect
from django.contrib import messages

from .models import VPS
from .proxmox import clone_vps, proxmox, PROXMOX_NODE


def dashboard(request):

    vps_list = VPS.objects.all()

    return render(
        request,
        "vps/dashboard.html",
        {
            "vps_list": vps_list,
        },
    )


def vps_list(request):

    vps = VPS.objects.all()

    return render(
        request,
        "vps/list.html",
        {
            "vps": vps,
        },
    )


def order_vps(request):

    if request.method == "POST":

        name = request.POST.get("name")

        cpu = request.POST.get("cpu")
        ram = request.POST.get("ram")
        storage = request.POST.get("storage")

        os = request.POST.get("os")
        billing = request.POST.get("billing")

        # Validate required values
        if not all([
            name,
            cpu,
            ram,
            storage,
            os,
            billing,
        ]):

            return render(
                request,
                "vps/order.html",
                {
                    "error": "Please complete all VPS configuration fields."
                },
            )

        try:

            cpu = int(cpu)
            ram = int(ram)
            storage = int(storage)

        except ValueError:

            return render(
                request,
                "vps/order.html",
                {
                    "error": "Invalid VPS configuration."
                },
            )

        # Payment confirmed
        if request.POST.get("payment") == "success":

            try:

                # -------------------------------------------------
                # 1. Clone template
                # -------------------------------------------------

                result = clone_vps(name)

                vmid = result["vmid"]

                vps = proxmox.nodes(
                    PROXMOX_NODE
                ).qemu(vmid)

                # -------------------------------------------------
                # 2. Remove installer ISO
                # -------------------------------------------------

                config = vps.config.get()

                if "ide2" in config:

                    vps.config.set(
                        delete="ide2"
                    )

                # -------------------------------------------------
                # 3. Configure CPU and RAM
                # -------------------------------------------------

                vps.config.set(
                    cores=cpu,
                    memory=ram * 1024,
                )

                # -------------------------------------------------
                # 4. Find VPS disk
                # -------------------------------------------------

                config = vps.config.get()

                disk_name = None

                for key in config:

                    if key.startswith(
                        ("scsi", "virtio", "sata")
                    ):

                        disk_name = key

                        break

                # -------------------------------------------------
                # 5. Resize disk
                # -------------------------------------------------

                if disk_name:

                    current_disk = config[disk_name]

                    current_size = 32

                    if "size=" in current_disk:

                        size_text = current_disk.split(
                            "size="
                        )[1]

                        size_text = size_text.split(
                            ","
                        )[0]

                        if size_text.lower().endswith("g"):

                            current_size = int(
                                float(
                                    size_text[:-1]
                                )
                            )

                    # Only expand disks.
                    # Never shrink a disk.
                    if storage > current_size:

                        additional_storage = (
                            storage - current_size
                        )

                        vps.resize.set(
                            disk=disk_name,
                            size=f"+{additional_storage}G",
                        )

                # -------------------------------------------------
                # 6. Start VPS
                # -------------------------------------------------

                vps.status.start.post()

                # -------------------------------------------------
                # 7. Save VPS to PostgreSQL
                # -------------------------------------------------

                VPS.objects.create(

                    name=name,

                    vmid=vmid,

                    ip_address="0.0.0.0",

                    status="Provisioning",

                    cpu=cpu,

                    ram=ram,

                    storage=storage,

                    operating_system=os,

                    billing_cycle=billing,

                    plan=f"{ram}GB VPS",
                )

                return redirect("dashboard")

            except Exception as e:

                # If provisioning fails,
                # don't silently fail.

                return render(
                    request,
                    "vps/billing.html",
                    {
                        "name": name,
                        "cpu": cpu,
                        "ram": ram,
                        "storage": storage,
                        "os": os,
                        "billing": billing,
                        "error": str(e),
                    },
                )

        # ---------------------------------------------------------
        # Payment page
        # ---------------------------------------------------------

        return render(
            request,
            "vps/billing.html",
            {
                "name": name,
                "cpu": cpu,
                "ram": ram,
                "storage": storage,
                "os": os,
                "billing": billing,
            },
        )

    return render(
        request,
        "vps/order.html",
    )