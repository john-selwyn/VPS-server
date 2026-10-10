import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta
from vps.models import VPS
from vps.public_ssh import PublicSSHError, sync_access


class Command(BaseCommand):
    help = "Create/retry persistent public SSH relays for ready Linux VPS services."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--order", type=int)

    def handle(self, *args, **options):
        if not settings.PUBLIC_SSH_ENABLED:
            raise CommandError("PUBLIC_SSH_ENABLED must be enabled after completing relay setup.")
        while True:
            now = timezone.now()
            records = VPS.objects.filter(status="Running").filter(
                (Q(operating_system="Windows 11") & ~Q(windows_password_encrypted=""))
                | (~Q(operating_system="Windows 11") & ~Q(ssh_username="") & ~Q(ssh_public_key=""))
            ).exclude(public_ssh_access__status="ready")
            records = records.filter(
                Q(public_ssh_access__retry_at__isnull=True) | Q(public_ssh_access__retry_at__lte=now)
            ).filter(
                Q(public_ssh_access__lease__isnull=True)
                | Q(public_ssh_access__claimed_at__lte=now - timedelta(minutes=2))
            )
            if options["order"]:
                records = records.filter(billing_order_id=options["order"])
            for vps in records.order_by("pk")[:20]:
                try:
                    access = sync_access(vps.pk)
                    self.stdout.write(f"Order {vps.billing_order_id}: {access.status}")
                except PublicSSHError as exc:
                    self.stderr.write(f"Order {vps.billing_order_id}: {exc.code}")
            if options["once"]:
                return
            time.sleep(15)
