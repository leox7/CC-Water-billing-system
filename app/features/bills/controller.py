"""Routes for the Bills screen. The rules are in bills/service.py.

Owner and member do this work alike (PRD section 3), so every route needs only a signed-in
user (require_login).

- /bills: the list, filtered by month and status. With no month chosen it shows the newest
  month that has bills.
- /bills/{bill_id}: the breakdown in the order the customer sees it, and the SMS attempts.
- /bills/{bill_id}/resend: Send again, sent by htmx. The answer is the Messages card again.
  Without htmx it is a normal post that comes back to the bill page.
The Payments card on the bill page, and its routes, belong to payments/controller.py.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_login
from app.core.errors import AppError, render_error, unexpected_error
from app.core.templating import is_htmx_request, templates
from app.features.bills import service as bills
from app.features.payments import service as payments
from app.models import User

router = APIRouter()

LIST_PAGE = "bills/views/list.html"
DETAIL_PAGE = "bills/views/detail.html"
MESSAGE_LOG = "bills/views/_message_log.html"
# How a Send again that was a normal form post ended, from the address of the bill page
RESEND_NOTICES = ("sent", "failed")
# ...and how a payment form that was a normal post ended
PAYMENT_SAVED_NOTICE = "payment_saved"


@router.get("/bills")
def bill_list(
    request: Request,
    month: str | None = None,
    status: str = "",
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    context: dict[str, Any] = {"user": user, "rows": [], "periods": [], "month": month or "", "status": status,
                               "statuses": bills.BILL_STATUSES, "list_limit": bills.LIST_LIMIT}
    try:
        context["periods"] = bills.bill_periods(db)
        if month is None and context["periods"]:
            # nothing chosen yet: the newest month with bills
            context["month"] = context["periods"][0]
        context["rows"] = bills.list_bills(db, context["month"], status)
        return templates.TemplateResponse(request, LIST_PAGE, context)
    except AppError as exc:
        # a filter that is refused comes back on the list, with the message
        return render_error(request, exc, LIST_PAGE, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the bills"))


@router.get("/bills/{bill_id}")
def bill_page(
    request: Request, bill_id: int, notice: str = "", user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    try:
        context = {"user": user, "details": bills.bill_details(db, bill_id),
                   # only a known word is used, never what was typed in the address
                   "resend_status": notice if notice in RESEND_NOTICES else None,
                   # after a payment saved without htmx (see payments/controller.py)
                   "payment_saved": notice == PAYMENT_SAVED_NOTICE,
                   # the payment form's date starts at today
                   "today": payments.today()}
        return templates.TemplateResponse(request, DETAIL_PAGE, context)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the bill"))


@router.post("/bills/{bill_id}/resend")
def resend_sms(
    request: Request, bill_id: int, user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    try:
        message = bills.resend_sms(db, user, bill_id)
        if not is_htmx_request(request):
            # a plain form post: back to the bill page, so a refresh does not send it again
            return RedirectResponse(f"/bills/{bill_id}?notice={message.status}", status_code=303)
        context = {"details": bills.bill_details(db, bill_id), "resend_status": message.status}
        return templates.TemplateResponse(request, MESSAGE_LOG, context)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "send the SMS again"))
