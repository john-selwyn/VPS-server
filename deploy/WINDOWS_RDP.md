# Windows 11 provisioning and public RDP

Recorded 2026-10-10. Production configuration and connection reports were
verified on 2026-10-08; the user reran the original 41 tests on 2026-10-10.

## Purpose and verified environment

The provisioner clones Windows template 106, generates an encrypted Admin
password, configures the reserved network, and publishes a confirmed RDP
endpoint through the same unique port registry used by Linux SSH.

| Component | Verified location/configuration |
| --- | --- |
| Django provisioner | webserver 10.50.0.21, `/home/ict-tech`, venv `/home/ict-tech/venv` |
| API | `vps-api.service`, active after restart |
| Power worker | `vps-power-worker.service`, active when inspected |
| Proxmox | vps01 10.50.0.10, Windows template 106 |
| Clone storage | `vps-storage` (captured live source; replaces `local-lvm` for all clone requests) |
| Customer network | `VPS_BRIDGE=vmbr1`, 10.60.0.0/24, gateway 10.60.0.1 |
| Shared relay | bind 10.50.0.10, TCP public ports 22000–22999, guest RDP 3389 |
| Customer test | order 20, VM 1020, IP 10.60.0.19, public RDP `proxmoxportal.dyndns.org:22000` |

Order 20 used VPS Standard: 4 CPUs, 4 GB RAM, 80 GB disk. Website payment was
confirmed. After a first-boot timeout and guarded recovery, billing returned
ACTIVE. The customer logged into Windows and reported shutdown/start/reconnection
working. A fresh order has not yet demonstrated the longer verification deadline.

## Configuration and dependencies

Set these on the provisioner in `/home/ict-tech/.env` without committing the file:

```dotenv
VPS_TEMPLATE_WINDOWS_11_VMID=106
VPS_BRIDGE=vmbr1
PUBLIC_SSH_ENABLED=True
```

`WINDOWS_PASSWORD_ENCRYPTION_KEY` must be a valid Fernet key and must remain
private and stable for existing encrypted credentials. Generate it only if no
key exists; never replace it during deployment. Back up the key securely with
the database. It is not a value to publish in an example file or PR.

The exported server patch referenced `settings.VPS_BRIDGE` but did not include
its settings definition. This branch adds an environment-backed definition,
default vmbr1, and validates the interface name. The live site uses vmbr1.
Confirm bridge/storage availability and Proxmox API permissions before deploying
this branch elsewhere. Bridge changes preserve the VM's MAC and other net0 options.

Cryptography is recorded in requirements. Migration
`0010_vps_windows_password_encrypted` adds the encrypted field and was already
applied on the live server. Minimum Windows resources: 2 CPUs, 4 GB RAM, 64 GB disk.
Windows internal requests reject SSH credential fields. Linux retains SSH behavior.

Cloudbase-Init, the guest agent, template firmware/TPM configuration, Proxmox
access, PostgreSQL, router forwarding and the updated relay are dependencies.
Passwords are configured before starting the clone. Sensitive password helper/API
locals are excluded from Django technical error reports in this branch.

## API and relay contract

The provisioner returns `ssh_access: null` and `rdp_access` for Windows. A ready
RDP endpoint has exactly host, port, and username Admin. Status/runtime responses
do not include the password. The authenticated GET route
`/api/internal/v1/vps/<billing_order_id>/windows-credentials/` returns the initial
password only for a running Windows VPS with a ready relay; responses use no-store.
Billing must enforce customer ownership and verified payment before calling it.

The relay request includes `protocol: rdp`, username Admin, VMID, order ID, guest
IP and public port. It never contains a password. Linux omits protocol and keeps
the existing SSH request shape. Responses must exactly match the request plus
`ready: true`. RDP access shares the port allocator with SSH; do not recycle a
reserved port or bypass its ownership records.

### Live relay prerequisite: preserve the deployed helper

The exact live helper was supplied on 2026-10-10 and is now captured byte-for-byte
in `deploy/proxmox_ssh_relay.py`, superseding the SSH-only version. No production
helper replacement was needed. Preserve the policy, claims registry and existing
service units during deployment.

Live paths on Proxmox:

- `/usr/local/sbin/vps-ssh-relay`: observed optional protocol, Admin/win11 checks,
  3389 targets and protocol-specific systemd unit names.
- `/etc/vps-ssh-relay.json`: policy with bind 10.50.0.10, guest network
  10.60.0.0/24, bridge vmbr1, port range 22000–22999.
- `/var/lib/vps-ssh-relay/claims.json`: ownership registry; preserve it.
- `/etc/systemd/system/vps-<vmid>-rdp-relay.service`: generated relay units.

EdgeRouter rules observed: WAN_IN 32 accepts new TCP to Proxmox at the port
range; DNAT 3005 forwards WAN eth1 public 124.106.26.76 to 10.50.0.10; DNAT 3006
and source masquerade 5006 handle switch0 clients in 10.50.0.0/19. Verify the
current public IP, interfaces and rules instead of blindly reapplying old values.

## Verification and deployment

On webserver (read-only):

```sh
cd /home/ict-tech
git branch --show-current
git status --short
./venv/bin/python manage.py check
./venv/bin/python manage.py shell -c 'from vps.proxmox import get_template_vmid; print(get_template_vmid("Windows 11"))'
```

Expected: clean intended source branch, no system check issues, template 106.
The server already has a local `codex/windows-rdp` commit `03fea59`, based on its
local merge `d5a4cc1`. This GitHub branch is rebased on the remote shared-IP branch
using the exported source patch and includes additional settings/tests/docs.
The histories differ: preserve the live branch, fetch and compare before choosing
a reconciliation/deployment procedure. Do not force-push, hard-reset production,
or blindly pull unrelated changes.

For a reviewed deployment (mutates environment/database/process):

```sh
cd /home/ict-tech
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python manage.py migrate vps
./venv/bin/python manage.py check
sudo systemctl restart vps-api.service
systemctl is-active vps-api.service
```

Apply only after source reconciliation, environment backup, and database backup.
The checked-in source/migration must not overwrite the server's private .env or
SSH manager key. The original migration should already be recorded as applied.

On Proxmox (read-only):

```sh
qm status 1020
qm agent 1020 ping
qm guest cmd 1020 network-get-interfaces
nc -nvz -w 5 10.60.0.19 3389
grep -nE 'rdp|3389|protocol' /usr/local/sbin/vps-ssh-relay
cat /etc/vps-ssh-relay.json
```

Expected: running VM, agent response, reserved IP, open RDP TCP and updated
helper/policy. Then use the customer portal's initial-password reveal and verify
an authenticated public RDP desktop session. A TCP-open test alone is insufficient.
Reconnect to an existing Linux SSH guest as a regression check; this live check
is not yet recorded after the Windows milestone.

## Resolved 98% timeout and prevention

The initial 120-second IP verifier failed during Windows first boot. The VM was
observed installing updates and later had the correct IP. Updates/readiness delay
is the probable cause; exact timing was not captured. Existing VM 1020 was
recovered after independently verifying runtime, agent network, and RDP readiness.
Its model was conditionally updated from the specific network-verification failure
to Running, then `sync_access` confirmed the relay. On billing-server:

```sh
cd /opt/billing && ./venv/bin/python manage.py sync_provisioning 20
```

Actual output: `Order 20: ACTIVE; provisioning: ACTIVE.` Payment and VM were
preserved; no new order or clone was required. This status-sync command updates
billing records and must target the existing paid order.

The worker now waits 600 seconds for win11 and 120 for Linux, still requiring the
reserved IP. Live backup: `provision_vps.py.before-windows-network-timeout`.
The user checked syntax/system settings and restarted the API. An expired
deadline still leaves the VPS Failed and does not publish ready access; tests cover
that behavior. Wait for first-boot updates; do not repeatedly force-stop the guest.

Rollback: preserve the current source and compare the timeout backup before
reverting only the changed deadline; system-check and restart the API. Restoring
120 seconds can reintroduce early failures. Do not reset a recovered paid order,
drop encrypted fields, delete the VM, replace the encryption key, or overwrite
the live relay registry as part of a timeout rollback. Retain `.before-*` files and
private environment backups outside source control.

## Validation limits

The user ran the original 41 SQLite tests on Linux successfully with the live
timeout change. Local validation also covers Windows worker credentials, bridge
selection, 600-second verification, failure behavior, and SSH/RDP request shape.
The local Windows runtime does not validate Linux file ownership/mode or systemd;
the Linux SSH/relay permission tests require a Linux test environment. PostgreSQL
locking and subsequent Windows first-boot completion require separate verification.


## Relay capture validation - 2026-10-10

The uploaded helper matches the captured source byte-for-byte and parses as Python.
Nine relay behavior tests passed locally with Linux locking/atomic file writes
mocked and root trust checks mocked by the test fixtures. These verify SSH service
adoption, invalid targets, RDP Admin/win11 restrictions, port 3389 forwarding,
idempotency, and failed readiness probes. The Linux permission and parallel-claim
tests were excluded from this Windows run. On Linux run:

```sh
python3 -m unittest deploy.test_proxmox_ssh_relay
```

No live service, policy or claims registry was modified during capture. An
unreachable duplicate raise in the supplied helper is retained to preserve the
source; it does not change request handling.


## Linux validation completed - 2026-10-10

User tested detached checkout `319824a` at `/home/ict-tech/windows-rdp-review`
using `/home/ict-tech/venv/bin/python`. All 44 tests in `vps.tests`,
`vps.test_public_ssh` and `vps.test_windows` passed against an in-memory SQLite
DB; system checks reported no issues and migration consistency reported no
changes. `python -m unittest deploy.test_proxmox_ssh_relay` passed all 11 tests,
including permission and parallel-claim tests excluded from the Windows run.
This supersedes earlier pending Linux-test notes. Test fixtures still mock
Proxmox/systemd/network operations and root trust; actual production service
ownership and startup are not established by unit tests. PostgreSQL concurrency
and a fresh Windows order with the longer deadline remain unverified.

No production checkout switch, merge or service restart was performed. Next:
compare the clean live commit `03fea59` with the reviewed checkout, preserving
site settings and the existing encryption key before choosing a deployment path.
