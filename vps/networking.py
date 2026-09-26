import ipaddress

from django.conf import settings


class IPPoolExhausted(RuntimeError):
    pass


def next_available_ip(reserved_addresses=()):
    """Return the first unreserved IPv4 in the configured dedicated VPS pool."""
    reserved = set()
    for value in reserved_addresses:
        try:
            address = ipaddress.ip_address(str(value))
        except ValueError:
            continue
        if address.version == 4 and str(address) != "0.0.0.0":
            reserved.add(address)

    start = settings.VPS_IP_POOL_START
    end = settings.VPS_IP_POOL_END
    for number in range(int(start), int(end) + 1):
        candidate = ipaddress.ip_address(number)
        if candidate not in reserved:
            return str(candidate)
    raise IPPoolExhausted("No customer VPS IP addresses are available.")
