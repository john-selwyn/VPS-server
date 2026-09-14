import os
from proxmoxer import ProxmoxAPI

PROXMOX_HOST = "192.168.80.135"
PROXMOX_USER = "vps-api@pve"
PROXMOX_TOKEN_NAME = "django"
PROXMOX_TOKEN_SECRET = "9454e800-8957-4a24-924f-cb93e6f1f080"

proxmox = ProxmoxAPI(
    PROXMOX_HOST,
    user=PROXMOX_USER,
    token_name=PROXMOX_TOKEN_NAME,
    token_value=PROXMOX_TOKEN_SECRET,
    verify_ssl=False,
    timeout=30,
)
