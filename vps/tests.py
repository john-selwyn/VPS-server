import json
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.core.management import call_command
from django.test import Client, TestCase, TransactionTestCase, override_settings

from .models import VPS
from .networking import IPPoolExhausted, next_available_ip


@override_settings(BILLING_API_SECRET="test-only-shared-secret")
class InternalProvisioningTests(TestCase):
    url = "/api/internal/provision/"
    auth = {"HTTP_AUTHORIZATION": "Bearer test-only-shared-secret"}

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.payload = {"order_id": 123, "name": "customer-vps", "cpu": 2, "ram": 4,
                        "storage": 50, "os": "Ubuntu 26.04", "billing_cycle": "MONTHLY",
                        "plan": "VPS Starter"}
        # Block the client as well as mock the normal provisioning entry points.
        # An unexpected direct Proxmox call must fail, never reach the network.
        for target, kwargs in (
            ("vps.proxmox.proxmox", {"side_effect": AssertionError("Unexpected Proxmox call")}),
            ("vps.views.get_next_vmid", {"return_value": 115}),
            ("vps.views.clone_vps", {"return_value": {"task": "mock-task"}}),
            ("vps.views._start_worker", {}),
        ):
            patcher = patch(target, **kwargs)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, target.rsplit(".", 1)[-1], mock)

    def post(self, payload=None, **kwargs):
        return self.client.post(self.url, json.dumps(self.payload if payload is None else payload),
                                content_type="application/json", **kwargs)

    def test_authentication(self):
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.post(HTTP_AUTHORIZATION="Bearer incorrect").status_code, 401)
        with override_settings(BILLING_API_SECRET=""):
            self.assertEqual(self.post(**self.auth).status_code, 401)
        self.clone_vps.assert_not_called()

    def test_post_only(self):
        response = self.client.get(self.url, **self.auth)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")

    def test_malformed_json(self):
        for body in ("{", b"\xff", "null", "[]", "1"):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(self.url, body, content_type="application/json",
                                                 **self.auth).status_code, 400)

    def test_missing_fields(self):
        for field in self.payload:
            with self.subTest(field=field):
                payload = self.payload.copy()
                del payload[field]
                self.assertEqual(self.post(payload, **self.auth).status_code, 400)

    def test_invalid_values(self):
        for field in ("order_id", "cpu", "ram", "storage"):
            for value in (0, -1, True, 1.5, "2", None, 2**64):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.post({**self.payload, field: value}, **self.auth).status_code, 400)
        for field in ("name", "os", "billing_cycle", "plan"):
            for value in ("", "   ", None, 2, "x" * 256):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.post({**self.payload, field: value}, **self.auth).status_code, 400)
        self.assertEqual(self.post({**self.payload, "price": 10}, **self.auth).status_code, 400)
        self.assertFalse(VPS.objects.exists())
        self.clone_vps.assert_not_called()

    def test_accepted_and_idempotent(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(**self.auth)
        self.assertEqual(response.status_code, 202)
        vps = VPS.objects.get(billing_order_id=123)
        self.assertEqual(response.json(), {"success": True, "vps_id": vps.pk,
                                          "vmid": 115, "status": "Provisioning"})
        self.assertEqual((vps.cpu, vps.ram, vps.storage, vps.plan), (2, 4, 50, "VPS Starter"))
        self.assertEqual(vps.ip_address, "220.100.130.211")
        self._start_worker.assert_called_once_with(vps.pk)
        retry = self.post(**self.auth)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json(), response.json())
        self.assertEqual(VPS.objects.count(), 1)
        self.clone_vps.assert_called_once_with("customer-vps", 115)
        self.get_next_vmid.assert_called_once()

    def test_failed_clone_is_not_retried(self):
        self.clone_vps.side_effect = RuntimeError("Sensitive internal details")
        first = self.post(**self.auth)
        self.assertEqual(first.json()["status"], "Failed")
        self.assertEqual(self.post(**self.auth).json(), first.json())
        self.clone_vps.assert_called_once()
        self._start_worker.assert_not_called()
        response = self.client.get(self.url + "123/status/", **self.auth)
        self.assertNotIn("Sensitive", response.content.decode())

    def test_existing_reservation_is_not_submitted_again(self):
        vps = VPS.objects.create(name="reserved", billing_order_id=123)
        self.assertEqual(self.post(**self.auth).json()["vps_id"], vps.pk)
        self.clone_vps.assert_not_called()

    def test_unique_order_constraint_and_legacy_nulls(self):
        VPS.objects.create(name="legacy-one")
        VPS.objects.create(name="legacy-two")
        VPS.objects.create(name="first", billing_order_id=123)
        with self.assertRaises(IntegrityError), transaction.atomic():
            VPS.objects.create(name="duplicate", billing_order_id=123)

    def test_status_matching_order_and_authentication(self):
        match = VPS.objects.create(name="match", billing_order_id=123, vmid=115)
        VPS.objects.create(name="other", billing_order_id=456, vmid=116)
        url = self.url + "123/status/"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION="Bearer bad").status_code, 401)
        response = self.client.get(url, **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"billing_order_id": 123, "vps_id": match.pk,
            "vmid": 115, "status": "Provisioning", "progress": 0,
            "current_step": "Preparing VPS", "ip_address": "0.0.0.0", "error_message": ""})
        self.assertEqual(self.client.get(self.url + "999/status/", **self.auth).status_code, 404)
        self.assertEqual(Client().post(url, **self.auth).status_code, 405)
        self.clone_vps.assert_not_called()

    def test_vm_and_ip_reservation_are_committed_before_clone_submission(self):
        def clone(name, vmid):
            from django.db import connection
            self.assertFalse(connection.in_atomic_block)
            reserved = VPS.objects.get(billing_order_id=123)
            self.assertEqual(reserved.vmid, vmid)
            self.assertEqual(reserved.ip_address, "220.100.130.211")
            return {"task": "mock-task"}

        self.clone_vps.side_effect = clone
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(**self.auth)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(VPS.objects.get(billing_order_id=123).ip_address, "220.100.130.211")

    def test_browser_workflow(self):
        browser = Client()
        response = browser.post("/order/", {"name": "browser-vps", "cpu": 2, "ram": 4,
                                           "storage": 50, "os": "Ubuntu 26.04", "billing": "Monthly"})
        self.assertEqual(response.status_code, 200)
        token = response.context["order_token"]
        with self.captureOnCommitCallbacks(execute=True):
            response = browser.post("/order/", {"payment": "success", "order_token": token})
        vps = VPS.objects.get()
        self.assertIsNone(vps.billing_order_id)
        self.assertRedirects(response, f"/provisioning/{vps.pk}/")
        self.assertEqual(browser.get("/").status_code, 200)
        self.assertEqual(browser.get(f"/provisioning/{vps.pk}/status/").status_code, 200)
        browser.post("/order/", {"payment": "success", "order_token": token})
        self.clone_vps.assert_called_once()
        self._start_worker.assert_called_once()
        self.assertEqual(self.client.post("/order/", {}).status_code, 403)

    def test_background_worker_completes_existing_workflow(self):
        vps = VPS.objects.create(name="worker-vps", vmid=115, task_upid="mock-task")
        vps.ip_address = "220.100.130.211"
        vps.save(update_fields=["ip_address"])
        with patch("vps.management.commands.provision_vps.proxmox") as proxmox, \
                patch("vps.management.commands.provision_vps.wait_for_task") as wait:
            vm = proxmox.nodes.return_value.qemu.return_value
            vm.config.get.return_value = {"scsi0": "local:disk,size=32G"}
            vm.status.current.get.return_value = {"status": "stopped"}
            vm.status.start.post.return_value = "mock-start"
            call_command("provision_vps", vps.pk)
            wait.assert_any_call("mock-start")
            vm.status.start.post.assert_called_once()
        vps.refresh_from_db()
        self.assertEqual((vps.status, vps.progress, vps.ip_address), ("Running", 100, "220.100.130.211"))
        vm.config.set.assert_any_call(
            ipconfig0="ip=220.100.130.211/24,gw=220.100.130.254",
            nameserver="8.8.8.8",
        )


@override_settings(BILLING_API_SECRET="test-only-shared-secret")
class ReservationCommitTests(TransactionTestCase):
    def test_reservation_is_committed_before_provisioning_and_retry(self):
        payload = {"order_id": 123, "name": "reserved", "cpu": 2, "ram": 4,
                   "storage": 50, "os": "Ubuntu 26.04", "billing_cycle": "MONTHLY", "plan": "Starter"}
        auth = {"HTTP_AUTHORIZATION": "Bearer test-only-shared-secret"}

        def provision(config, token, *, reserved_vps):
            from django.db import connection
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(VPS.objects.filter(billing_order_id=123).exists())
            # A retry arriving while the first request is still provisioning
            # must see the reservation and never enter this helper again.
            retry = Client().post("/api/internal/provision/", json.dumps(payload),
                                  content_type="application/json", **auth)
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json()["vps_id"], reserved_vps.pk)
            return reserved_vps

        with patch("vps.internal_api._create_and_start_clone", side_effect=provision) as helper:
            response = self.client.post("/api/internal/provision/", json.dumps(payload),
                                        content_type="application/json", **auth)
            self.assertEqual(response.status_code, 202)
            helper.assert_called_once()
        self.assertEqual(VPS.objects.count(), 1)


class StaticIPAllocationTests(TestCase):
    def test_allocator_skips_reserved_addresses(self):
        self.assertEqual(next_available_ip([]), "220.100.130.211")
        self.assertEqual(next_available_ip(["220.100.130.211"]), "220.100.130.212")

    def test_allocator_fails_closed_when_pool_is_exhausted(self):
        with self.assertRaises(IPPoolExhausted):
            next_available_ip([
                "220.100.130.211",
                "220.100.130.212",
                "220.100.130.213",
            ])

    def test_non_placeholder_vps_ips_are_unique(self):
        VPS.objects.create(name="one", ip_address="220.100.130.211")
        with self.assertRaises(IntegrityError), transaction.atomic():
            VPS.objects.create(name="two", ip_address="220.100.130.211")
        VPS.objects.create(name="placeholder-one")
        VPS.objects.create(name="placeholder-two")
