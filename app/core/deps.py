"""Who is signed in, and what they may do (SECURITY.md sections 1 to 3).

- current_user reloads the person from the database on every request, so role and
  status always come from the database, never from the cookie (SES-2). A missing or
  inactive person has the session cleared, which locks a deactivated member out on
  their very next click (AUTH-7).
- protect_by_default runs on every route of the app, so a route nobody remembered to
  protect still needs a signed-in user (AUTH-S1). Only PUBLIC_PATHS are open.
- require_login and require_owner are what a controller asks for. require_owner is for
  account management only; everything else needs require_login (AUTHZ-1).
"""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import AppError, AuthenticationRequiredError, PermissionDeniedError, unexpected_error
from app.features.audit import service as audit
from app.models import User

# AUTH-S1: the only pages that need nobody signed in. Static files are served before
# any route runs, so they are not listed here.
PUBLIC_PATHS = frozenset({"/login", "/register", "/health"})


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The signed-in person, or None. Loaded once per request and kept on request.state."""
    try:
        if hasattr(request.state, "user"):
            return request.state.user
        request.state.user = None
        user_id = request.session.get("user_id")
        if user_id is None:
            return None
        user = db.get(User, user_id)
        if user is None or user.status != "active":
            _reject_session(request, user_id, "missing" if user is None else "inactive")
            return None
        request.state.user = user
        return user
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the signed-in user") from exc


def _reject_session(request: Request, user_id: int, reason: str) -> None:
    # SES-2: a valid cookie for someone who is gone or deactivated is not enough
    request.session.clear()
    audit.record_problem(
        "auth.session_rejected",
        f"Session refused for user {user_id}: account is {reason}",
        details={"user_id": user_id, "reason": reason},
    )


def protect_by_default(request: Request, user: User | None = Depends(current_user)) -> None:
    """App-wide: every route needs a signed-in user unless it is in PUBLIC_PATHS."""
    try:
        if request.url.path in PUBLIC_PATHS:
            return
        if user is None:
            raise AuthenticationRequiredError()
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "check the sign in") from exc


def require_login(user: User | None = Depends(current_user)) -> User:
    """For a controller: the signed-in person. Anyone else is sent to the login page."""
    try:
        if user is None:
            raise AuthenticationRequiredError()
        return user
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "check the sign in") from exc


def require_owner(request: Request, user: User = Depends(require_login)) -> User:
    """For a controller: the owner. A member gets 403 (AUTHZ-2), and it is recorded."""
    try:
        if user.role == "owner":
            return user
        audit.record_problem(
            "auth.access_denied",
            f"{user.first_name} ({user.role}) was refused {request.method} {request.url.path}",
            actor=user,
            details={"route": request.url.path, "method": request.method, "role": user.role},
        )
        raise PermissionDeniedError()
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "check the role") from exc
