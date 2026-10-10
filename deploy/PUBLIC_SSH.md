# Shared public IP SSH access

Paid Linux VPS orders receive a unique TCP port on `proxmoxportal.dyndns.org`.
The provisioner reserves the port, asks a restricted Proxmox helper to create a
systemd relay, and publishes the endpoint only after both the guest and relay
respond with an SSH banner. The billing portal receives the endpoint through
its existing authenticated status/runtime API. A failed relay retries separately
and does not reverse a verified payment or mark a running VPS as failed.

The router forwards the range once. Purchases do not require router credentials
or a new router rule. A reserved port is never automatically recycled, even
after a failed attempt. Retiring a VPS and its port needs an explicit operator
procedure; this release does not implement deletion or port recycling.

The original release supports the existing Ubuntu/Debian SSH workflow. The
Windows restriction is superseded by [WINDOWS_RDP.md](WINDOWS_RDP.md). Dedicated
public IP or 1:1 NAT allocation still requires a separate implementation.

For Windows deployments, preserve the live RDP-capable helper. The historical
`deploy/proxmox_ssh_relay.py` below supports only SSH and must not replace it.

## Deployment order

Use `feature/shared-ip-ssh` in both `john-selwyn/proxmox-billing` and
`john-selwyn/VPS-server`. Keep the provisioner's `PUBLIC_SSH_ENABLED=False`
until the router, restricted key, existing reservation and worker are ready.

1. Preserve the deployed billing edits and inspect its working tree before
   merging the branch. The deployed `billing.0009_order_ssh_host_order_ssh_port`
   already adds the same two fields, so do not create another migration or fake
   it. Back up any locally generated migration before resolving an add/add
   conflict against the checked-in migration. Preserve unrelated local edits.
2. Deploy billing first: merge the branch, run `venv/bin/python manage.py migrate`
   and `venv/bin/python manage.py check`, then restart `billing.service`. The old
   provisioner does not erase manually configured SSH endpoints.
3. Deploy the provisioner branch in its actual existing directory. Run
   `venv/bin/python manage.py migrate` and `venv/bin/python manage.py check`.
   Identify the existing provisioner service's user, working directory, Python
   executable and source IP with `systemctl cat YOUR_PROVISIONER_SERVICE` and
   `ip route get 10.50.0.10`. Use those values below; do not use the billing
   server's paths or assume its IP is the provisioner's IP.
4. Install the helper/key and router range below. Reserve order #17 at port
   22015 **before enabling allocation**. Adopt every other existing public
   endpoint too; inventory existing relays and router rules first.
5. Enable the provisioner flag, restart its web service, and process order #17
   once. Confirm its reservation is ready, then enable the persistent worker.
6. Make one new test order using the existing approved test-payment procedure.
   Confirm its assigned port through an external port check and authenticate
   over SSH with that customer's private key. Check the same endpoint from the
   office LAN to confirm hairpin NAT. Code tests cannot validate the live router.

## Restricted key and helper

On the **provisioner**, as its actual service user, choose an absolute private
key path outside the repository and create the dedicated key:

```sh
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/vps-relay-manager
cat ~/.ssh/vps-relay-manager.pub
ip route get 10.50.0.10
```

The private key stays on the provisioner, mode 600. Copy the two files
`deploy/proxmox_ssh_relay.py` and `deploy/vps-ssh-relay.example.json` to
`/root/` on **Proxmox 10.50.0.10** using your existing administrative access.
As root on Proxmox:

```sh
apt-get install socat
install -o root -g root -m 0755 /root/proxmox_ssh_relay.py /usr/local/sbin/vps-ssh-relay
```

If `/etc/vps-ssh-relay.json` already exists, inspect and back it up before
updating it. Otherwise install the supplied policy:

```sh
install -o root -g root -m 0600 /root/vps-ssh-relay.example.json /etc/vps-ssh-relay.json
install -d -o root -g root -m 0700 /var/lib/vps-ssh-relay
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

The sample policy matches this deployment: bind `10.50.0.10`, guest subnet
`10.60.0.0/24`, bridge `vmbr1`, ports `22000-22999`. Adjust it only if the actual
deployment differs. The helper checks the VM's real `qm config`, matching its
name `customer-<customer id>-vps-<order id>`, cloud-init username, bridge and IP.
It refuses a conflicting existing service or port claim. Order #17's current
`vps-1016-ssh-relay.service` is adopted without rewriting or restarting it.

Append this **single line** to root's existing `/root/.ssh/authorized_keys`,
replacing the source IP and entire public key with the values just obtained.
Keep existing administrative keys:

```text
from="REPLACE_PROVISIONER_SOURCE_IP",restrict,command="/usr/local/sbin/vps-ssh-relay" ssh-ed25519 REPLACE_PUBLIC_KEY vps-relay-manager
```

Use directory mode 700 and authorized_keys mode 600. The key permits only this
forced command and no interactive shell, PTY, port forwarding or agent forwarding.

On the **provisioner**, collect the host key and compare its SHA256 fingerprint
with the independently displayed Proxmox fingerprint above before trusting it:

```sh
ssh-keyscan -T 5 -t ed25519 10.50.0.10 > ~/.ssh/vps-relay-known-hosts.candidate
ssh-keygen -lf ~/.ssh/vps-relay-known-hosts.candidate
```

After the fingerprints match, move the candidate to
`~/.ssh/vps-relay-known-hosts` and set mode 600. Enter the actual absolute paths
in the provisioner's existing environment configuration:

```dotenv
PUBLIC_SSH_ENABLED=False
PUBLIC_SSH_HOST=proxmoxportal.dyndns.org
PUBLIC_SSH_PORT_START=22000
PUBLIC_SSH_PORT_END=22999
PUBLIC_SSH_RELAY_HOST=10.50.0.10
PUBLIC_SSH_RELAY_KEY=/REPLACE/ABSOLUTE/PATH/.ssh/vps-relay-manager
PUBLIC_SSH_RELAY_KNOWN_HOSTS=/REPLACE/ABSOLUTE/PATH/.ssh/vps-relay-known-hosts
```

The three port ranges in the app, root policy and router must match. Confirm the
provisioner's existing IP pool remains within `10.60.0.0/24` with gateway
`10.60.0.1`; this change does not alter guest IP allocation.

## One-time EdgeRouter range

These example rules match the observed setup: WAN `eth1`, LAN `switch0`, public
IP `124.106.26.76`, LAN subnet `10.50.0.0/19`, relay host `10.50.0.10`.
Inspect existing rules first. Use different rule numbers if 3005, 3006, 5006 or
WAN_IN 32 are occupied. Keep the working 22015 rules and web-hosting rules.
Also check that no top-level `port-forward` rule overrides ports in this range.

In the **EdgeRouter CLI**:

```text
show configuration commands | match 'service nat rule'
show configuration commands | match 'firewall name WAN_IN'
show configuration commands | match 'port-forward'
configure
set service nat rule 3005 description 'VPS shared SSH range'
set service nat rule 3005 type destination
set service nat rule 3005 inbound-interface eth1
set service nat rule 3005 protocol tcp
set service nat rule 3005 destination address 124.106.26.76
set service nat rule 3005 destination port 22000-22999
set service nat rule 3005 inside-address address 10.50.0.10
set service nat rule 3006 description 'VPS shared SSH LAN hairpin'
set service nat rule 3006 type destination
set service nat rule 3006 inbound-interface switch0
set service nat rule 3006 protocol tcp
set service nat rule 3006 source address 10.50.0.0/19
set service nat rule 3006 destination address 124.106.26.76
set service nat rule 3006 destination port 22000-22999
set service nat rule 3006 inside-address address 10.50.0.10
set service nat rule 5006 description 'VPS shared SSH hairpin source NAT'
set service nat rule 5006 type masquerade
set service nat rule 5006 outbound-interface switch0
set service nat rule 5006 protocol tcp
set service nat rule 5006 source address 10.50.0.0/19
set service nat rule 5006 destination address 10.50.0.10
set service nat rule 5006 destination port 22000-22999
set firewall name WAN_IN rule 32 description 'VPS shared SSH range'
set firewall name WAN_IN rule 32 action accept
set firewall name WAN_IN rule 32 protocol tcp
set firewall name WAN_IN rule 32 destination address 10.50.0.10
set firewall name WAN_IN rule 32 destination port 22000-22999
compare
commit
save
exit
```

There is deliberately no `inside-address port`: address-only DNAT preserves
each customer's original destination port. If the CLI rejects any statement,
stop before committing and inspect the error. Verify the resulting NAT rules
preserve ports before enabling new orders. An unassigned port has no listener
and is expected to test closed. If the public IP changes, update the NAT
destination addresses along with dynamic DNS.

References: [Ubiquiti destination NAT](https://help.uisp.com/hc/en-us/articles/22591200002071-EdgeRouter-Destination-NAT)
and [hairpin NAT](https://help.uisp.com/hc/en-us/articles/22591184776983-EdgeRouter-Hairpin-NAT).

## Adopt order #17 and start the worker

In the **provisioner project directory**, using its virtual environment:

```sh
venv/bin/python manage.py reserve_public_ssh 17 --port 22015
```

This requires the existing record for order #17, VM 1016, guest 10.60.0.15 and
username/key to be present. It does not create another VPS or payment. If it
fails, resolve the mismatch before enabling automation. Reserve all other
existing public ports with the same command and their actual order IDs.

Change `PUBLIC_SSH_ENABLED=True`, restart the existing provisioner web service,
then verify the adopted relay:

```sh
venv/bin/python manage.py process_public_ssh --once --order 17
venv/bin/python manage.py shell -c 'from vps.models import PublicSSHAccess; print(list(PublicSSHAccess.objects.filter(vps__billing_order_id=17).values("host", "port", "status", "error_code")))'
```

Expected: host `proxmoxportal.dyndns.org`, port `22015`, status `ready`.
If status is failed, its fixed error code identifies the failed setup stage
without exposing credentials. For `SSH_RELAY_FAILED`, inspect the restricted
key, pinned host key, management connection and Proxmox helper separately.

Copy `deploy/vps-public-ssh-worker.service.example` to
`/etc/systemd/system/vps-public-ssh-worker.service` on the **provisioner**, replace
its user/directory/Python values with the existing deployment values, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now vps-public-ssh-worker.service
systemctl is-active vps-public-ssh-worker.service
sudo journalctl -u vps-public-ssh-worker.service -n 30 --no-pager
```

The web service and worker must use the same database and environment. After a
reboot, the worker resumes pending access jobs and each Proxmox relay starts
automatically. Failed jobs keep their port and retry after a delay. The API's
status/runtime GET requests never install a relay.

The customer's command is `ssh -p PORT vpsuser@proxmoxportal.dyndns.org`. A
custom private key filename requires `-i /path/to/their/private_key`; the portal
does not hard-code the operator's Windows key filename.

## Rollback

Disable the worker and set `PUBLIC_SSH_ENABLED=False`, then restart the
provisioner web service. Keep the port reservations and enabled existing relays
so customers retain their endpoints. Do not reverse the migrations, delete
claim files, reset assigned ports or disable the working 22015 rules. The
billing adapter accepts the previous API shape and keeps saved endpoints.

## Local validation

```sh
python manage.py test vps --settings=vps_portal.test_settings
python -m unittest deploy.test_proxmox_ssh_relay
python manage.py makemigrations --check --dry-run --settings=vps_portal.test_settings
```

All network, Proxmox, SSH and systemd boundaries are mocked in local tests.
Live router forwarding and customer SSH authentication remain deployment checks.
