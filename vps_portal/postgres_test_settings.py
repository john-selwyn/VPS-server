"""Opt-in local PostgreSQL tests; never inherit the deployment database."""
import os

from .test_settings import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "127.0.0.1",
        "PORT": os.environ.get("POWER_TEST_POSTGRES_PORT", "5432"),
        "NAME": os.environ["POWER_TEST_POSTGRES_DB"],
        "USER": os.environ["POWER_TEST_POSTGRES_USER"],
        "PASSWORD": os.environ.get("POWER_TEST_POSTGRES_PASSWORD", ""),
    }
}
