import ipaddress
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI

# Environment always takes precedence; the local .env is only a development fallback.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROXMOX_HOST = os.getenv("PROXMOX_HOST")
PROXMOX_USER = os.getenv("PROXMOX_USER")
PROXMOX_TOKEN_NAME = os.getenv("PROXMOX_TOKEN_NAME")
PROXMOX_TOKEN_SECRET = os.getenv("PROXMOX_TOKEN_SECRET")

PROXMOX_NODE = "test-1"
VPS_TEMPLATE_VMID = 101

if not all((PROXMOX_HOST, PROXMOX_USER, PROXMOX_TOKEN_NAME, PROXMOX_TOKEN_SECRET)):
    raise RuntimeError("Proxmox credentials must be configured with environment variables.")

proxmox = ProxmoxAPI(
    PROXMOX_HOST, user=PROXMOX_USER, token_name=PROXMOX_TOKEN_NAME,
    token_value=PROXMOX_TOKEN_SECRET, verify_ssl=False, timeout=30,
)


def sanitize_vps_name(name):
    if not name:
        name = "vps"

    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name)
    name = name.strip("-")

    if not name:
        name = "vps"

    return name[:63]


def get_next_vmid(reserved_vmids=()):
    existing_vms = proxmox.nodes(PROXMOX_NODE).qemu.get()
    existing_vmids = {
        int(vm["vmid"])
        for vm in existing_vms
        if "vmid" in vm
    }
    existing_vmids.update(int(vmid) for vmid in reserved_vmids if vmid is not None)

    vmid = 1000
    while vmid in existing_vmids:
        vmid += 1

    return vmid


def _extract_progress_from_log(lines):
    """Return the latest 0-100 percentage found in a Proxmox task log."""
    if not lines:
        return None

    latest = None

    for entry in lines:
        if isinstance(entry, dict):
            text = str(entry.get("t", ""))
        else:
            text = str(entry)

        # Handles values such as '(34.21%)' or '34%'.
        matches = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)%", text)
        for match in matches:
            value = float(match)
            if 0 <= value <= 100:
                latest = value

    return latest


def get_task_progress(task):
    """Read the latest percentage from the Proxmox task log."""
    try:
        log = proxmox.nodes(PROXMOX_NODE).tasks(task).log.get()
        return _extract_progress_from_log(log)
    except Exception:
        # Progress is a UI enhancement. A temporary log-read failure should
        # never abort the actual provisioning task.
        return None



def get_guest_ipv4(vmid):
    """Return the best non-loopback IPv4 reported by QEMU Guest Agent."""
    try:
        data = proxmox.nodes(PROXMOX_NODE).qemu(vmid).agent("network-get-interfaces").get()
    except Exception:
        # Guest-agent availability is eventually consistent after boot and is
        # a UI/connection-detail enhancement, not a reason to fail the VPS.
        return None

    interfaces = data.get("result", []) if isinstance(data, dict) else data
    if not isinstance(interfaces, list):
        return None

    addresses = []
    for interface in interfaces:
        if not isinstance(interface, dict) or interface.get("name") == "lo":
            continue
        for item in interface.get("ip-addresses", []):
            if not isinstance(item, dict):
                continue
            raw = item.get("ip-address")
            if not isinstance(raw, str):
                continue
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if address.version != 4 or address.is_loopback or address.is_link_local:
                continue
            addresses.append(address)

    if not addresses:
        return None

    # Prefer a globally routable address, then fall back to a private IPv4.
    addresses.sort(key=lambda address: (not address.is_global, address.is_private, str(address)))
    return str(addresses[0])


def wait_for_guest_ipv4(vmid, timeout=90, interval=3):
    """Wait briefly for QEMU Guest Agent/DHCP to report the guest IPv4."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        address = get_guest_ipv4(vmid)
        if address:
            return address
        time.sleep(interval)
    return None


def get_vm_state(vmid):
    """Return the Proxmox runtime state for a VM."""
    result = proxmox.nodes(PROXMOX_NODE).qemu(vmid).status.current.get()
    state = str(result.get("status", "")).strip().lower()
    if state not in {"running", "stopped", "paused", "suspended"}:
        return "unknown"
    return state


def perform_power_action(vmid, action):
    """Submit a safe VPS power action and return the current state plus task."""
    if action not in {"start", "shutdown", "reboot"}:
        raise ValueError("Unsupported power action.")

    vm = proxmox.nodes(PROXMOX_NODE).qemu(vmid)
    state = get_vm_state(vmid)

    if action == "start":
        if state == "running":
            return {"state": state, "task": None, "changed": False}
        task = vm.status.start.post()
        return {"state": state, "task": task, "changed": True}

    if action == "shutdown":
        if state == "stopped":
            return {"state": state, "task": None, "changed": False}
        task = vm.status.shutdown.post()
        return {"state": state, "task": task, "changed": True}

    if state != "running":
        raise RuntimeError("VPS must be running before it can be rebooted.")
    task = vm.status.reboot.post()
    return {"state": state, "task": task, "changed": True}


def clone_vps(name, vmid):
    """Start a clone and return immediately with the Proxmox UPID.

    Waiting and post-clone configuration are intentionally performed by the
    provisioning worker, never by a request or status-poll endpoint.
    """
    safe_name = sanitize_vps_name(name)

    task = proxmox.nodes(PROXMOX_NODE).qemu(VPS_TEMPLATE_VMID).clone.create(
        newid=vmid,
        name=safe_name,
        target=PROXMOX_NODE,
        full=1,
        storage="local-lvm",
    )

    return {
        "vmid": vmid,
        "task": task,
        "name": safe_name,
    }


def wait_for_task(task, progress_callback=None):
    while True:
        result = proxmox.nodes(PROXMOX_NODE).tasks(task).status.get()

        if result.get("status") == "stopped":
            if result.get("exitstatus") == "OK":
                if progress_callback:
                    progress_callback(90, "Template clone completed.")
                return True

            raise Exception(
                f"Proxmox task failed: {result.get('exitstatus')}"
            )

        if progress_callback:
            percent = get_task_progress(task)

            if percent is not None:
                # Reserve 0-90% for the actual template clone.
                mapped = 5 + int(percent * 0.85)
                mapped = min(90, max(5, mapped))
                progress_callback(mapped, f"Cloning VPS... {percent:.0f}%")
            else:
                progress_callback(5, "Cloning VPS template...")

        time.sleep(2)
