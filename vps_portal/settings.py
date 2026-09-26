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


BILLING_API_SECRET = _required_secret("BILLING_API_SECRET")
SECRET_KEY = _required_secret("DJANGO_SECRET_KEY", minimum_length=50)
DEBUG = False

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
