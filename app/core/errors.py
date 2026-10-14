"""The exception family for expected failures, unexpected_error(), and the code that
turns a failure into a response (architecture.md section 5).

A service raises one of these with a plain message. The controller calls
render_error() to turn it into a response with the same status code, and the handlers
registered by register_error_handlers() do the same for anything raised outside a
controller's own try. Messages follow the microcopy rules in DESIGN_SYSTEM.md: they say
what is wrong and how to fix it, and never contain anything technical.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var
from app.core.templating import is_htmx_request, templates
from app.features.audit import service as audit

logger = logging.getLogger("app.errors")


class AppError(Exception):
    """Base of every expected failure. Carries a status code and a user message."""

    status_code = 500
    default_message = "Something went wrong. Please try again."

    def __init__(self, message: str | None = None, field: str | None = None) -> None:
        self.message = message or self.default_message
        # the form field the message belongs to, so it can show beside that field
        self.field = field
        super().__init__(self.message)


class BusinessRuleError(AppError):
    """Input or a business rule fails, such as a reading that is too low."""

    status_code = 422
    default_message = "That could not be saved. Please check the details and try again."


class ConflictError(AppError):
    """A duplicate, such as a reading for the period or an M-Pesa code."""

    status_code = 409
    default_message = "That already exists."


class NotFoundError(AppError):
    status_code = 404
    default_message = "We could not find that."


class PermissionDeniedError(AppError):
    """The role is not allowed, or a CSRF check failed."""

    status_code = 403
    default_message = "You do not have access to this page."


class AuthenticationRequiredError(AppError):
    """No valid session. The user is sent to the login page."""

    status_code = 401
    default_message = "Please sign in to continue."


class TooManyAttemptsError(AppError):
    """The login lock or the SMS cap. The message says how long to wait."""

    status_code = 429
    default_message = "Too many attempts. Please wait a little and try again."


class InternalError(AppError):
    """Anything unexpected, including a database failure."""

    status_code = 500
    default_message = "Something went wrong on our side. Please try again."

    def __init__(
        self,
        message: str | None = None,
        field: str | None = None,
        action: str | None = None,
        cause_type: str | None = None,
    ) -> None:
        super().__init__(message, field)
        # What was being attempted and what kind of exception stopped it. These go to the
        # audit log. The exception's own text does not: it can hold SQL or secrets.
        self.action = action
        self.cause_type = cause_type


def unexpected_error(exc: Exception, action: str) -> InternalError:
    """Log an unexpected exception with its stack trace and return a safe 500.

    The caller raises the result with `raise ... from exc`, so the cause is kept.
    The message names the action, never the exception, so nothing technical
    (SQL, file paths, secrets) can reach the user.
    """
    logger.error("Unexpected error while trying to %s", action, exc_info=exc)
    return InternalError(action=action, cause_type=type(exc).__name__)


# ---- turning a failure into a response ----

# The big heading on the error page. Anything not listed gets the default.
ERROR_HEADINGS = {
    403: "You do not have access",
    404: "We could not find that",
    405: "That is not allowed here",
    422: "That could not be saved",
    429: "Too many attempts",
    500: "Something went wrong on our side",
}
DEFAULT_ERROR_HEADING = "Something needs your attention"

# Messages for errors that were not raised by our own code (a bad URL, a wrong method)
HTTP_ERROR_MESSAGES = {
    400: "We could not understand that request. Please go back and try again.",
    404: NotFoundError.default_message,
    405: "That is not allowed here. Please go back and try again.",
}
VALIDATION_ERROR_MESSAGE = "Some details are missing or not valid. Please check the form and try again."


def render_error(
    request: Request,
    exc: AppError,
    form_template: str | None = None,
    form_context: dict[str, Any] | None = None,
) -> Response:
    """The one place that decides what an error looks like.

    - 401 sends the person to the login page.
    - When the controller gives form_template, the form is shown again with the message
      beside the field and the typed values kept (form_context).
    - Otherwise an HTMX request gets a small message fragment, and a normal request gets
      a friendly error page, both with the same status code.
    - For a status of 500 or more it also records system.error in the audit log. Refusals
      (4xx) were already recorded by the service that refused them.
    """
    try:
        return _build_error_response(request, exc, form_template, form_context or {})
    except Exception:
        # The error page itself failed. Say so in the log, and still answer with a plain
        # message and the right status code, never a blank page or a traceback.
        logger.error("The error page could not be rendered", exc_info=True)
        return PlainTextResponse(InternalError.default_message, status_code=500)


def _build_error_response(
    request: Request, exc: AppError, form_template: str | None, form_context: dict[str, Any]
) -> Response:
    status_code = exc.status_code
    is_htmx = is_htmx_request(request)

    if status_code == 401:
        return _redirect_to_login(is_htmx)
    if status_code >= 500:
        _record_system_error(request, exc)

    context = {
        "request": request,
        # set by the login dependency, so the page can show the signed-in layout
        "user": getattr(request.state, "user", None),
        "message": exc.message,
        "heading": ERROR_HEADINGS.get(status_code, DEFAULT_ERROR_HEADING),
        # the request id, so the developer can find this exact request (AUD-4)
        "reference": request_id_var.get() if status_code >= 500 else None,
        "errors": [exc],
        "error": exc,
    }
    if form_template:
        # The controller chose the template: the whole page for a normal request, or just
        # the form for an HTMX request. Either way the message sits beside its field.
        return templates.TemplateResponse(
            request, form_template, {**form_context, **context}, status_code=status_code
        )
    if is_htmx:
        return templates.TemplateResponse(
            request, "partials/error_message.html", context, status_code=status_code
        )
    return templates.TemplateResponse(request, "error.html", context, status_code=status_code)


def _redirect_to_login(is_htmx: bool) -> Response:
    if is_htmx:
        # htmx would swap a redirect into the page. This header makes it load the page.
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def _record_system_error(request: Request, exc: AppError) -> None:
    action = getattr(exc, "action", None) or "handle the request"
    audit.record_problem(
        "system.error",
        f"Unhandled error while trying to {action}",
        # the stack trace stays in the application log, not here
        details={
            "error_type": getattr(exc, "cause_type", None) or type(exc).__name__,
            "action": action,
            "route": request.url.path,
            "method": request.method,
        },
    )


# ---- the safety net: handlers for anything raised outside a controller's own try ----


def handle_app_error(request: Request, exc: AppError) -> Response:
    return render_error(request, exc)


def handle_validation_error(request: Request, exc: RequestValidationError) -> Response:
    # REQ-1: an id in the URL that is not a number is a record that does not exist
    first_error = exc.errors()[0]
    where, *rest = first_error["loc"]
    if where == "path":
        return render_error(request, NotFoundError())
    # the field name is shown beside the field, the technical wording never is
    field = str(rest[0]) if where in ("body", "form") and rest else None
    return render_error(request, BusinessRuleError(VALIDATION_ERROR_MESSAGE, field=field))


def handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
    # a bad URL (404), a wrong method (405) and the like
    message = HTTP_ERROR_MESSAGES.get(exc.status_code, AppError.default_message)
    error = AppError(message)
    error.status_code = exc.status_code
    response = render_error(request, error)
    for name, value in (exc.headers or {}).items():
        response.headers[name] = value
    return response


def handle_database_error(request: Request, exc: SQLAlchemyError) -> Response:
    return render_error(request, unexpected_error(exc, "use the database"))


def handle_unexpected_error(request: Request, exc: Exception) -> Response:
    return render_error(request, unexpected_error(exc, "handle the request"))


def register_error_handlers(app: FastAPI) -> None:
    """Called once from main.py. Every handler ends in render_error()."""
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(SQLAlchemyError, handle_database_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
