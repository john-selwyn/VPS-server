import re

from django.core.management.base import BaseCommand, CommandError

from vps.models import VPS
from vps.proxmox import PROXMOX_NODE, get_task_progress, proxmox, wait_for_guest_ipv4, wait_for_task


class Command(BaseCommand):
    help = "Complete configuration for an already-submitted asynchronous VPS clone."

    def add_arguments(self, parser):
        parser.add_argument("vps_id", type=int)

    @staticmethod
    def _disk_size_gb(value):
        for item in value.split(","):
            if item.startswith("size=") and item[5:].lower().endswith("g"):
                return int(float(item[5:-1]))
        return 0

    def handle(self, *args, **options):
        try:
            vps = VPS.objects.get(pk=options["vps_id"])
        except VPS.DoesNotExist as exc:
            raise CommandError("VPS does not exist.") from exc
        if vps.status in ("Running", "Failed"):
            return

        def update(progress, step):
            VPS.objects.filter(pk=vps.pk, status="Provisioning").update(
                progress=max(0, min(100, int(progress))), current_step=step, progress_message=step
            )

        try:
            if not vps.task_upid:
                raise RuntimeError("The clone task UPID was not recorded.")
            update(5, "Cloning template")

            def clone_progress(_percent, _message):
                # Proxmox task-log percentages are the only clone percentages shown.
                actual = get_task_progress(vps.task_upid)
                if actual is None:
                    update(5, "Cloning template")
                else:
                    update(min(70, max(5, actual)), f"Cloning template ({actual:.0f}%)")

            wait_for_task(vps.task_upid, progress_callback=clone_progress)
            vm = proxmox.nodes(PROXMOX_NODE).qemu(vps.vmid)

            update(75, "Removing installation media")
            config = vm.config.get()
            if config.get("ide2"):
                vm.config.set(delete="ide2")

            update(82, "Configuring CPU, RAM and guest agent")
            vm.config.set(cores=vps.cpu, memory=vps.ram * 1024, agent=1)

            update(89, "Configuring storage")
            config = vm.config.get()
            # `scsihw` describes the controller, not a disk. Proxmox's resize
            # API accepts only numbered disk slots (for example, scsi0).
            disk_name = next(
                (
                    key for key in config
                    if re.fullmatch(r"(?:scsi|virtio|sata)\d+", key)
                ),
                None,
            )
            if not disk_name:
                raise RuntimeError("No virtual disk was found on the cloned VM.")
            current_size = self._disk_size_gb(config[disk_name])
            if vps.storage > current_size:
                resize_task = vm.resize.set(disk=disk_name, size=f"+{vps.storage - current_size}G")
                if resize_task:
                    wait_for_task(resize_task)

            update(96, "Starting VPS")
            if vm.status.current.get().get("status") != "running":
                start_task = vm.status.start.post()
                if start_task:
                    wait_for_task(start_task)

            update(98, "Obtaining IP address")
            ip_address = wait_for_guest_ipv4(vps.vmid)
            ready_message = "VPS is ready." if ip_address else "VPS is ready. IP address is still being assigned."
            fields = {
                "status": "Running",
                "progress": 100,
                "current_step": ready_message,
                "progress_message": ready_message,
                "error_message": "",
            }
            if ip_address:
                fields["ip_address"] = ip_address
            VPS.objects.filter(pk=vps.pk).update(**fields)
        except Exception as exc:
            VPS.objects.filter(pk=vps.pk).update(status="Failed", current_step="Provisioning failed.",
                progress_message="Provisioning failed.", error_message=str(exc))
            raise CommandError(str(exc)) from exc
