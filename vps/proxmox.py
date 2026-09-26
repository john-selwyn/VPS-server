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
PROXMOX_NODE = os.getenv("PROXMOX_NODE", "").strip()
VPS_TEMPLATE_VMID_RAW = os.getenv("VPS_TEMPLATE_VMID", "").strip()

if not all((
    PROXMOX_HOST, PROXMOX_USER, PROXMOX_TOKEN_NAME, PROXMOX_TOKEN_SECRET,
    PROXMOX_NODE, VPS_TEMPLATE_VMID_RAW,
)):
    raise RuntimeError("Proxmox connection and target configuration must be set with environment variables.")

try:
    VPS_TEMPLATE_VMID = int(VPS_TEMPLATE_VMID_RAW)
except ValueError as exc:
    raise RuntimeError("VPS_TEMPLATE_VMID must be a positive integer.") from exc
if VPS_TEMPLATE_VMID <= 0:
    raise RuntimeError("VPS_TEMPLATE_VMID must be a positive integer.")

proxmox = ProxmoxAPI(
    PROXMOX_HOST, user=PROXMOX_USER, token_name=PROXMOX_TOKEN_NAME,
    token_value=PROXMOX_TOKEN_SECRET,
    verify_ssl=os.getenv("PROXMOX_CA_BUNDLE") or True, timeout=30,
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


class InvalidProxmoxResponse(ValueError):
    pass


def resolve_vm_node(vmid):
    """Resolve a QEMU VM's current cluster node and reject ambiguous responses."""
    if type(vmid) is not int or vmid <= 0:
        raise ValueError("Invalid VMID.")
    resources = proxmox.cluster.resources.get(type="vm")
    if not isinstance(resources, list):
        raise InvalidProxmoxResponse("Invalid cluster resource response.")

    matches = []
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("type") != "qemu":
            continue
        try:
            resource_vmid = int(resource.get("vmid"))
        except (TypeError, ValueError):
            continue
        if resource_vmid != vmid:
            continue
        node = resource.get("node")
        if not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}", node):
            raise InvalidProxmoxResponse("Invalid VM node response.")
        matches.append(node)

    if len(matches) != 1:
        raise InvalidProxmoxResponse("VM node could not be resolved uniquely.")
    return matches[0]


def get_vm_state(vmid, *, node=PROXMOX_NODE):
    """Validate the process status and the more precise QMP run state."""
    result = proxmox.nodes(node).qemu(vmid).status.current.get()
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        raise InvalidProxmoxResponse("Invalid runtime response.")
    state = result["status"]
    qmp = result.get("qmpstatus")
    if "qmpstatus" in result and not isinstance(qmp, str):
        raise InvalidProxmoxResponse("Invalid QMP state.")
    lock = result.get("lock")
    if "lock" in result and not isinstance(lock, str):
        raise InvalidProxmoxResponse("Invalid VM lock.")
    # Suspend-to-disk has no running QEMU process, but is not an ordinary stop.
    if lock == "suspended":
        return "suspended"
    if lock:
        return "unknown"
    if state not in {"running", "stopped", "paused", "suspended", "unknown"}:
        return "unknown"
    if qmp is None:
        return state
    if state == "running":
        return {"running": "running", "paused": "paused", "suspended": "suspended"}.get(qmp, "unknown")
    if state == "stopped":
        return "stopped" if qmp == "stopped" else "unknown"
    return state if qmp == state else "unknown"


def validate_power_upid(task, vmid, action, node):
    # UPID:<node>:<pid hex>:<process start hex>:<start hex>:<type>:<id>:<user>:
    if not isinstance(task, str) or len(task) > 1024:
        raise InvalidProxmoxResponse("Invalid power task.")
    match = re.fullmatch(r"UPID:([^:\s]+):([0-9A-Fa-f]{8,}):([0-9A-Fa-f]{8,}):([0-9A-Fa-f]{8,}):([^:\s]+):([0-9]+):([^:\s]+):", task)
    task_types = {
        "start": {"qmstart", "hastart"},
        "shutdown": {"qmshutdown", "hastop"},
        "reboot": {"qmreboot"},
    }
    if not match or match[1] != node or match[5] not in task_types[action] or match[6] != str(vmid):
        raise InvalidProxmoxResponse("Invalid power task.")
    return task


def submit_power_action(vmid, action, *, node=PROXMOX_NODE):
    """One submission only. Caller must durably record the ambiguous window first."""
    if not isinstance(action, str) or action not in {"start", "shutdown", "reboot"}:
        raise ValueError("Unsupported power action.")
    vm = proxmox.nodes(node).qemu(vmid)
    if action == "start":
        task = vm.status.start.post()
    elif action == "shutdown":
        task = vm.status.shutdown.post(timeout=120, forceStop=0)
    else:
        task = vm.status.reboot.post(timeout=120)
    # Do not retry with different parameters if an older server rejects these.
    return validate_power_upid(task, vmid, action, node)


def get_power_task_status(task, *, node=PROXMOX_NODE):
    """Return only running/succeeded/failed; never propagate upstream error text."""
    result = proxmox.nodes(node).tasks(task).status.get()
    if not isinstance(result, dict):
        raise InvalidProxmoxResponse("Invalid task response.")
    if result.get("status") == "running" and "exitstatus" not in result:
        return "running"
    if result.get("status") == "stopped" and isinstance(result.get("exitstatus"), str) and result["exitstatus"]:
        return "succeeded" if result["exitstatus"] == "OK" else "failed"
    raise InvalidProxmoxResponse("Invalid task response.")


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
