import ipaddress
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.env"))

BASE_DIR = Path(__file__).resolve().parent.parent


def _required_secret(name, minimum_length=32):
    value = os.environ.get(name, "").strip()
    if len(value) < minimum_length or any(char.isspace() for char in value):
        raise ImproperlyConfigured(
            f"{name} must contain at least {minimum_length} non-whitespace characters."
        )
    return value


def _required_database_env(name):
    value = os.environ.get(name, "")
    if not value.strip():
        raise ImproperlyConfigured(f"{name} must be configured and non-blank.")
    return value


def _required_ipv4(name):
    value = os.environ.get(name, "").strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a valid IPv4 address.") from exc
    if address.version != 4:
        raise ImproperlyConfigured(f"{name} must be an IPv4 address.")
    return address


def _required_prefix(name):
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer prefix length.") from exc
    if not 1 <= value <= 30:
        raise ImproperlyConfigured(f"{name} must be between 1 and 30.")
    return value


BILLING_API_SECRET = _required_secret("BILLING_API_SECRET")
SECRET_KEY = _required_secret("DJANGO_SECRET_KEY", minimum_length=50)
DEBUG = False

VPS_IP_POOL_START = _required_ipv4("VPS_IP_POOL_START")
VPS_IP_POOL_END = _required_ipv4("VPS_IP_POOL_END")
VPS_IP_PREFIX = _required_prefix("VPS_IP_PREFIX")
VPS_IP_GATEWAY = _required_ipv4("VPS_IP_GATEWAY")
VPS_IP_DNS = _required_ipv4("VPS_IP_DNS")

VPS_IP_NETWORK = ipaddress.ip_network(f"{VPS_IP_POOL_START}/{VPS_IP_PREFIX}", strict=False)
if VPS_IP_POOL_END not in VPS_IP_NETWORK or VPS_IP_GATEWAY not in VPS_IP_NETWORK:
    raise ImproperlyConfigured("VPS IP pool and gateway must be in the configured IPv4 network.")
if int(VPS_IP_POOL_END) < int(VPS_IP_POOL_START):
    raise ImproperlyConfigured("VPS_IP_POOL_END must not be lower than VPS_IP_POOL_START.")
if VPS_IP_POOL_START in {VPS_IP_NETWORK.network_address, VPS_IP_NETWORK.broadcast_address}:
    raise ImproperlyConfigured("VPS_IP_POOL_START cannot be the network or broadcast address.")
if VPS_IP_POOL_END in {VPS_IP_NETWORK.network_address, VPS_IP_NETWORK.broadcast_address}:
    raise ImproperlyConfigured("VPS_IP_POOL_END cannot be the network or broadcast address.")
if VPS_IP_GATEWAY >= VPS_IP_POOL_START and VPS_IP_GATEWAY <= VPS_IP_POOL_END:
    raise ImproperlyConfigured("VPS_IP_GATEWAY cannot be inside the customer VPS address pool.")
if int(VPS_IP_POOL_END) - int(VPS_IP_POOL_START) > 4095:
    raise ImproperlyConfigured("VPS IP pool may contain at most 4096 addresses.")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")
    if host.strip()
]
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must contain one or more explicit hosts and must not use '*'."
    )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "vps_portal",
    "vps",
]

MIDDLEWARE = [
    "vps.middleware.InternalAPIErrorMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "vps_portal.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "vps_portal" / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

WSGI_APPLICATION = "vps_portal.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _required_database_env("VPS_DB_NAME"),
        "USER": _required_database_env("VPS_DB_USER"),
        "PASSWORD": _required_database_env("VPS_DB_PASSWORD"),
        "HOST": _required_database_env("VPS_DB_HOST"),
        "PORT": os.environ.get("VPS_DB_PORT", "5432"),
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
