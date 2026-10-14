"""Routes for the Account screen. The rules are in account/service.py.

The changes are sent by htmx, and each answer is a small piece of HTML: the changed row,
a fresh form, and a message. If htmx is not running, the form is a normal post, and
the answer is the Account page again (or the error page).
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_login, require_owner
from app.core.errors import AppError, render_error, unexpected_error
from app.core.templating import is_htmx_request, templates
from app.features.account import service as account
from app.models import User

router = APIRouter()

TEAM_UPDATE = "account/views/_team_update.html"
ADD_MEMBER_FORM = "account/views/_add_member_form.html"
CHANGE_PASSWORD_FORM = "account/views/_change_password_form.html"


def _answer(request: Request, template: str, context: dict[str, Any]) -> Response:
    if is_htmx_request(request):
        return templates.TemplateResponse(request, template, context)
    # a plain form post: go back to the page, so a refresh does not send the form again
    return RedirectResponse("/account", status_code=303)


def _failed(request: Request, exc: AppError, form_template: str | None = None, form_context: dict | None = None) -> Response:
    # for htmx, a form comes back with its message beside the field; otherwise the error page
    if form_template and is_htmx_request(request):
        return render_error(request, exc, form_template, form_context or {})
    return render_error(request, exc)


@router.get("/account")
def account_page(request: Request, user: User = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    try:
        # ACC-2: a member sees only change password and sign out
        team = account.list_team(db) if user.role == "owner" else []
        return templates.TemplateResponse(request, "account/views/index.html", {"user": user, "team": team, "form": {}})
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the account page"))


@router.post("/account/members")
def add_member(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    identifier: str = Form(""),
    password: str = Form(""),
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    typed = {"form": {"first_name": first_name, "last_name": last_name, "identifier": identifier}}
    try:
        member = account.create_member(db, owner, first_name, last_name, identifier, password)
        message = f"{member.first_name} {member.last_name} was added. Give them their password in person."
        context = {"added_member": account.TeamMember(member, 0), "show_add_form": True, "message": message}
        return _answer(request, TEAM_UPDATE, context)
    except AppError as exc:
        return _failed(request, exc, ADD_MEMBER_FORM, typed)
    except Exception as exc:
        return _failed(request, unexpected_error(exc, "add the member"), ADD_MEMBER_FORM, typed)


@router.post("/account/members/{member_id}/deactivate")
def deactivate_member(
    request: Request, member_id: int, owner: User = Depends(require_owner), db: Session = Depends(get_db)
) -> Response:
    try:
        member = account.deactivate_member(db, owner, member_id)
        message = f"{member.first_name} {member.last_name} was deactivated and can no longer sign in."
        return _answer(request, TEAM_UPDATE, {"member": account.team_member(db, member), "message": message})
    except AppError as exc:
        return _failed(request, exc)
    except Exception as exc:
        return _failed(request, unexpected_error(exc, "deactivate the member"))


@router.post("/account/members/{member_id}/reactivate")
def reactivate_member(
    request: Request, member_id: int, owner: User = Depends(require_owner), db: Session = Depends(get_db)
) -> Response:
    try:
        member = account.reactivate_member(db, owner, member_id)
        message = f"{member.first_name} {member.last_name} was reactivated and can sign in again."
        return _answer(request, TEAM_UPDATE, {"member": account.team_member(db, member), "message": message})
    except AppError as exc:
        return _failed(request, exc)
    except Exception as exc:
        return _failed(request, unexpected_error(exc, "reactivate the member"))


@router.post("/account/members/{member_id}/delete")
def delete_member(
    request: Request, member_id: int, owner: User = Depends(require_owner), db: Session = Depends(get_db)
) -> Response:
    try:
        name = account.delete_member(db, owner, member_id)
        return _answer(request, TEAM_UPDATE, {"message": f"{name} was deleted."})
    except AppError as exc:
        return _failed(request, exc)
    except Exception as exc:
        return _failed(request, unexpected_error(exc, "delete the member"))


@router.post("/account/members/{member_id}/password")
def set_member_password(
    request: Request,
    member_id: int,
    new_password: str = Form(""),
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    try:
        member = account.set_member_password(db, owner, member_id, new_password)
        message = (
            f"New password set for {member.first_name} {member.last_name}. "
            "Give it to them in person. They can sign in with it now."
        )
        return _answer(request, TEAM_UPDATE, {"member": account.team_member(db, member), "message": message})
    except AppError as exc:
        return _failed(request, exc)
    except Exception as exc:
        return _failed(request, unexpected_error(exc, "set the new password"))


@router.post("/account/password")
def change_own_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    try:
        # AUTHZ-4: whose password changes comes from the session, never from the form
        account.change_own_password(db, user, current_password, new_password)
        return _answer(request, CHANGE_PASSWORD_FORM, {"saved": True})
    except AppError as exc:
        return _failed(request, exc, CHANGE_PASSWORD_FORM)
    except Exception as exc:
        return _failed(request, unexpected_error(exc, "change the password"), CHANGE_PASSWORD_FORM)
