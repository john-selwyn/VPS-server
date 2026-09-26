import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from vps.power import work_once

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process durable power operations. Run continuously under a service supervisor."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one recovery/submission/poll pass.")

    def handle(self, *args, **options):
        while True:
            close_old_connections()
            try:
                work_once()
            except Exception as exc:
                # No raw upstream text or credentials in routine service logs.
                logger.error("Power worker pass failed (%s)", type(exc).__name__)
                if options["once"]:
                    from django.core.management.base import CommandError
                    raise CommandError("Power worker pass failed.") from None
            if options["once"]:
                return
            time.sleep(2)
