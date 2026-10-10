from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from .proxmox_ssh_relay import Relay, RelayError, probe_ssh, trusted_file, unique_object


class RelayTests(unittest.TestCase):
    def setUp(self):
        # Test the root helper's behavior without requiring a root test runner.
        trust = patch("deploy.proxmox_ssh_relay.trusted_file")
        trust.start()
        self.addCleanup(trust.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.units = root / "units"
        self.units.mkdir()
        self.policy = {"bind_ip": "10.50.0.10", "guest_network": "10.60.0.0/24",
                       "bridge": "vmbr1", "port_start": 22000, "port_end": 22999}
        self.request = {"vmid": 1016, "order_id": 17, "ip": "10.60.0.15", "port": 22015, "username": "vpsuser"}
        self.config = "name: customer-2-vps-17\nnet0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr1,firewall=1\nipconfig0: ip=10.60.0.15/24,gw=10.60.0.1\nciuser: vpsuser\n"
        self.runner = Mock(side_effect=lambda argv: self.config if argv[0] == "/usr/sbin/qm" else "")
        self.probe = Mock()
        self.relay = Relay(self.policy, root / "state", self.units, self.runner, self.probe)

    def test_persistent_idempotent_service_and_private_claims(self):
        self.assertEqual(self.relay.ensure(self.request), {**self.request, "ready": True})
        self.relay.ensure(self.request)
        unit = self.units / "vps-1016-ssh-relay.service"
        self.assertIn("WantedBy=multi-user.target", unit.read_text())
        self.assertIn("User=nobody", unit.read_text())
        self.assertIn("TCP:10.60.0.15:22", unit.read_text())
        self.assertEqual(sum(call.args[0][-1] == "daemon-reload" for call in self.runner.call_args_list), 1)
        claims = self.relay.state / "claims.json"
        self.assertEqual(claims.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(claims.read_text()), {"1016": self.request})

    def test_adopts_existing_service_without_rewriting_or_restarting(self):
        unit = self.units / "vps-1016-ssh-relay.service"
        original = "[Service]\nExecStart=/usr/bin/socat TCP-LISTEN:22015,bind=10.50.0.10,reuseaddr,fork TCP:10.60.0.15:22\n"
        unit.write_text(original)
        self.relay.ensure(self.request)
        self.assertEqual(unit.read_text(), original)
        self.assertFalse(any("restart" in call.args[0] for call in self.runner.call_args_list))

    def test_failed_guest_probe_keeps_claim_but_never_confirms_endpoint(self):
        self.probe.side_effect = OSError("unavailable")
        with self.assertRaises(OSError):
            self.relay.ensure(self.request)
        self.assertTrue((self.relay.state / "claims.json").exists())
        self.assertFalse(list(self.units.iterdir()))
        self.assertEqual(self.runner.call_count, 1)

    def test_guest_ready_but_relay_failed_never_confirms_endpoint(self):
        self.probe.side_effect = [None, RelayError("SSH_NOT_READY")]
        with self.assertRaisesRegex(RelayError, "SSH_NOT_READY"):
            self.relay.ensure(self.request)
        self.assertTrue((self.relay.state / "claims.json").exists())

    def test_refuses_retargeting_existing_vm_and_unrelated_unit(self):
        self.relay.ensure(self.request)
        self.config = self.config.replace("10.60.0.15", "10.60.0.16")
        with self.assertRaisesRegex(RelayError, "VM_ALREADY_ASSIGNED"):
            self.relay.ensure({**self.request, "ip": "10.60.0.16"})
        (self.units / "vps-1016-ssh-relay.service").write_text("ExecStart=/usr/bin/other-service\n")
        self.config = self.config.replace("10.60.0.16", "10.60.0.15")
        with self.assertRaisesRegex(RelayError, "SERVICE_ALREADY_ASSIGNED"):
            self.relay.ensure(self.request)

    def test_rejects_injection_outside_network_and_mismatched_vm(self):
        invalid = [{**self.request, "port": True}, {**self.request, "port": 22},
                   {**self.request, "vmid": "1016;id"}, {**self.request, "order_id": True},
                   {**self.request, "username": "root"}, {**self.request, "username": "user;id"},
                   {**self.request, "ip": "10.50.0.22"}, {**self.request, "extra": "command"}]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(RelayError):
                self.relay.ensure(request)
        self.runner.assert_not_called()
        for old, new in (("vmbr1", "vmbr0"), ("vps-17", "vps-18"),
                         ("ciuser: vpsuser", "ciuser: root"), ("10.60.0.15/24", "dhcp")):
            original = self.config
            self.config = original.replace(old, new)
            with self.subTest(new=new), self.assertRaisesRegex(RelayError, "VM_TARGET_MISMATCH"):
                self.relay.ensure(self.request)
            self.config = original
        self.assertFalse(self.relay.state.exists())

    def test_parallel_requests_cannot_claim_same_port(self):
        other = {**self.request, "vmid": 1017, "order_id": 18, "ip": "10.60.0.16"}
        def runner(argv):
            if argv[0] == "/usr/sbin/qm":
                return self.config if argv[-1] == "1016" else self.config.replace("vps-17", "vps-18").replace("10.60.0.15", "10.60.0.16")
            return ""
        def ensure(request):
            try:
                return Relay(self.policy, self.relay.state, self.units, runner, self.probe).ensure(request)
            except RelayError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(ensure, (self.request, other)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn("TARGET_ALREADY_ASSIGNED", results)
        self.assertEqual(len(json.loads((self.relay.state / "claims.json").read_text())), 1)

    def windows_request(self):
        self.config = self.config.replace("ciuser: vpsuser", "ciuser: Admin") + "ostype: win11\n"
        return {**self.request, "username": "Admin", "protocol": "rdp"}

    def test_rdp_idempotent_service_and_tcp_probes(self):
        request = self.windows_request()
        with patch("deploy.proxmox_ssh_relay.probe_tcp") as probe:
            self.assertEqual(self.relay.ensure(request), {**request, "ready": True})
            self.relay.ensure(request)
        self.assertIn("TCP:10.60.0.15:3389", (self.units / "vps-1016-rdp-relay.service").read_text())
        self.assertEqual(probe.call_args_list[0].args, ("10.60.0.15", 3389))
        self.assertEqual(probe.call_args_list[1].args, ("10.50.0.10", 22015))
        self.probe.assert_not_called()
        self.assertEqual(sum(call.args[0][-1] == "daemon-reload" for call in self.runner.call_args_list), 1)
        self.assertEqual(json.loads((self.relay.state / "claims.json").read_text()), {"1016": request})

    def test_rejects_wrong_rdp_os_user_protocol_and_ssh_on_windows(self):
        request = self.windows_request()
        for invalid in ({**request, "username": "vpsuser"}, {**request, "protocol": "http"},
                        {**request, "username": "vpsuser", "protocol": "ssh"}):
            with self.subTest(invalid=invalid), self.assertRaises(RelayError):
                self.relay.validate(invalid)
        self.config = self.config.replace("ostype: win11", "ostype: l26")
        with self.assertRaisesRegex(RelayError, "VM_TARGET_MISMATCH"):
            self.relay.validate(request)
        self.assertFalse(self.relay.state.exists())

    def test_failed_rdp_probe_keeps_claim_without_service(self):
        request = self.windows_request()
        with patch("deploy.proxmox_ssh_relay.probe_tcp", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.relay.ensure(request)
        self.assertTrue((self.relay.state / "claims.json").exists())
        self.assertFalse(list(self.units.iterdir()))

    def test_duplicate_fields_and_nonssh_banner_are_rejected(self):
        path = Mock()
        path.lstat.return_value = Mock(st_uid=0, st_mode=0o100600)
        path.is_symlink.return_value = False
        trusted_file(path)
        path.lstat.return_value.st_uid = 1000
        with self.assertRaisesRegex(RelayError, "UNSAFE_CONFIGURATION"):
            trusted_file(path)
        path.lstat.return_value.st_uid = 0
        path.lstat.return_value.st_mode = 0o100666
        with self.assertRaisesRegex(RelayError, "UNSAFE_CONFIGURATION"):
            trusted_file(path)
        with self.assertRaisesRegex(RelayError, "DUPLICATE_FIELD"):
            json.loads('{"port":22015,"port":22}', object_pairs_hook=unique_object)
        with patch("deploy.proxmox_ssh_relay.socket.create_connection") as connect:
            stream = connect.return_value.__enter__.return_value
            stream.recv.return_value = b"HTTP/1.1 200 OK"
            with self.assertRaisesRegex(RelayError, "SSH_NOT_READY"):
                probe_ssh("10.60.0.15", 22)
            stream.recv.side_effect = [b"SS", b"H-2.0-OpenSSH"]
            probe_ssh("10.60.0.15", 22)
