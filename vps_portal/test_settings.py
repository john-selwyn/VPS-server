"""Local validation settings: never connect to deployment database or Proxmox."""
import os
from unittest.mock import patch

# Public fake values used only while importing production settings.
with patch.dict(os.environ, {
    "BILLING_API_SECRET": "test-only-internal-api-secret-00000000000000000000",
    "DJANGO_SECRET_KEY": "test-only-signing-key-for-isolated-local-tests-never-use-in-production",
    "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
    "VPS_DB_NAME": "test_only_unused_database",
    "VPS_DB_USER": "test_only_unused_user",
    "VPS_DB_PASSWORD": "test-only-unused-password",
    "VPS_DB_HOST": "database.invalid",
    "VPS_DB_PORT": "5432",
}):
    from .settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

# Proxmox helpers are imported after Django settings. Force harmless process-local
# values so a missed mock cannot point at a real deployment endpoint.
os.environ.update({
    "PROXMOX_HOST": "proxmox.invalid",
    "PROXMOX_USER": "test@pve",
    "PROXMOX_TOKEN_NAME": "test-only-token",
    "PROXMOX_TOKEN_SECRET": "test-only-secret",
    "PROXMOX_NODE": "test-1",
    "VPS_TEMPLATE_VMID": "101",
})
