"""Keep Django's debug/error pages outside the internal API boundary."""
import logging

from django.utils.deprecation import MiddlewareMixin

from .power_contract import error_response

logger = logging.getLogger(__name__)


class InternalAPIErrorMiddleware(MiddlewareMixin):
    def process_exception(self, request, exception):
        if request.path.startswith("/api/internal/"):
            # Deliberately omit exception text, headers, bodies and traceback locals.
            logger.error("Internal API request failed (%s)", type(exception).__name__)
            return error_response("INTERNAL_ERROR", 500)

    def process_response(self, request, response):
        if request.path.startswith("/api/internal/"):
            if response.status_code >= 400 and not response.get("Content-Type", "").startswith("application/json"):
                code = {400: "INVALID_REQUEST", 401: "UNAUTHORIZED", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(response.status_code, "INTERNAL_ERROR")
                response = error_response(code, response.status_code)
            response["Cache-Control"] = "no-store"
        return response
