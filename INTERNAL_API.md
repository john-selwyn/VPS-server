# Internal billing provisioning API

Set `BILLING_API_SECRET` in the provisioning server process environment and use
the same secret on the billing server. An unset or empty secret denies access.
Apply migration `0005_vps_billing_order_id` before enabling the integration.

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
