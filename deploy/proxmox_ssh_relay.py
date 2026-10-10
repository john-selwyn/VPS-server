#!/usr/bin/python3
"""Root-owned forced SSH command. No arbitrary commands or guest targets allowed."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time


class RelayError(Exception):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RelayError("DUPLICATE_FIELD")
        result[key] = value
    return result


def trusted_file(path):
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
        raise RelayError("UNSAFE_CONFIGURATION")


def load_policy():
    path = Path("/etc/vps-ssh-relay.json")
    trusted_file(path)
    data = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if set(data) != {"bind_ip", "guest_network", "bridge", "port_start", "port_end"}:
        raise RelayError("INVALID_POLICY")
    ipaddress.IPv4Address(data["bind_ip"])
    ipaddress.IPv4Network(data["guest_network"])
    if (not re.fullmatch(r"vmbr[0-9]+", data["bridge"])
            or type(data["port_start"]) is not int or type(data["port_end"]) is not int
            or not 1024 <= data["port_start"] <= data["port_end"] <= 65535):
        raise RelayError("INVALID_POLICY")
    return data


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise RelayError("RELAY_COMMAND_FAILED")
    return result.stdout


def probe_ssh(host, port):
    with socket.create_connection((host, port), timeout=3) as connection:
        connection.settimeout(3)
        banner = b""
        deadline = time.monotonic() + 3
        while len(banner) < 8:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RelayError("SSH_NOT_READY")
            connection.settimeout(remaining)
            chunk = connection.recv(512 - len(banner))
            if not chunk:
                break
            banner += chunk
        if not banner.startswith(b"SSH-2.0-"):
            raise RelayError("SSH_NOT_READY")


def probe_tcp(host, port):
    with socket.create_connection((host, port), timeout=3):
        pass


def atomic_write(path, content, mode):
    fd, temporary = tempfile.mkstemp(prefix=".relay-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), mode)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Relay:
    def __init__(self, policy, state=Path("/var/lib/vps-ssh-relay"),
                 units=Path("/etc/systemd/system"), runner=run, probe=probe_ssh):
        self.policy, self.state, self.units = policy, state, units
        self.run, self.probe = runner, probe

    def validate(self, request):
        fields = {"vmid", "order_id", "ip", "port", "username"}
        if not isinstance(request, dict) or set(request) not in (fields, fields | {"protocol"}):
            raise RelayError("INVALID_REQUEST")
        protocol = request.get("protocol", "ssh")
        if protocol not in ("ssh", "rdp"):
            raise RelayError("INVALID_REQUEST")
            raise RelayError("INVALID_REQUEST")
        for field, minimum, maximum in (("vmid", 100, 999999999), ("order_id", 1, 9223372036854775807)):
            if type(request[field]) is not int or not minimum <= request[field] <= maximum:
                raise RelayError("INVALID_REQUEST")
        if (type(request["port"]) is not int
                or not self.policy["port_start"] <= request["port"] <= self.policy["port_end"]
                or not isinstance(request["username"], str)
                or not re.fullmatch(
                    r"Admin" if protocol == "rdp" else r"[a-z_][a-z0-9_-]{0,31}",
                    request["username"])
                or request["username"] == "root"):
            raise RelayError("INVALID_REQUEST")
        try:
            ip = ipaddress.IPv4Address(request["ip"])
            network = ipaddress.IPv4Network(self.policy["guest_network"])
        except (ValueError, TypeError):
            raise RelayError("INVALID_REQUEST") from None
        if ip not in network or ip in {network.network_address, network.broadcast_address}:
            raise RelayError("INVALID_TARGET")
        config = {}
        for line in self.run(["/usr/sbin/qm", "config", str(request["vmid"])]).splitlines():
            key, separator, value = line.partition(": ")
            if separator:
                config[key] = value
        network_options = dict(item.split("=", 1) for item in config.get("net0", "").split(",") if "=" in item)
        ip_options = dict(item.split("=", 1) for item in config.get("ipconfig0", "").split(",") if "=" in item)
        try:
            configured_ip = ipaddress.IPv4Interface(ip_options.get("ip", "")).ip
        except ValueError:
            raise RelayError("VM_TARGET_MISMATCH") from None
        if (configured_ip != ip or str(ip) != request["ip"]
                or network_options.get("bridge") != self.policy["bridge"]
                or not re.fullmatch(rf"customer-[0-9]+-vps-{request['order_id']}", config.get("name", ""))
                or config.get("ciuser") != request["username"]
                or (protocol == "rdp" and config.get("ostype") != "win11")
                or (protocol == "ssh" and config.get("ostype", "").startswith("win"))
                or config.get("template") == "1"):
            raise RelayError("VM_TARGET_MISMATCH")

    def ensure(self, request):
        self.validate(request)
        protocol = request.get("protocol", "ssh")
        target_port = 3389 if protocol == "rdp" else 22
        probe = probe_tcp if protocol == "rdp" else self.probe
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        trusted_file(self.state)
        lock = self.state / "lock"
        with lock.open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            registry = self.state / "claims.json"
            if registry.exists():
                trusted_file(registry)
                claims = json.loads(registry.read_text(), object_pairs_hook=unique_object)
                if not isinstance(claims, dict):
                    raise RelayError("INVALID_REGISTRY")
            else:
                claims = {}
            key = str(request["vmid"])
            if key in claims and claims[key] != request:
                raise RelayError("VM_ALREADY_ASSIGNED")
            for vmid, claim in claims.items():
                if vmid != key and (claim["port"] == request["port"] or claim["ip"] == request["ip"]):
                    raise RelayError("TARGET_ALREADY_ASSIGNED")
            unit_name = f"vps-{request['vmid']}-{protocol}-relay.service"
            unit = self.units / unit_name
            command = (f"/usr/bin/socat TCP-LISTEN:{request['port']},bind={self.policy['bind_ip']},reuseaddr,fork "
                       f"TCP:{request['ip']}:{target_port}")
            if unit.exists():
                trusted_file(unit)
                starts = re.findall(r"(?m)^ExecStart=(.+)$", unit.read_text())
                if starts != [command]:
                    raise RelayError("SERVICE_ALREADY_ASSIGNED")
            # Persist ownership before any service change, including failed starts.
            claims[key] = request
            atomic_write(registry, json.dumps(claims, sort_keys=True) + "\n", 0o600)
            probe(request["ip"], target_port)
            if not unit.exists():
                content = ("[Unit]\nDescription=Public " + protocol.upper() + " relay for VPS " + str(request["vmid"]) +
                    "\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\n" +
                    "User=nobody\nGroup=nogroup\nExecStart=" + command +
                    "\nRestart=always\nRestartSec=3\nNoNewPrivileges=yes\nPrivateTmp=yes\n" +
                    "ProtectSystem=strict\nProtectHome=yes\nRestrictAddressFamilies=AF_INET AF_INET6\n" +
                    "TasksMax=128\nLimitNOFILE=512\n\n[Install]\nWantedBy=multi-user.target\n")
                atomic_write(unit, content, 0o644)
                self.run(["/usr/bin/systemctl", "daemon-reload"])
            # --now leaves an already running service and its connections intact.
            self.run(["/usr/bin/systemctl", "enable", "--now", unit_name])
            self.run(["/usr/bin/systemctl", "is-active", "--quiet", unit_name])
            probe(self.policy["bind_ip"], request["port"])
            return {**request, "ready": True}


def main():
    try:
        if os.geteuid() != 0 or os.environ.get("SSH_ORIGINAL_COMMAND") != "vps-ssh-relay":
            raise RelayError("FORCED_COMMAND_REQUIRED")
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise RelayError("INVALID_REQUEST")
        request = json.loads(raw, object_pairs_hook=unique_object)
        print(json.dumps(Relay(load_policy()).ensure(request)))
    except (RelayError, OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.TimeoutExpired):
        # Never return raw subprocess output or confidential host configuration.
        print("SSH_RELAY_FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
