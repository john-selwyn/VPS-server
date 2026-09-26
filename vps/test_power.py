"""Power protocol, worker recovery, and PostgreSQL concurrency regressions."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import PowerOperation, VPS
from .power import admit_power, execute_pending, poll_operation, recover_interrupted_submissions
from .proxmox import InvalidProxmoxResponse, get_power_task_status, get_vm_state, resolve_vm_node, submit_power_action


def upid(action="shutdown", vmid=120, *, kind=None, node="test-1"):
    kind = kind or {"start": "qmstart", "shutdown": "qmshutdown", "reboot": "qmreboot"}[action]
    return f"UPID:{node}:00000001:00000002:00000003:{kind}:{vmid}:service@pve!power:"


class PowerFixtures:
    auth = {"HTTP_AUTHORIZATION": "Bearer test-only-shared-secret"}
    url = "/api/internal/v1/vps/321/power/"

    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.vps = VPS.objects.create(name="managed", billing_order_id=321, vmid=120, status="Running")
        self.other = VPS.objects.create(name="other", billing_order_id=322, vmid=121, status="Running")
        # No test may make a real Proxmox request, even if a helper mock is missed.
        patcher = patch("vps.proxmox.proxmox")
        self.proxmox = patcher.start()
        self.proxmox.nodes.side_effect = AssertionError("Unexpected Proxmox network call")
        self.addCleanup(patcher.stop)

    def post(self, action="shutdown", key=None, order=321, body=None, **headers):
        headers = {**self.auth, "HTTP_IDEMPOTENCY_KEY": str(key or uuid.uuid4()), **headers}
        return self.client.post(f"/api/internal/v1/vps/{order}/power/",
                                json.dumps({"action": action}) if body is None else body,
                                content_type="application/json", **headers)

    def reserve(self, action="shutdown", key=None):
        return admit_power(321, action, key or uuid.uuid4())


@override_settings(BILLING_API_SECRET="test-only-shared-secret")
class PowerAPITests(PowerFixtures, TestCase):
    def test_authentication_and_methods_for_all_endpoints(self):
        operation = self.reserve()
        routes = [(self.url, "post", "get"),
                  ("/api/internal/v1/vps/321/", "get", "post"),
                  (f"/api/internal/v1/vps/321/power-operations/{operation.pk}/", "get", "post")]
        for url, method, wrong in routes:
            for auth in ("", "Bearer wrong", "Basic test-only-shared-secret", "Bearer", "Bearer "):
                with self.subTest(url=url, auth=auth):
                    response = getattr(self.client, method)(url, HTTP_AUTHORIZATION=auth)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json()["error"]["code"], "UNAUTHORIZED")
                    self.assertEqual(response["Cache-Control"], "no-store")
            with override_settings(BILLING_API_SECRET=""):
                self.assertEqual(getattr(self.client, method)(url, **self.auth).status_code, 401)
            response = getattr(self.client, wrong)(url, **self.auth)
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response["Allow"], method.upper())

    def test_invalid_json_actions_keys_and_extra_fields(self):
        bodies = ["{", b"\xff", "null", "[]", "1", '"start"', "true",
                  '{"action":"start","action":"shutdown"}', "{}"]
        bodies += [json.dumps({"action": value}) for value in (None, True, False, 1, 1.5, [], {}, "", "destroy", "stop", "reset")]
        bodies += [json.dumps({"action": "start", field: value}) for field, value in (("vmid", 999), ("node", "other"), ("billing_order_id", 322))]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.post(body=body).status_code, 400)
        for key in ("", "invalid", "1", uuid.uuid4().hex):
            self.assertEqual(self.post(HTTP_IDEMPOTENCY_KEY=key).status_code, 400)
        self.assertEqual(self.client.post(self.url, {"action": "start"}, **self.auth).status_code, 400)
        self.assertEqual(self.client.post(self.url + "?vmid=999", '{"action":"start"}', content_type="application/json", **self.auth).status_code, 400)
        self.assertFalse(PowerOperation.objects.exists())

    def test_missing_and_nonready_orders(self):
        self.assertEqual(self.post(order=999).status_code, 404)
        for order in (0, 2**63):
            self.assertEqual(self.post(order=order).status_code, 400)
        for status, vmid in (("Provisioning", 120), ("Failed", 120), ("Running", None)):
            VPS.objects.filter(pk=self.vps.pk).update(status=status, vmid=vmid)
            self.assertEqual(self.post().status_code, 409)
        self.assertFalse(PowerOperation.objects.exists())

    def test_admission_mapping_and_strict_response(self):
        response = self.post()
        self.assertEqual(response.status_code, 202)
        operation = PowerOperation.objects.get()
        self.assertEqual((operation.vps_id, operation.target_vmid, operation.billing_order_id), (self.vps.pk, 120, 321))
        self.assertEqual(response.json(), {
            "version": 1, "billing_order_id": 321, "operation_id": str(operation.pk),
            "action": "shutdown", "status": "pending", "result": None,
            "observed_state": "unknown", "observed_at": None, "error": None,
        })
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Retry-After"], "2")
        self.assertEqual(self.client.get(response["Location"], **self.auth).json(), response.json())
        self.proxmox.nodes.assert_not_called()

    def test_replay_precedes_readiness_and_conflicts(self):
        key = uuid.uuid4()
        first = self.post(key=key)
        self.assertEqual(self.post(key=key).json(), first.json())
        VPS.objects.filter(pk=self.vps.pk).update(status="Failed")
        self.assertEqual(self.post(key=key).json(), first.json())
        for kwargs in ({"action": "reboot"}, {"order": 322}, {"order": 999}):
            response = self.post(key=key, **kwargs)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(PowerOperation.objects.count(), 1)

    def test_all_unresolved_statuses_block_new_keys_even_same_action(self):
        operation = self.reserve()
        for status in PowerOperation.UNRESOLVED:
            PowerOperation.objects.filter(pk=operation.pk).update(status=status)
            response = self.post()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "OPERATION_IN_PROGRESS")
            self.assertEqual(response.json()["active_operation_id"], str(operation.pk))

    def test_polling_scoped_to_order_terminal_status_and_replay(self):
        operation = self.reserve()
        url = f"/api/internal/v1/vps/321/power-operations/{operation.pk}/"
        self.assertEqual(self.client.get(url.replace("321", "322"), **self.auth).status_code, 404)
        self.assertEqual(self.client.get(url.replace(str(operation.pk), str(uuid.uuid4())), **self.auth).status_code, 404)
        for status, result in (("succeeded", "noop"), ("failed", None), ("unknown", None)):
            PowerOperation.objects.filter(pk=operation.pk).update(status=status, result=result)
            response = self.client.get(url, **self.auth)
            self.assertEqual(response.status_code, 202 if status == "unknown" else 200)
            self.assertEqual(self.post(key=operation.idempotency_key).json(), response.json())

    def test_runtime_safe_schema_and_upstream_failure(self):
        for state in PowerOperation.STATES:
            with patch("vps.internal_api.resolve_vm_node", return_value="node-b") as resolve, \
                    patch("vps.internal_api.get_vm_state", return_value=state) as get:
                response = self.client.get("/api/internal/v1/vps/321/", **self.auth)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.json()), {"version", "billing_order_id", "state", "observed_at", "ip_address"})
            self.assertEqual(response.json()["state"], state)
            resolve.assert_called_once_with(120)
            get.assert_called_once_with(120, node="node-b")
        with patch("vps.internal_api.resolve_vm_node", side_effect=RuntimeError("private token and node")), \
                patch("vps.internal_api.get_vm_state") as get:
            response = self.client.get("/api/internal/v1/vps/321/", **self.auth)
        get.assert_not_called()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.content.decode())
        self.assertEqual(self.client.get("/api/internal/v1/vps/999/", **self.auth).status_code, 404)
        VPS.objects.filter(pk=self.vps.pk).update(vmid=None)
        self.assertEqual(self.client.get("/api/internal/v1/vps/321/", **self.auth).status_code, 409)

    @override_settings(DEBUG=True)
    def test_debug_errors_are_sanitized_and_legacy_power_is_removed(self):
        with patch("vps.internal_api.admit_power", side_effect=RuntimeError("private password")):
            response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("private", response.content.decode())
        response = self.client.post("/api/internal/vps/321/power/", **self.auth)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")

    def test_database_constraints_protect_reservations(self):
        operation = self.reserve()
        fields = dict(vps=self.vps, billing_order_id=321, target_vmid=120, target_node="test-1", action="start")
        with self.assertRaises(IntegrityError), transaction.atomic():
            PowerOperation.objects.create(idempotency_key=uuid.uuid4(), **fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PowerOperation.objects.create(idempotency_key=operation.idempotency_key, status="failed", **fields)
        for fields in ({"action": "stop"}, {"status": "invalid"}, {"result": "executed"}, {"status": "succeeded", "result": None}, {"observed_state": "invalid"}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                PowerOperation.objects.filter(pk=operation.pk).update(**fields)

    def test_private_task_and_unrecognized_error_text_never_leave_api(self):
        operation = self.reserve()
        PowerOperation.objects.filter(pk=operation.pk).update(
            status="unknown", task_upid=upid(), error_code="private-upstream-password",
        )
        response = self.post(key=operation.idempotency_key)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["error"]["code"], "INTERNAL_ERROR")
        for private in ("UPID", "vmid", "target_node", "test-1", "private-upstream-password"):
            self.assertNotIn(private, response.content.decode())


class ProxmoxPowerTests(SimpleTestCase):
    def setUp(self):
        patcher = patch("vps.proxmox.proxmox")
        self.proxmox = patcher.start()
        self.addCleanup(patcher.stop)
        self.vm = self.proxmox.nodes.return_value.qemu.return_value

    def test_runtime_status_and_qmp_validation(self):
        cases = [({"status": state}, state) for state in PowerOperation.STATES]
        cases += [({"status": "running", "qmpstatus": qmp}, state) for qmp, state in
                  (("running", "running"), ("paused", "paused"), ("suspended", "suspended"), ("prelaunch", "unknown"), ("io-error", "unknown"))]
        cases += [({"status": "stopped", "qmpstatus": "running"}, "unknown"), ({"status": "stopped", "qmpstatus": "stopped"}, "stopped"), ({"status": "RUNNING"}, "unknown")]
        cases += [({"status": "stopped", "qmpstatus": "stopped", "lock": "suspended"}, "suspended"),
                  ({"status": "running", "lock": "backup"}, "unknown")]
        for payload, expected in cases:
            self.vm.status.current.get.return_value = payload
            self.assertEqual(get_vm_state(120), expected)
        for payload in (None, [], "running", {}, {"status": None}, {"status": []}, {"status": "running", "qmpstatus": None}, {"status": "running", "qmpstatus": {}}):
            self.vm.status.current.get.return_value = payload
            with self.assertRaises(InvalidProxmoxResponse):
                get_vm_state(120)

    def test_only_graceful_power_endpoints_and_valid_upids(self):
        for action in PowerOperation.ACTIONS:
            getattr(self.vm.status, action).post.return_value = upid(action)
            self.assertEqual(submit_power_action(120, action), upid(action))
        self.vm.status.start.post.assert_called_once_with()
        self.vm.status.shutdown.post.assert_called_once_with(timeout=120, forceStop=0)
        self.vm.status.reboot.post.assert_called_once_with(timeout=120)
        self.vm.status.stop.post.assert_not_called()
        self.vm.status.reset.post.assert_not_called()

    def test_ha_power_upids_are_accepted_only_for_matching_actions(self):
        self.vm.status.start.post.return_value = upid("start", kind="hastart")
        self.assertEqual(submit_power_action(120, "start"), upid("start", kind="hastart"))
        self.vm.status.shutdown.post.return_value = upid("shutdown", kind="hastop")
        self.assertEqual(submit_power_action(120, "shutdown"), upid("shutdown", kind="hastop"))
        self.vm.status.reboot.post.return_value = upid("reboot", kind="hastop")
        with self.assertRaises(InvalidProxmoxResponse):
            submit_power_action(120, "reboot")

    def test_resolve_vm_node_requires_one_valid_qemu_match(self):
        resources = self.proxmox.cluster.resources.get
        resources.return_value = [
            {"type": "qemu", "vmid": 119, "node": "node-a"},
            {"type": "qemu", "vmid": 120, "node": "node-b"},
            {"type": "lxc", "vmid": 120, "node": "node-c"},
        ]
        self.assertEqual(resolve_vm_node(120), "node-b")
        resources.assert_called_once_with(type="vm")
        for payload in (None, {}, [], [{"type": "qemu", "vmid": 120}],
                        [{"type": "qemu", "vmid": 120, "node": "bad node"}],
                        [{"type": "qemu", "vmid": 120, "node": "a"}, {"type": "qemu", "vmid": 120, "node": "b"}]):
            resources.return_value = payload
            with self.assertRaises(InvalidProxmoxResponse):
                resolve_vm_node(120)

    def test_malformed_missing_or_wrong_target_task_is_rejected(self):
        for value in (None, "", {}, [], True, "mock-task", upid("reboot"), upid(vmid=999), upid().replace("test-1", "other")):
            self.vm.status.shutdown.post.return_value = value
            with self.assertRaises(InvalidProxmoxResponse):
                submit_power_action(120, "shutdown")

    def test_task_response_validation_and_shutdown_timeout(self):
        task = self.proxmox.nodes.return_value.tasks.return_value.status.get
        for payload, expected in (({"status": "running"}, "running"), ({"status": "stopped", "exitstatus": "OK"}, "succeeded"), ({"status": "stopped", "exitstatus": "shutdown timeout private details"}, "failed")):
            task.return_value = payload
            self.assertEqual(get_power_task_status(upid()), expected)
        for payload in (None, [], {}, {"status": "stopped"}, {"status": "stopped", "exitstatus": []}, {"status": "running", "exitstatus": "OK"}):
            task.return_value = payload
            with self.assertRaises(InvalidProxmoxResponse):
                get_power_task_status(upid())


@override_settings(BILLING_API_SECRET="test-only-shared-secret")
class PowerWorkerTests(PowerFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch("vps.power.resolve_vm_node", return_value="test-1")
        self.resolve_node = patcher.start()
        self.addCleanup(patcher.stop)

    def make_poll_due(self, operation):
        PowerOperation.objects.filter(pk=operation.pk).update(
            next_poll_at=timezone.now() - timedelta(seconds=1),
            poll_claimed_at=None,
        )

    def test_worker_command_submits_and_resumes_known_task(self):
        operation = self.reserve()
        with patch("vps.power.get_vm_state", return_value="running"), \
                patch("vps.power.submit_power_action", return_value=upid()) as submit, \
                patch("vps.power.get_power_task_status", return_value="running"):
            call_command("process_power_operations", once=True)
        submit.assert_called_once()
        operation.refresh_from_db()
        self.assertEqual(operation.status, "running")
        self.make_poll_due(operation)
        with patch("vps.power.get_vm_state", return_value="stopped"), \
                patch("vps.power.submit_power_action") as submit, \
                patch("vps.power.get_power_task_status", return_value="succeeded"):
            call_command("process_power_operations", once=True)
        submit.assert_not_called()
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.result), ("succeeded", "executed"))

    def test_worker_resolves_current_node_before_submission(self):
        operation = self.reserve()
        self.resolve_node.return_value = "node-b"
        with patch("vps.power.get_vm_state", return_value="running") as observe, \
                patch("vps.power.submit_power_action", return_value=upid(node="node-b")) as submit:
            execute_pending(operation.pk)
        operation.refresh_from_db()
        self.assertEqual(operation.target_node, "node-b")
        observe.assert_called_once_with(120, node="node-b")
        submit.assert_called_once_with(120, "shutdown", node="node-b")

    def test_successful_task_re_resolves_vm_node_for_final_state(self):
        operation = self.reserve("reboot")
        self.resolve_node.return_value = "node-a"
        with patch("vps.power.get_vm_state", return_value="running"), \
                patch("vps.power.submit_power_action", return_value=upid("reboot", node="node-a")):
            execute_pending(operation.pk)
        self.make_poll_due(operation)
        self.resolve_node.return_value = "node-b"
        with patch("vps.power.get_power_task_status", return_value="succeeded") as task, \
                patch("vps.power.get_vm_state", return_value="running") as observe:
            poll_operation(operation.pk)
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.result), ("succeeded", "executed"))
        task.assert_called_once_with(upid("reboot", node="node-a"), node="node-a")
        observe.assert_called_once_with(120, node="node-b")

    def test_changed_mapping_is_not_used_to_control_another_vm(self):
        operation = self.reserve()
        VPS.objects.filter(pk=self.vps.pk).update(vmid=999)
        with patch("vps.power.get_vm_state") as observe, patch("vps.power.submit_power_action") as submit:
            execute_pending(operation.pk)
        observe.assert_not_called()
        submit.assert_not_called()
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.error_code), ("failed", "VPS_NOT_READY"))

    def test_every_action_state_combination(self):
        for state in PowerOperation.STATES:
            for action in PowerOperation.ACTIONS:
                with self.subTest(state=state, action=action):
                    operation = self.reserve(action)
                    with patch("vps.power.get_vm_state", return_value=state), patch("vps.power.submit_power_action", return_value=upid(action)) as submit:
                        execute_pending(operation.pk)
                        execute_pending(operation.pk)
                    operation.refresh_from_db()
                    if state not in ("running", "stopped") or (state, action) == ("stopped", "reboot"):
                        self.assertEqual((operation.status, operation.error_code), ("failed", "INVALID_STATE"))
                        submit.assert_not_called()
                    elif (state, action) in (("running", "start"), ("stopped", "shutdown")):
                        self.assertEqual((operation.status, operation.result), ("succeeded", "noop"))
                        submit.assert_not_called()
                    else:
                        self.assertEqual((operation.status, operation.result, operation.task_upid), ("running", None, upid(action)))
                        submit.assert_called_once_with(120, action, node="test-1")
                    self.assertEqual(operation.observed_state, state)
                    self.assertIsNotNone(operation.observed_at)
                    # Simulate a confirmed terminal failure before the next case.
                    PowerOperation.objects.filter(pk=operation.pk, status="running").update(status="failed")

    def test_reservation_and_submission_marker_committed_before_network(self):
        operation = self.reserve()
        def observe(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(PowerOperation.objects.get(pk=operation.pk).status, "running")
            return "running"
        def submit(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertIsNotNone(PowerOperation.objects.get(pk=operation.pk).submission_started_at)
            self.assertEqual(self.post(key=operation.idempotency_key).json()["operation_id"], str(operation.pk))
            self.assertEqual(self.post(action="start").status_code, 409)
            return upid()
        with patch("vps.power.get_vm_state", side_effect=observe), patch("vps.power.submit_power_action", side_effect=submit):
            execute_pending(operation.pk)

    def test_submission_unknown_never_retried_and_keeps_reservation(self):
        for exception in (TimeoutError("private secret"), InvalidProxmoxResponse("private response")):
            operation = self.reserve()
            with patch("vps.power.get_vm_state", return_value="running"), patch("vps.power.submit_power_action", side_effect=exception) as submit:
                execute_pending(operation.pk)
                recover_interrupted_submissions()
                execute_pending(operation.pk)
                poll_operation(operation.pk)
                response = self.post(key=operation.idempotency_key)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "unknown")
            self.assertEqual(response.json()["error"]["code"], "SUBMISSION_UNKNOWN")
            self.assertNotIn("private", response.content.decode())
            self.assertEqual(self.post().status_code, 409)
            submit.assert_called_once()
            # Test-only operator resolution; production must first reconcile.
            PowerOperation.objects.filter(pk=operation.pk).update(status="failed")

    def test_crash_window_recovers_to_unknown_without_resubmit(self):
        operation = self.reserve()
        with patch("vps.power.get_vm_state", return_value="running"), patch("vps.power.submit_power_action", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                execute_pending(operation.pk)
        PowerOperation.objects.filter(pk=operation.pk).update(updated_at=timezone.now() - timedelta(minutes=3))
        recover_interrupted_submissions()
        with patch("vps.power.submit_power_action") as submit:
            execute_pending(operation.pk)
            call_command("process_power_operations", once=True)
        operation.refresh_from_db()
        self.assertEqual(operation.status, "unknown")
        submit.assert_not_called()

    def test_recovery_fences_slow_checker_but_accepts_original_late_upid(self):
        operation = self.reserve()
        def quarantine(*args, **kwargs):
            PowerOperation.objects.filter(pk=operation.pk).update(updated_at=timezone.now() - timedelta(minutes=3))
            recover_interrupted_submissions()
            return "running"
        with patch("vps.power.get_vm_state", side_effect=quarantine), patch("vps.power.submit_power_action") as submit:
            execute_pending(operation.pk)
        submit.assert_not_called()
        operation.refresh_from_db()
        self.assertEqual(operation.status, "unknown")
        PowerOperation.objects.filter(pk=operation.pk).update(status="failed")
        operation = self.reserve()
        def delayed_submit(*args, **kwargs):
            quarantine()
            return upid()
        with patch("vps.power.get_vm_state", return_value="running"), patch("vps.power.submit_power_action", side_effect=delayed_submit) as submit:
            execute_pending(operation.pk)
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.task_upid), ("running", upid()))
        submit.assert_called_once()

    def test_task_completion_requires_task_success_and_expected_observation(self):
        operation = self.reserve("reboot")
        with patch("vps.power.get_vm_state", return_value="running"), patch("vps.power.submit_power_action", return_value=upid("reboot")):
            execute_pending(operation.pk)
        for task_state, observed, expected in (("running", "running", "running"), ("succeeded", "unknown", "unknown"), ("succeeded", "running", "succeeded")):
            self.make_poll_due(operation)
            with patch("vps.power.get_power_task_status", return_value=task_state), patch("vps.power.get_vm_state", return_value=observed):
                poll_operation(operation.pk)
            operation.refresh_from_db()
            self.assertEqual(operation.status, expected)
        self.assertEqual(operation.result, "executed")
        with patch("vps.power.get_power_task_status") as task:
            poll_operation(operation.pk)
        task.assert_not_called()
        self.assertEqual(self.post(key=operation.idempotency_key, action="reboot").status_code, 200)

    def test_task_failure_and_poll_failure_are_distinct(self):
        operation = self.reserve()
        with patch("vps.power.get_vm_state", return_value="running"), patch("vps.power.submit_power_action", return_value=upid()):
            execute_pending(operation.pk)
        with patch("vps.power.get_power_task_status", side_effect=TimeoutError("private")):
            poll_operation(operation.pk)
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.error_code), ("unknown", "TASK_STATUS_UNAVAILABLE"))
        self.make_poll_due(operation)
        with patch("vps.power.get_power_task_status", return_value="failed"):
            poll_operation(operation.pk)
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.error_code, operation.result), ("unknown", "TASK_FAILED", None))
        self.assertEqual(self.post().status_code, 409)
        self.make_poll_due(operation)
        with patch("vps.power.get_power_task_status") as task:
            poll_operation(operation.pk)
        task.assert_not_called()

    def test_runtime_failure_before_submission_is_terminal(self):
        operation = self.reserve()
        with patch("vps.power.get_vm_state", side_effect=RuntimeError("private")), patch("vps.power.submit_power_action") as submit:
            execute_pending(operation.pk)
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.error_code), ("failed", "RUNTIME_UNAVAILABLE"))
        submit.assert_not_called()

    def test_shutdown_timeout_is_safe_terminal_failure_without_fallback(self):
        self.proxmox.nodes.side_effect = None
        node = self.proxmox.nodes.return_value
        vm = node.qemu.return_value
        vm.status.current.get.return_value = {"status": "running", "qmpstatus": "running"}
        vm.status.shutdown.post.return_value = upid()
        node.tasks.return_value.status.get.return_value = {
            "status": "stopped", "exitstatus": "shutdown timeout: private host and token",
        }
        operation = self.reserve()
        execute_pending(operation.pk)
        poll_operation(operation.pk)
        response = self.post(key=operation.idempotency_key)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "unknown")
        self.assertEqual(response.json()["error"]["code"], "TASK_FAILED")
        self.assertEqual(self.post(action="start").status_code, 409)
        self.assertNotIn("private", response.content.decode())
        vm.status.stop.post.assert_not_called()
        vm.status.reset.post.assert_not_called()

    def test_polling_lease_and_backoff_prevent_duplicate_polling(self):
        operation = self.reserve()
        with patch("vps.power.get_vm_state", return_value="running"), \
                patch("vps.power.submit_power_action", return_value=upid()):
            execute_pending(operation.pk)

        with patch("vps.power.get_power_task_status", return_value="running") as task:
            poll_operation(operation.pk)
            poll_operation(operation.pk)
        task.assert_called_once()

        operation.refresh_from_db()
        self.assertIsNotNone(operation.next_poll_at)
        self.assertIsNone(operation.poll_claimed_at)

        PowerOperation.objects.filter(pk=operation.pk).update(
            next_poll_at=timezone.now() - timedelta(seconds=1),
            poll_claimed_at=timezone.now(),
        )
        with patch("vps.power.get_power_task_status") as task:
            poll_operation(operation.pk)
        task.assert_not_called()

        PowerOperation.objects.filter(pk=operation.pk).update(
            next_poll_at=timezone.now() - timedelta(seconds=1),
            poll_claimed_at=timezone.now() - timedelta(minutes=2),
        )
        with patch("vps.power.get_power_task_status", return_value="running") as task:
            poll_operation(operation.pk)
        task.assert_called_once()

    def test_malformed_submission_response_quarantines_real_helper(self):
        self.proxmox.nodes.side_effect = None
        vm = self.proxmox.nodes.return_value.qemu.return_value
        vm.status.current.get.return_value = {"status": "running", "qmpstatus": "running"}
        vm.status.shutdown.post.return_value = None
        operation = self.reserve()
        execute_pending(operation.pk)
        response = self.post(key=operation.idempotency_key)
        execute_pending(operation.pk)
        self.assertEqual(response.json()["status"], "unknown")
        vm.status.shutdown.post.assert_called_once()


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks and independent connections")
@override_settings(BILLING_API_SECRET="test-only-shared-secret")
class PowerConcurrencyTests(PowerFixtures, TransactionTestCase):
    def race(self, requests):
        barrier = threading.Barrier(len(requests))
        def run(request):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                order = request.get("order", 321)
                return Client(enforce_csrf_checks=True).post(
                    f"/api/internal/v1/vps/{order}/power/",
                    json.dumps({"action": request.get("action", "shutdown")}),
                    content_type="application/json", **self.auth,
                    HTTP_IDEMPOTENCY_KEY=str(request.get("key", uuid.uuid4())),
                )
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            return list(pool.map(run, requests))

    def test_same_key_concurrent_requests_share_operation(self):
        key = uuid.uuid4()
        responses = self.race([{"key": key}, {"key": key}])
        self.assertEqual([r.status_code for r in responses], [202, 202])
        self.assertEqual(responses[0].json()["operation_id"], responses[1].json()["operation_id"])
        self.assertEqual(PowerOperation.objects.count(), 1)

    def test_different_actions_reserve_only_one_operation(self):
        responses = self.race([{"action": "shutdown"}, {"action": "reboot"}])
        self.assertEqual(sorted(r.status_code for r in responses), [202, 409])
        self.assertEqual(PowerOperation.objects.count(), 1)

    def test_global_key_collision_across_different_orders(self):
        key = uuid.uuid4()
        responses = self.race([{"key": key}, {"key": key, "order": 322}])
        self.assertEqual(sorted(r.status_code for r in responses), [202, 409])
        self.assertEqual(next(r for r in responses if r.status_code == 409).json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(PowerOperation.objects.count(), 1)

    def test_concurrent_workers_submit_once(self):
        operation = self.reserve()
        barrier = threading.Barrier(2)
        def worker(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                execute_pending(operation.pk)
            finally:
                close_old_connections()
        with patch("vps.power.resolve_vm_node", return_value="test-1"), \
                patch("vps.power.get_vm_state", return_value="running"), \
                patch("vps.power.submit_power_action", return_value=upid()) as submit:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(worker, range(2)))
        submit.assert_called_once()
