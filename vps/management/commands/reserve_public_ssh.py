from django.core.management.base import BaseCommand, CommandError
from vps.models import VPS
from vps.public_ssh import PublicSSHError, reserve_access


class Command(BaseCommand):
    help = "Reserve an existing VPS's public port before enabling automatic SSH access."

    def add_arguments(self, parser):
        parser.add_argument("billing_order_id", type=int)
        parser.add_argument("--port", type=int, required=True)

    def handle(self, *args, **options):
        try:
            vps = VPS.objects.get(billing_order_id=options["billing_order_id"])
            access = reserve_access(vps.pk, port=options["port"])
        except (VPS.DoesNotExist, PublicSSHError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"Order {vps.billing_order_id}, VM {vps.vmid}: {access.host}:{access.port} reserved.")
