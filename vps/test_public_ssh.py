import ipaddress
import json
from pathlib import Path
import subprocess
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
import uuid

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import PublicSSHAccess, VPS
from .public_ssh import Config, PublicSSHError, _request_relay, endpoint_fields, reserve_access, sync_access


@override_settings(PUBLIC_SSH_ENABLED=True, PUBLIC_SSH_HOST="proxmoxportal.dyndns.org",
                   PUBLIC_SSH_PORT_START=22000, PUBLIC_SSH_PORT_END=22002,
                   PUBLIC_SSH_RELAY_HOST="10.50.0.10",
                   VPS_IP_NETWORK=ipaddress.IPv4Network("10.60.0.0/24"),
                   VPS_IP_GATEWAY=ipaddress.IPv4Address("10.60.0.1"))
class PublicSSHTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.key = Path(temporary.name) / "key"
        self.known = Path(temporary.name) / "known_hosts"
        self.key.write_text("test fixture, not a real key")
        self.key.chmod(0o600)
        self.known.write_text("test fixture")
        config = override_settings(PUBLIC_SSH_RELAY_KEY=str(self.key), PUBLIC_SSH_RELAY_KNOWN_HOSTS=str(self.known))
        config.enable()
        self.addCleanup(config.disable)
        self.vps = self.make_vps(17, 1016, "10.60.0.15")

    def make_vps(self, order, vmid, ip):
        return VPS.objects.create(billing_order_id=order, name=f"customer-2-vps-{order}",
            vmid=vmid, ip_address=ip, status="Running", ssh_username="vpsuser",
            ssh_public_key="test-only-public-key")

    def test_adoption_and_retries_preserve_port_and_ownership(self):
        access = reserve_access(self.vps.pk, port=22001)
        self.assertEqual(reserve_access(self.vps.pk).pk, access.pk)
        other = self.make_vps(18, 1017, "10.60.0.16")
        with self.assertRaisesMessage(PublicSSHError, "SSH_PORT_IN_USE"):
            reserve_access(other.pk, port=22001)
        with self.assertRaisesMessage(PublicSSHError, "SSH_PORT_ALREADY_ASSIGNED"):
            reserve_access(self.vps.pk, port=22002)
        self.assertEqual(reserve_access(other.pk).port, 22000)

    def test_pool_exhaustion_and_unique_constraint(self):
        access = reserve_access(self.vps.pk)
        for index in (18, 19):
            reserve_access(self.make_vps(index, index + 1000, f"10.60.0.{index}").pk)
        extra = self.make_vps(20, 1020, "10.60.0.20")
        with self.assertRaisesMessage(PublicSSHError, "SSH_PORT_POOL_EXHAUSTED"):
            reserve_access(extra.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PublicSSHAccess.objects.create(vps=extra, host=access.host, port=access.port)

    def test_reservation_retries_a_database_collision(self):
        with patch.object(PublicSSHAccess.objects, "create", side_effect=[IntegrityError(), Mock(port=22000)]):
            self.assertEqual(reserve_access(self.vps.pk).port, 22000)
        self.assertFalse(PublicSSHAccess.objects.exists())

    def test_invalid_configuration_and_noncustomer_targets(self):
        for host in ("-oProxyCommand=x", "host;id", "https://example.org", "", "a" * 254):
            with override_settings(PUBLIC_SSH_HOST=host), self.assertRaises(PublicSSHError):
                Config.load()
        self.key.chmod(0o644)
        with self.assertRaisesMessage(PublicSSHError, "SSH_ACCESS_NOT_CONFIGURED"):
            Config.load()
        self.key.chmod(0o600)
        for ip in ("10.50.0.22", "10.60.0.1", "10.60.0.255", "0.0.0.0"):
            self.vps.ip_address = ip
            self.vps.save(update_fields=["ip_address"])
            with self.assertRaisesMessage(PublicSSHError, "SSH_ACCESS_NOT_READY"):
                reserve_access(self.vps.pk)

    @patch("vps.public_ssh._request_relay")
    def test_endpoint_only_appears_after_confirmation_and_is_idempotent(self, relay):
        self.assertEqual(endpoint_fields(self.vps), {"ssh_access": None})
        access = sync_access(self.vps.pk)
        self.assertEqual(access.status, "ready")
        self.assertEqual(endpoint_fields(self.vps), {"ssh_access": {"host": access.host, "port": 22000}})
        sync_access(self.vps.pk)
        relay.assert_called_once()
        with override_settings(PUBLIC_SSH_ENABLED=False):
            self.assertEqual(endpoint_fields(self.vps), {})
            self.assertIsNone(sync_access(self.vps.pk))

    @patch("vps.public_ssh._request_relay", side_effect=PublicSSHError("SSH_RELAY_FAILED"))
    def test_failed_relay_keeps_running_vm_and_reservation_then_retries(self, relay):
        with self.assertRaises(PublicSSHError):
            sync_access(self.vps.pk)
        access = PublicSSHAccess.objects.get(vps=self.vps)
        self.assertEqual((access.status, access.port), ("failed", 22000))
        self.vps.refresh_from_db()
        self.assertEqual(self.vps.status, "Running")
        self.assertEqual(endpoint_fields(self.vps), {"ssh_access": None})
        sync_access(self.vps.pk)
        relay.assert_called_once()
        PublicSSHAccess.objects.filter(pk=access.pk).update(retry_at=timezone.now() - timedelta(seconds=1))
        relay.side_effect = None
        self.assertEqual(sync_access(self.vps.pk).status, "ready")
        self.assertEqual(relay.call_count, 2)

    @patch("vps.public_ssh._request_relay")
    def test_live_lease_and_late_results_do_not_overwrite_new_worker(self, relay):
        access = reserve_access(self.vps.pk)
        PublicSSHAccess.objects.filter(pk=access.pk).update(lease=uuid.uuid4(), claimed_at=timezone.now())
        sync_access(self.vps.pk)
        relay.assert_not_called()
        PublicSSHAccess.objects.filter(pk=access.pk).update(claimed_at=timezone.now() - timedelta(minutes=3))
        newer_lease = uuid.uuid4()
        relay.side_effect = lambda *args: PublicSSHAccess.objects.filter(pk=access.pk).update(lease=newer_lease)
        sync_access(self.vps.pk)
        access.refresh_from_db()
        self.assertEqual(access.lease, newer_lease)
        self.assertEqual(access.status, "pending")

    @patch("vps.public_ssh.subprocess.run")
    def test_transport_pins_host_key_and_rejects_wrong_response(self, run):
        access = reserve_access(self.vps.pk)
        run.return_value = Mock(returncode=0, stdout=json.dumps({"ready": True}))
        with self.assertRaisesMessage(PublicSSHError, "SSH_RELAY_INVALID_RESPONSE"):
            _request_relay(access, Config.load())
        argv = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertEqual(argv[-2:], ["root@10.50.0.10", "vps-ssh-relay"])
        self.assertNotIn("shell", run.call_args.kwargs)
        request = json.loads(run.call_args.kwargs["input"])
        run.return_value.stdout = json.dumps({**request, "ready": True})
        _request_relay(access, Config.load())
        run.side_effect = subprocess.TimeoutExpired("ssh", 45)
        with self.assertRaisesMessage(PublicSSHError, "SSH_RELAY_TIMEOUT"):
            _request_relay(access, Config.load())

    @patch("vps.public_ssh._request_relay")
    def test_worker_skips_backoff_without_starving_unreserved_order(self, relay):
        access = reserve_access(self.vps.pk)
        PublicSSHAccess.objects.filter(pk=access.pk).update(status="failed", retry_at=timezone.now() + timedelta(seconds=60))
        other = self.make_vps(18, 1017, "10.60.0.16")
        call_command("process_public_ssh", once=True, stdout=StringIO())
        self.assertEqual(PublicSSHAccess.objects.get(vps=other).status, "ready")
        relay.assert_called_once()

    @override_settings(BILLING_API_SECRET="test-only-shared-secret")
    @patch("vps.internal_api.get_vm_state", return_value="running")
    @patch("vps.internal_api.resolve_vm_node", return_value="test-1")
    @patch("vps.public_ssh._request_relay")
    def test_authenticated_status_reads_have_no_relay_side_effect(self, relay, node, state):
        access = reserve_access(self.vps.pk)
        PublicSSHAccess.objects.filter(pk=access.pk).update(status="ready")
        for url in ("/api/internal/provision/17/status/", "/api/internal/v1/vps/17/"):
            self.assertEqual(self.client.get(url).status_code, 401)
            response = self.client.get(url, HTTP_AUTHORIZATION="Bearer test-only-shared-secret")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ssh_access"]["port"], 22000)
        relay.assert_not_called()
