"""Routes for the Customers screen. The rules are in customers/service.py.

Owner and member do this work alike (PRD section 3), so every route here needs only a
signed-in user (require_login).

- The add and edit form is a normal page. After a save the user goes back to the list,
  which says what happened (/customers?notice=added&customer=12).
- Deactivate, reactivate and remove are sent by htmx. The answer is the changed row and a
  message. If htmx is not running, the form is a normal post and the answer is the list.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_login
from app.core.errors import AppError, NotFoundError, render_error, unexpected_error
from app.core.templating import is_htmx_request, templates
from app.features.customers import service as customers
from app.models import Customer, User

router = APIRouter()

LIST_PAGE = "customers/views/list.html"
FORM_PAGE = "customers/views/form.html"
ROW_UPDATE = "customers/views/_row_update.html"
# What the list says when the form sends the user back to it
NOTICES = {"added": "{name} was added.", "saved": "{name}'s details were saved."}


def typed_customer_form(
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(""),
    location: str = Form(""),
    meter_number: str = Form(""),
    rate_per_unit: str = Form(""),
    service_charge: str = Form(""),
) -> dict[str, str]:
    """The boxes of the customer form, by name. Anything else sent with the form is ignored (IN-7)."""
    return {"first_name": first_name, "last_name": last_name, "phone": phone, "location": location,
            "meter_number": meter_number, "rate_per_unit": rate_per_unit, "service_charge": service_charge}


def _form_values(customer: Customer) -> dict[str, str]:
    # the phone is shown the way people write it: 254722123456 becomes 0722123456
    return {"first_name": customer.first_name, "last_name": customer.last_name, "phone": "0" + customer.phone[3:],
            "location": customer.location, "meter_number": customer.meter_number,
            "rate_per_unit": str(customer.rate_per_unit), "service_charge": str(customer.service_charge)}


def _notice(rows: list[customers.CustomerRow], notice: str, customer_id: int | None) -> str | None:
    """The message after a save, using the customer's real name. Nothing for an unknown notice."""
    if notice not in NOTICES:
        return None
    for row in rows:
        if row.customer.customer_id == customer_id:
            return NOTICES[notice].format(name=f"{row.customer.first_name} {row.customer.last_name}")
    return None


def _answer(request: Request, context: dict[str, Any]) -> Response:
    if is_htmx_request(request):
        return templates.TemplateResponse(request, ROW_UPDATE, context)
    # a plain form post: go back to the list, so a refresh does not send the form again
    return RedirectResponse("/customers", status_code=303)


@router.get("/customers")
def customer_list(
    request: Request,
    search: str = "",
    notice: str = "",
    customer: int | None = None,
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    context: dict[str, Any] = {"user": user, "search": search, "rows": [], "active_count": 0,
                               "list_limit": customers.LIST_LIMIT}
    try:
        context["active_count"] = customers.count_active_customers(db)
        context["rows"] = customers.list_customers(db, search)
        context["notice"] = _notice(context["rows"], notice, customer)
        return templates.TemplateResponse(request, LIST_PAGE, context)
    except AppError as exc:
        # a search that is refused comes back on the list, with the message
        return render_error(request, exc, LIST_PAGE, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the customers"))


@router.get("/customers/new")
def new_customer_page(request: Request, user: User = Depends(require_login)) -> Response:
    try:
        form = {name: str(amount) for name, amount in customers.default_charges().items()}
        return templates.TemplateResponse(request, FORM_PAGE, {"user": user, "customer_id": None, "form": form})
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the customer form"))


@router.post("/customers/new")
def add_customer(
    request: Request,
    typed: dict[str, str] = Depends(typed_customer_form),
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    form_context = {"customer_id": None, "form": typed}
    try:
        customer = customers.create_customer(db, user, typed)
        return RedirectResponse(f"/customers?notice=added&customer={customer.customer_id}", status_code=303)
    except AppError as exc:
        return render_error(request, exc, FORM_PAGE, form_context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "add the customer"), FORM_PAGE, form_context)


@router.get("/customers/{customer_id}/edit")
def edit_customer_page(
    request: Request, customer_id: int, user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    try:
        customer = customers.get_customer(db, customer_id)
        context = {"user": user, "customer_id": customer_id, "form": _form_values(customer)}
        return templates.TemplateResponse(request, FORM_PAGE, context)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the customer form"))


@router.post("/customers/{customer_id}/edit")
def save_customer(
    request: Request,
    customer_id: int,
    typed: dict[str, str] = Depends(typed_customer_form),
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    form_context = {"customer_id": customer_id, "form": typed}
    try:
        customers.update_customer(db, user, customer_id, typed)
        return RedirectResponse(f"/customers?notice=saved&customer={customer_id}", status_code=303)
    except NotFoundError as exc:
        # there is no customer to show the form for
        return render_error(request, exc)
    except AppError as exc:
        return render_error(request, exc, FORM_PAGE, form_context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "save the customer"), FORM_PAGE, form_context)


@router.post("/customers/{customer_id}/deactivate")
def deactivate_customer(
    request: Request, customer_id: int, user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    try:
        customer = customers.deactivate_customer(db, user, customer_id)
        message = f"{customer.first_name} {customer.last_name} was deactivated and will get no new bills."
        return _answer(request, {"row": customers.customer_row(db, customer), "message": message,
                                 "active_count": customers.count_active_customers(db)})
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "deactivate the customer"))


@router.post("/customers/{customer_id}/reactivate")
def reactivate_customer(
    request: Request, customer_id: int, user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    try:
        customer = customers.reactivate_customer(db, user, customer_id)
        message = f"{customer.first_name} {customer.last_name} was reactivated and will be billed again."
        return _answer(request, {"row": customers.customer_row(db, customer), "message": message,
                                 "active_count": customers.count_active_customers(db)})
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "reactivate the customer"))


@router.post("/customers/{customer_id}/remove")
def remove_customer(
    request: Request, customer_id: int, user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    try:
        name = customers.remove_customer(db, user, customer_id)
        return _answer(request, {"message": f"{name} was removed.",
                                 "active_count": customers.count_active_customers(db)})
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "remove the customer"))
