import os
import re
import time

from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI


# Load environment variables
load_dotenv()


# Proxmox configuration
PROXMOX_HOST = os.getenv("PROXMOX_HOST", "192.168.80.135")
PROXMOX_USER = os.getenv("PROXMOX_USER", "vps-api@pve")
PROXMOX_TOKEN_NAME = os.getenv("PROXMOX_TOKEN_NAME", "django")
PROXMOX_TOKEN_SECRET = os.getenv("PROXMOX_TOKEN_SECRET")

PROXMOX_NODE = "test-1"

# Ubuntu VPS template
VPS_TEMPLATE_VMID = 101


# Connect to Proxmox
proxmox = ProxmoxAPI(
    PROXMOX_HOST,
    user=PROXMOX_USER,
    token_name=PROXMOX_TOKEN_NAME,
    token_value=PROXMOX_TOKEN_SECRET,
    verify_ssl=False,
    timeout=30,
)


def sanitize_vps_name(name):
    """
    Convert a customer VPS name into a valid Proxmox hostname.
    """

    if not name:
        name = "vps"

    # Convert to lowercase
    name = name.lower().strip()

    # Replace spaces and invalid characters with hyphens
    name = re.sub(r"[^a-z0-9-]", "-", name)

    # Remove repeated hyphens
    name = re.sub(r"-+", "-", name)

    # Remove leading/trailing hyphens
    name = name.strip("-")

    # Fallback if nothing remains
    if not name:
        name = "vps"

    # DNS hostnames should not exceed 63 characters
    return name[:63]


def get_next_vmid():
    """
    Find the next available VMID starting from 1000.
    """

    existing_vms = proxmox.nodes(PROXMOX_NODE).qemu.get()

    existing_vmids = {
        int(vm["vmid"])
        for vm in existing_vms
        if "vmid" in vm
    }

    vmid = 1000

    while vmid in existing_vmids:
        vmid += 1

    return vmid


def clone_vps(name):
    """
    Clone the VPS template and wait for the clone to finish.
    """

    vmid = get_next_vmid()

    # Convert customer name into a Proxmox-safe hostname
    safe_name = sanitize_vps_name(name)

    # Clone template
    task = proxmox.nodes(PROXMOX_NODE).qemu(VPS_TEMPLATE_VMID).clone.create(
        newid=vmid,
        name=safe_name,
        target=PROXMOX_NODE,
        full=1,
        storage="local-lvm",
    )

    # Wait until cloning finishes
    wait_for_task(task)

    return {
        "vmid": vmid,
        "task": task,
        "name": safe_name,
    }


def wait_for_task(task):
    """
    Wait until a Proxmox task finishes.
    """

    while True:

        result = proxmox.nodes(PROXMOX_NODE).tasks(task).status.get()

        if result.get("status") == "stopped":

            if result.get("exitstatus") == "OK":
                return True

            raise Exception(
                f"Proxmox task failed: {result.get('exitstatus')}"
            )

        time.sleep(2)