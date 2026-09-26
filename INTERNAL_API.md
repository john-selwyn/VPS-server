# Internal billing provisioning API

Set a strong `BILLING_API_SECRET` (minimum 32 non-whitespace characters) in the
provisioning server process environment and use the same secret on the billing
server. Missing/weak secrets fail application startup. Also set `DJANGO_SECRET_KEY`
(minimum 50 non-whitespace characters), explicit `DJANGO_ALLOWED_HOSTS`, the
`VPS_DB_*` database variables, `PROXMOX_NODE`, and `VPS_TEMPLATE_VMID`. Apply
migration `0005_vps_billing_order_id` before enabling the integration.

Send `Authorization: Bearer <secret>` and `Content-Type: application/json` to
`POST /api/internal/provision/` with exactly these fields:

```json
{"order_id":123,"name":"customer-vps","cpu":2,"ram":4,"storage":50,"os":"Ubuntu 26.04","billing_cycle":"MONTHLY","plan":"VPS Starter"}
```

Order IDs are positive signed 64-bit integers; resources are positive signed
32-bit integers (RAM and storage in GB). Text fields must be nonempty and fit
their model limits. OS, plan and billing cycle are descriptive metadata; actual
template selection remains the existing provisioning workflow. Pricing fields
and other unknown fields are rejected.

New requests return 202; retries return 200 with the existing VPS. Inspect
`success` and `status` for failures. Authentication failures return 401, invalid
bodies return 400, and unsupported methods return 405 (after authentication).
The first request's configuration wins. Retries never modify or reprovision it.

Poll `GET /api/internal/provision/123/status/` using the same authorization.
Unknown orders return 404. Responses contain only the billing order ID, VPS ID,
VMID, status, progress, current step, IP address and a public error message.
Detailed provisioning exceptions remain internal.

The billing order reservation commits before clone submission. Concurrent
requests are constrained by the database unique key and only the creator may
provision. A process crash can leave a reserved order pending; an ambiguous
Proxmox failure can leave an existing VM. These require operator reconciliation,
not automatic resubmission or deleting the order reservation.

Integration work: configure the shared secret, apply the migration, use HTTPS
or a protected encrypted transport, and restrict ingress for these internal
routes to VM200 (220.100.130.191) at the firewall/reverse proxy. Configure VM200
to persist order IDs, retry with the same ID, and poll status. No deployment or
network configuration is performed by this change.

Local verification uses an isolated in-memory database:

```powershell
$env:DJANGO_SETTINGS_MODULE = "vps_portal.test_settings"
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

The tests mock VMID allocation, cloning and worker startup; they never start a VM.

## VM100 power protocol v1

VM200 continues to enforce its existing customer ownership and subscription
policy. VM100 authenticates the service using the same bearer secret and resolves
the billing Order ID to the VPS record. No request may supply a VMID or node.
Use verified HTTPS and restrict internal ingress to VM200.

Apply migration `0006_poweroperation` before enabling this protocol. Start a
supervised, automatically restarted worker alongside the web service:

```sh
python manage.py process_power_operations
```

The database is the durable queue: admission never depends on starting a child
process or successfully delivering a broker message. The command also accepts
`--once` for a single recovery/submission/poll pass. Run the continuous worker in
production and monitor pending age, unknown operations, and worker availability.
Multiple worker processes are supported; claims use a conditional database update.
Production admission/concurrency requires PostgreSQL.

### Admission

```http
POST /api/internal/v1/vps/321/power/
Authorization: Bearer <internal secret>
Idempotency-Key: <canonical UUID generated and persisted by VM200>
Content-Type: application/json

{"action":"shutdown"}
```

Only `start`, `shutdown`, and `reboot` are accepted, as case-sensitive strings.
Extra fields, duplicate JSON keys, query parameters, and non-object bodies are
rejected. Order IDs must be positive signed 64-bit integers. Keys are globally
unique for this single-service credential and must never be reused for another
order/action. Keep operation/key records indefinitely (including failures and
no-ops); deleting them would make an old request executable again.

Admission locks the VPS row in a short transaction. Replays are checked before
readiness and other operations: identical key/order/action returns the original
operation's current representation, including terminal or unknown outcomes.
A different payload with that key returns `409 IDEMPOTENCY_CONFLICT`. Another
pending/running/unknown operation returns `409 OPERATION_IN_PROGRESS`. Different
keys for the same action also conflict. The old unversioned power route is removed.

New admissions return `202` with a pending operation. This acknowledges durable
reservation, not Proxmox acceptance. Runtime validation happens in the worker;
invalid runtime transitions become terminal failed operations with `INVALID_STATE`.
The worker uses these rules:

| Observation | start | shutdown | reboot |
| --- | --- | --- | --- |
| stopped | submit | successful no-op | fail |
| running | successful no-op | submit | submit |
| paused/suspended/unknown | fail | fail | fail |

Provisioning `VPS.status` remains a readiness marker; runtime actions do not
rewrite it. VM100 snapshots the VMID from its database and resolves the VM's current
Proxmox cluster node before a power side effect, so a migrated VM is not controlled
through a stale hard-coded node. Do not reassign/delete/reuse a VM identity while an
operation remains unresolved.
External Proxmox administrators are outside the database reservation mechanism.

### Operation responses

Poll the authenticated `Location` header:

```http
GET /api/internal/v1/vps/321/power-operations/<operation_id>/
Authorization: Bearer <internal secret>
```

Admission, replay and polling use exactly these JSON fields:

```json
{
  "version": 1,
  "billing_order_id": 321,
  "operation_id": "f86bda46-4250-47f4-8948-ab6d16fb256f",
  "action": "shutdown",
  "status": "pending",
  "result": null,
  "observed_state": "unknown",
  "observed_at": null,
  "error": null
}
```

- `status`: `pending`, `running`, `succeeded`, `failed`, `unknown`.
- `result`: `executed` or `noop` only on success; otherwise null.
- `observed_state`: `running`, `stopped`, `paused`, `suspended`, `unknown`.
- `observed_at`: ISO-8601 timestamp with timezone, or null before observation.
- `error`: null or a fixed `{ "code": "...", "message": "..." }` object.

Unresolved statuses return `202`, terminal statuses `200`. All responses have
`Cache-Control: no-store`; unresolved operation responses have `Retry-After: 2`.
Observations are timestamped snapshots, never promises about the VM's current
state. `running` means the operation is being processed; it does not mean the VM
is running or guarantee submission acceptance. `succeeded/executed` requires a
valid task UPID, successful task completion, and a fresh expected-state observation.
A running VM by itself cannot prove reboot success. UPIDs, VMIDs and nodes are private.

Errors before reservation have exactly `version`, `billing_order_id` (null when
unavailable), `error`, and `active_operation_id` (null except operation conflicts).
HTTP codes: 400 `INVALID_REQUEST`, 401 `UNAUTHORIZED`, 404 `NOT_FOUND`,
405 `METHOD_NOT_ALLOWED`, 409 `VPS_NOT_READY`/`IDEMPOTENCY_CONFLICT`/
`OPERATION_IN_PROGRESS`, 500 `INTERNAL_ERROR`, 502 `RUNTIME_UNAVAILABLE`.
Operation errors additionally include `INVALID_STATE`, `SUBMISSION_UNKNOWN`,
`TASK_STATUS_UNAVAILABLE`, `TASK_FAILED`, and `STATE_UNCONFIRMED`.
VM200 must validate field types/enums, order/operation identifiers and status/result
combinations. Never forward arbitrary upstream text into customer responses.

### Runtime observations

`GET /api/internal/v1/vps/321/` returns exactly `version`, `billing_order_id`,
`state`, `observed_at`, `ip_address`. The existing unversioned runtime route remains
an alias with this safe schema (its old `vmid` field has been removed). Only the
mapped VM is queried. Proxmox status/QMP types are validated; contradictory or
unrecognized states fail closed as `unknown`. Structural failures return sanitized
502 errors. The IP is the stored provisioning IP, not a fresh guest-agent lookup.

### Submission, graceful behavior, and recovery

Shutdown submits `timeout=120, forceStop=0`. Reboot submits `timeout=120` without
stop/reset fallback. Verify these parameters against the installed Proxmox API
before rollout; unsupported parameters are never retried with weaker semantics.
The HTTP timeout (30 seconds) is independent of the guest/task timeout.

The worker commits a claim, reads runtime state, and commits a submission marker
before sending a command. No external call runs inside a database transaction.
An exception or malformed/wrong-target UPID during submission becomes `unknown`.
If a worker disappears without persisting a UPID, recovery quarantines its claim
after two minutes. It never reclaims/re-executes that command, even if it crashed
before sending. A late original worker can attach its validated UPID to permit
polling of that same task. Recovery fences a checker that has not started submission.

Known UPIDs are polled after worker restarts and temporary read failures. Polling
uses a durable per-operation lease and retry schedule so multiple workers do not
poll the same task concurrently and transient Proxmox failures back off. A failed
Proxmox task (including graceful shutdown timeout) becomes `unknown/TASK_FAILED`;
raw exit text is discarded and no automatic re-poll/resubmission is performed for
that failed task. Successful tasks with unconfirmed runtime state remain unknown and
are re-observed. Unknown operations require operator reconciliation and continue
blocking new commands. Before any manual resolution,
stop/fence the original submitter and establish the actual task outcome; age, VM
state alone, or lease expiry is not proof that a command was never submitted.

After any VM200 timeout, broken connection, 5xx, or invalid response, retry with
the SAME idempotency key/order/action or poll the known operation. Never generate
a replacement key automatically. Even an unknown operation's repeated POST is
only a read of the original operation.

### TLS and verification

Proxmox certificate verification is enabled. Set `PROXMOX_CA_BUNDLE` to a local
trusted CA PEM path when the system trust store does not trust the Proxmox
certificate. `PROXMOX_NODE` and `VPS_TEMPLATE_VMID` are deployment environment
configuration rather than source constants. Start/shutdown UPID validation accepts
Proxmox's normal `qmstart`/`qmshutdown` workers and HA-managed `hastart`/`hastop`
workers; reboot remains `qmreboot`. There is no insecure verification switch. No
certificate or secret belongs in the repository. Django DEBUG is disabled; internal
error middleware also prevents debug/HTML tracebacks from escaping if DEBUG is
enabled elsewhere.

The full local suite uses the isolated SQLite settings above. PostgreSQL-only
concurrency cases explicitly skip there. To run them, install `psycopg[binary]`
and configure a dedicated LOCAL PostgreSQL role with test-database creation rights:

```powershell
$env:POWER_TEST_POSTGRES_DB = "vps_power_tests"
$env:POWER_TEST_POSTGRES_USER = "<local test role>"
$env:POWER_TEST_POSTGRES_PASSWORD = "<local test password>"
python manage.py test --settings=vps_portal.postgres_test_settings
```

The optional settings pin the database host to loopback and never inherit the
deployment database. `POWER_TEST_POSTGRES_PORT` defaults to 5432. Proxmox calls
are mocked; no VM is controlled by these tests. Application startup still requires
the four Proxmox environment variables; use dummy values for isolated test runs.
