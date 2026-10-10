import re
from urllib.parse import quote

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from vps.models import VPS
from vps.windows_credentials import decrypt_windows_password
from vps.public_ssh import PublicSSHError, sync_access
from vps.proxmox import (
    PROXMOX_NODE,
    get_task_progress,
    proxmox,
    resolve_vm_node,
    wait_for_guest_ipv4_match,
    wait_for_task,
)


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

            update(80, "Configuring CPU, RAM and guest agent")
            vm.config.set(cores=vps.cpu, memory=vps.ram * 1024, agent=1)

            if vps.operating_system == "Windows 11":
                update(82, "Configuring Windows access")
                if config.get("ostype") != "win11":
                    raise RuntimeError("Windows template has an unexpected OS type.")
                password = decrypt_windows_password(vps.windows_password_encrypted)
                try:
                    vm.config.set(ciuser="Admin", cipassword=password)
                except Exception:
                    raise RuntimeError("Windows access configuration failed.") from None
                finally:
                    del password

            if bool(vps.ssh_username) != bool(vps.ssh_public_key):
                raise RuntimeError("SSH access configuration is incomplete.")
            if vps.operating_system != "Windows 11" and vps.ssh_username and vps.ssh_public_key:
                update(83, "Configuring SSH access")
                vm.config.set(
                    ciuser=vps.ssh_username,
                    sshkeys=quote(vps.ssh_public_key.strip(), safe=""),
                )

            if not vps.ip_address or str(vps.ip_address) == "0.0.0.0":
                raise RuntimeError("No static IP address was reserved for this VPS.")

            update(84, "Attaching customer network")
            config = vm.config.get()
            net0 = config.get("net0")
            if not isinstance(net0, str) or not net0.strip():
                raise RuntimeError("The cloned VPS has no usable primary network interface.")

            net0_parts = [part.strip() for part in net0.split(",") if part.strip()]
            bridge_found = False
            for index, part in enumerate(net0_parts):
                if part.startswith("bridge="):
                    net0_parts[index] = f"bridge={settings.VPS_BRIDGE}"
                    bridge_found = True
                    break

            if not bridge_found:
                net0_parts.append(f"bridge={settings.VPS_BRIDGE}")

            vm.config.set(net0=",".join(net0_parts))

            update(85, "Configuring static network")
            vm.config.set(
                ipconfig0=f"ip={vps.ip_address}/{settings.VPS_IP_PREFIX},gw={settings.VPS_IP_GATEWAY}",
                nameserver=str(settings.VPS_IP_DNS),
            )

            update(90, "Configuring storage")
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

            update(98, "Verifying guest network")
            node = resolve_vm_node(vps.vmid)
            if not wait_for_guest_ipv4_match(
                vps.vmid,
                str(vps.ip_address),
                timeout=600 if vm.config.get().get("ostype") == "win11" else 120,
                interval=3,
                node=node,
            ):
                raise RuntimeError("Guest network verification failed.")

            ready_message = "VPS is ready."
            VPS.objects.filter(pk=vps.pk).update(
                status="Running",
                progress=100,
                current_step=ready_message,
                progress_message=ready_message,
                error_message="",
            )
        except Exception as exc:
            VPS.objects.filter(pk=vps.pk).update(status="Failed", current_step="Provisioning failed.",
                progress_message="Provisioning failed.", error_message=str(exc))
            raise CommandError(str(exc)) from exc

        # Access setup has its own retries; it must never turn a running VM or a
        # verified payment into a failed provision after the VM is already ready.
        if settings.PUBLIC_SSH_ENABLED:
            try:
                sync_access(vps.pk)
            except PublicSSHError as exc:
                self.stderr.write(f"Public SSH setup will retry: {exc.code}")
