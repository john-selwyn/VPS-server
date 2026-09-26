"""Customer-safe, versioned power API serialization."""
from django.http import JsonResponse
from django.urls import reverse

from .models import PowerOperation


ERRORS = {
    "INVALID_REQUEST": "Invalid request.",
    "UNAUTHORIZED": "Unauthorized.",
    "NOT_FOUND": "Order or operation not found.",
    "METHOD_NOT_ALLOWED": "Method not allowed.",
    "VPS_NOT_READY": "VPS not ready.",
    "IDEMPOTENCY_CONFLICT": "Idempotency key is already bound to another request.",
    "OPERATION_IN_PROGRESS": "A power operation is unresolved for this VPS.",
    "INVALID_STATE": "Power action is not allowed in the observed state.",
    "RUNTIME_UNAVAILABLE": "Runtime status unavailable.",
    "SUBMISSION_UNKNOWN": "The command outcome is unknown. Do not submit another command.",
    "TASK_STATUS_UNAVAILABLE": "The operation outcome cannot currently be verified.",
    "TASK_FAILED": "The power task reported failure and requires reconciliation.",
    "STATE_UNCONFIRMED": "The task completed but the expected state could not be confirmed.",
    "INTERNAL_ERROR": "Internal service unavailable.",
}


def safe_error(code):
    # Unknown database/error values must never become response text.
    if code not in ERRORS:
        code = "INTERNAL_ERROR"
    return {"code": code, "message": ERRORS[code]}


def json_response(data, status=200):
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "no-store"
    return response


def error_response(code, status, *, billing_order_id=None, active_operation_id=None):
    return json_response({
        "version": 1,
        "billing_order_id": billing_order_id,
        "error": safe_error(code),
        "active_operation_id": str(active_operation_id) if active_operation_id else None,
    }, status)


def operation_response(operation):
    if (operation.action not in PowerOperation.ACTIONS
            or operation.status not in PowerOperation.STATUSES
            or operation.observed_state not in PowerOperation.STATES
            or (operation.status == "succeeded" and operation.result not in ("executed", "noop"))
            or (operation.status != "succeeded" and operation.result is not None)):
        return error_response("INTERNAL_ERROR", 500)
    unresolved = operation.status in PowerOperation.UNRESOLVED
    response = json_response({
        "version": 1,
        "billing_order_id": operation.billing_order_id,
        "operation_id": str(operation.operation_id),
        "action": operation.action,
        "status": operation.status,
        "result": operation.result,
        "observed_state": operation.observed_state,
        "observed_at": operation.observed_at.isoformat() if operation.observed_at else None,
        "error": safe_error(operation.error_code) if operation.error_code else None,
    }, 202 if unresolved else 200)
    response["Location"] = reverse("internal_power_operation", kwargs={
        "billing_order_id": operation.billing_order_id, "operation_id": operation.operation_id,
    })
    if unresolved:
        response["Retry-After"] = "2"
    return response
