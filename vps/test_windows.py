import ipaddress
import json
import os
from unittest.mock import patch
from django.core.management import call_command, CommandError

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings

from .internal_api import configuration
from .models import VPS, PublicSSHAccess
from .windows_credentials import generate_encrypted_password, decrypt_windows_password


@override_settings(
    BILLING_API_SECRET="windows-test-secret",
    PUBLIC_SSH_ENABLED=True,
    VPS_IP_POOL_START=ipaddress.IPv4Address("10.60.0.100"),
    VPS_IP_POOL_END=ipaddress.IPv4Address("10.60.0.102"),
    VPS_IP_NETWORK=ipaddress.IPv4Network("10.60.0.0/24"),
    VPS_IP_GATEWAY=ipaddress.IPv4Address("10.60.0.1"),
    VPS_IP_PREFIX=24,
    VPS_IP_DNS=ipaddress.IPv4Address("8.8.8.8"),
)
class WindowsTests(TestCase):
    auth = {"HTTP_AUTHORIZATION": "Bearer windows-test-secret"}
    url = "/api/internal/v1/vps/500/windows-credentials/"

    def setUp(self):
        key = Fernet.generate_key().decode()
        env = patch.dict(os.environ, {"WINDOWS_PASSWORD_ENCRYPTION_KEY": key})
        env.start()
        self.addCleanup(env.stop)
        templates = patch("vps.proxmox.VPS_OS_TEMPLATES", {"Windows 11": 106})
        templates.start()
        self.addCleanup(templates.stop)
        self.vps = VPS.objects.create(
            billing_order_id=500, name="customer-1-vps-500", vmid=1500,
            operating_system="Windows 11", status="Running",
            ip_address="10.60.0.101", cpu=2, ram=4, storage=64,
            windows_password_encrypted=generate_encrypted_password(),
        )
        self.access = PublicSSHAccess.objects.create(
            vps=self.vps, host="example.test", port=22500, status="ready",
        )

    def payload(self):
        return dict(order_id=501, name="customer-1-vps-501",
                    cpu=2, ram=4, storage=64, os="Windows 11",
                    billing_cycle="Monthly", plan="Windows")

    def test_passwords_are_unique_encrypted_and_complex(self):
        one = generate_encrypted_password()
        two = generate_encrypted_password()
        plain = decrypt_windows_password(one)
        self.assertNotEqual(one, two)
        self.assertNotEqual(plain, one)
        self.assertEqual(len(plain), 24)
        self.assertTrue(any(c.islower() for c in plain))
        self.assertTrue(any(c.isupper() for c in plain))
        self.assertTrue(any(c.isdigit() for c in plain))
        self.assertTrue(any(c in "!@#%+-_" for c in plain))

    def test_wrong_encryption_key_fails(self):
        with patch.dict(os.environ, {
            "WINDOWS_PASSWORD_ENCRYPTION_KEY": Fernet.generate_key().decode()
        }):
            with self.assertRaises(RuntimeError):
                decrypt_windows_password(self.vps.windows_password_encrypted)

    def test_credentials_require_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.client.get(self.url,
            HTTP_AUTHORIZATION="Bearer wrong").status_code, 401)
        self.assertEqual(self.client.post(self.url, **self.auth).status_code, 405)

    def test_credentials_and_cache_headers(self):
        response = self.client.get(self.url, **self.auth)
        self.assertEqual(response.status_code, 200)
        access = response.json()["rdp_access"]
        self.assertEqual(access["host"], "example.test")
        self.assertEqual(access["port"], 22500)
        self.assertEqual(access["username"], "Admin")
        self.assertTrue(access["password"] == decrypt_windows_password(
            self.vps.windows_password_encrypted))
        self.assertIn("no-store", response["Cache-Control"])

    def test_not_ready_and_linux_credentials_rejected(self):
        for fields in (
            {"status": "Provisioning"},
            {"status": "Running", "operating_system": "Ubuntu 26.04"},
        ):
            VPS.objects.filter(pk=self.vps.pk).update(**fields)
            self.assertEqual(self.client.get(self.url, **self.auth).status_code, 409)

    def test_relay_must_be_ready(self):
        PublicSSHAccess.objects.filter(pk=self.access.pk).update(status="pending")
        self.assertEqual(self.client.get(self.url, **self.auth).status_code, 409)

    def test_invalid_ciphertext_returns_generic_error(self):
        VPS.objects.filter(pk=self.vps.pk).update(windows_password_encrypted="invalid")
        self.assertEqual(self.client.get(self.url, **self.auth).status_code, 503)

    def test_windows_minimum_resources(self):
        for field, value in (("cpu", 1), ("ram", 2), ("storage", 32)):
            payload = self.payload()
            payload[field] = value
            with self.assertRaises(ValueError):
                configuration(payload)

    def test_windows_rejects_ssh_fields(self):
        payload = self.payload()
        payload.update(ssh_username="user", ssh_public_key="unused")
        with self.assertRaises(ValueError):
            configuration(payload)

    def test_windows_provisioning_is_idempotent(self):
        with patch("vps.views.get_next_vmid", return_value=1501), patch(
            "vps.views.clone_vps", return_value={"task": "mock-task"}
        ) as clone, patch("vps.views._start_worker"):
            for _ in range(2):
                response = self.client.post("/api/internal/provision/",
                    json.dumps(self.payload()), content_type="application/json", **self.auth)
                self.assertIn(response.status_code, (200, 202))
                self.assertNotIn("password", response.content.decode())
            clone.assert_called_once_with("customer-1-vps-501", 1501, template_vmid=106)
        created = VPS.objects.get(billing_order_id=501)
        self.assertTrue(created.windows_password_encrypted)
        self.assertEqual(len(decrypt_windows_password(created.windows_password_encrypted)), 24)

    @override_settings(PUBLIC_SSH_ENABLED=False, VPS_BRIDGE="vmbr1")
    def test_windows_worker_configures_credentials_and_waits_for_network(self):
        VPS.objects.filter(pk=self.vps.pk).update(status="Provisioning", task_upid="mock-clone")
        prefix = "vps.management.commands.provision_vps."
        with patch(prefix + "proxmox") as proxmox, patch(prefix + "wait_for_task"), \
                patch(prefix + "resolve_vm_node", return_value="test-1"), \
                patch(prefix + "wait_for_guest_ipv4_match", return_value=True) as verify:
            vm = proxmox.nodes.return_value.qemu.return_value
            vm.config.get.return_value = {
                "ostype": "win11", "scsi0": "local:disk,size=64G",
                "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1",
            }
            vm.status.current.get.return_value = {"status": "running"}
            call_command("provision_vps", self.vps.pk)
        self.vps.refresh_from_db()
        self.assertEqual(self.vps.status, "Running")
        vm.config.set.assert_any_call(ciuser="Admin", cipassword=decrypt_windows_password(
            self.vps.windows_password_encrypted))
        vm.config.set.assert_any_call(net0="virtio=BC:24:11:AA:BB:CC,bridge=vmbr1,firewall=1")
        verify.assert_called_once_with(1500, "10.60.0.101", timeout=600, interval=3, node="test-1")

    @override_settings(PUBLIC_SSH_ENABLED=True)
    def test_windows_network_timeout_never_publishes_ready_access(self):
        VPS.objects.filter(pk=self.vps.pk).update(status="Provisioning", task_upid="mock-clone")
        prefix = "vps.management.commands.provision_vps."
        with patch(prefix + "proxmox") as proxmox, patch(prefix + "wait_for_task"), \
                patch(prefix + "resolve_vm_node", return_value="test-1"), \
                patch(prefix + "wait_for_guest_ipv4_match", return_value=False), \
                patch(prefix + "sync_access") as relay:
            vm = proxmox.nodes.return_value.qemu.return_value
            vm.config.get.return_value = {"ostype": "win11", "scsi0": "local:disk,size=64G",
                                         "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr1"}
            vm.status.current.get.return_value = {"status": "running"}
            with self.assertRaisesMessage(CommandError, "Guest network verification failed."):
                call_command("provision_vps", self.vps.pk)
        self.vps.refresh_from_db()
        self.assertEqual(self.vps.status, "Failed")
        relay.assert_not_called()
        from .public_ssh import endpoint_fields
        self.assertIsNone(endpoint_fields(self.vps)["rdp_access"])

    def test_relay_protocol_and_response_match_without_sending_password(self):
        from types import SimpleNamespace
        from .public_ssh import Config, _request_relay
        config = Config("example.test", 22000, 22999, "10.50.0.10", "/unused/key", "/unused/hosts")
        def reply(argv, **kwargs):
            request = json.loads(kwargs["input"])
            self.assertNotIn("password", request)
            return SimpleNamespace(returncode=0, stdout=json.dumps({**request, "ready": True}))
        with patch("vps.public_ssh.subprocess.run", side_effect=reply) as run:
            _request_relay(self.access, config)
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual((request["protocol"], request["username"]), ("rdp", "Admin"))
        self.assertEqual(request["vmid"], self.vps.vmid)
        self.access.vps.operating_system = "Ubuntu 26.04"
        self.access.vps.ssh_username = "vpsuser"
        with patch("vps.public_ssh.subprocess.run", side_effect=reply) as run:
            _request_relay(self.access, config)
        request = json.loads(run.call_args.kwargs["input"])
        self.assertNotIn("protocol", request)
        self.assertEqual(request["username"], "vpsuser")
