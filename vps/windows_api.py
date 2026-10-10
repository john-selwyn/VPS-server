"""Windows credentials for the authenticated billing backend."""
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables

from .internal_api import authenticated
from .models import VPS
from .public_ssh import endpoint_fields
from .windows_credentials import decrypt_windows_password


@never_cache
@authenticated("GET")
@sensitive_variables()
def windows_credentials(request, billing_order_id):
    if request.GET or not 0 < billing_order_id <= 9223372036854775807:
        return JsonResponse({"error": "Invalid request."}, status=400)
    vps = VPS.objects.filter(billing_order_id=billing_order_id).first()
    if vps is None:
        return JsonResponse({"error": "Order not found."}, status=404)
    if vps.operating_system != "Windows 11":
        return JsonResponse({"error": "Windows credentials unavailable."}, status=409)
    if vps.status != "Running":
        return JsonResponse({"error": "Windows VPS is not ready."}, status=409)
    access = endpoint_fields(vps).get("rdp_access")
    if not access:
        return JsonResponse({"error": "RDP access is not ready."}, status=409)
    try:
        password = decrypt_windows_password(vps.windows_password_encrypted)
    except RuntimeError:
        return JsonResponse({"error": "Windows credentials unavailable."}, status=503)
    return JsonResponse({
        "billing_order_id": vps.billing_order_id,
        "rdp_access": {**access, "password": password},
    })
