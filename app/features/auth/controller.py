"""Routes for /login, /register and /logout. The rules are in auth/service.py."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import current_user, require_login
from app.core.errors import AppError, PermissionDeniedError, render_error, unexpected_error
from app.core.templating import templates
from app.features.auth import service as auth
from app.models import User

router = APIRouter()

# Where a person lands after signing in: the dashboard
HOME_PATH = "/"

# Messages the login page can show, chosen by ?notice= in the address. Only these fixed
# words can appear, never anything typed into the address.
NOTICES = {
    "registered": "Your account is ready. Sign in to continue.",
    "signed-out": "You have signed out.",
}


@router.get("/login")
def login_page(
    request: Request, notice: str = "", user: User | None = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    try:
        if user is not None:
            return RedirectResponse(HOME_PATH, status_code=303)
        context = {"form": {}, "notice": NOTICES.get(notice), "registration_open": auth.registration_is_open(db)}
        return templates.TemplateResponse(request, "auth/views/login.html", context)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the sign in page"))


@router.post("/login")
def login(
    request: Request, identifier: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)
) -> Response:
    try:
        user = auth.sign_in(db, identifier, password)
        # AUTH-S6: signing in starts a new session. SES-1: it holds the user id.
        request.session.clear()
        request.session["user_id"] = user.user_id
        return RedirectResponse(HOME_PATH, status_code=303)
    except AppError as exc:
        # the typed phone or email is kept; the password never is
        return render_error(request, exc, "auth/views/login.html", {"form": {"identifier": identifier}})
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "sign in"))


@router.post("/logout")
def logout(request: Request, user: User = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    try:
        auth.record_sign_out(db, user)
        # AUTH-S6: signing out clears the session; the browser is told to drop the cookie
        request.session.clear()
        return RedirectResponse("/login?notice=signed-out", status_code=303)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "sign out"))


@router.get("/register")
def register_page(request: Request, db: Session = Depends(get_db)) -> Response:
    try:
        # AUTH-2: once any account exists, registration is closed
        if not auth.registration_is_open(db):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "auth/views/register.html", {"form": {}})
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the register page"))


@router.post("/register")
def register(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    identifier: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    typed = {"first_name": first_name, "last_name": last_name, "identifier": identifier}
    try:
        auth.register_owner(db, first_name, last_name, identifier, password)
        return RedirectResponse("/login?notice=registered", status_code=303)
    except PermissionDeniedError:
        # someone already registered: registration is closed, so go to sign in
        return RedirectResponse("/login", status_code=303)
    except AppError as exc:
        return render_error(request, exc, "auth/views/register.html", {"form": typed})
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "create the owner account"))
