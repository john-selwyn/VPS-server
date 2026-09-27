"""Opt-in PostgreSQL tests for row-lock and concurrency regressions.

These settings require a dedicated test-only PostgreSQL role/database and never
inherit the deployment database credentials from the normal settings module.
"""
import os

from django.core.exceptions import ImproperlyConfigured

from .test_settings import *  # noqa: F403,F401


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} must be configured for PostgreSQL tests.")
    return value


_db_name = _required("POWER_TEST_POSTGRES_DB")
_db_user = _required("POWER_TEST_POSTGRES_USER")
_db_password = _required("POWER_TEST_POSTGRES_PASSWORD")
_db_host = os.environ.get("POWER_TEST_POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1"
_db_port = os.environ.get("POWER_TEST_POSTGRES_PORT", "5432").strip() or "5432"

# Fail closed if someone accidentally supplies deployment-looking credentials.
if "test" not in _db_name.lower():
    raise ImproperlyConfigured("POWER_TEST_POSTGRES_DB must contain 'test'.")
if "test" not in _db_user.lower():
    raise ImproperlyConfigured("POWER_TEST_POSTGRES_USER must contain 'test'.")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": _db_host,
        "PORT": _db_port,
        "NAME": _db_name,
        "USER": _db_user,
        "PASSWORD": _db_password,
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 5},
        "TEST": {"NAME": f"test_{_db_name}"},
    }
}
