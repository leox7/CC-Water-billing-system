"""Routes for recording a payment on the bill page. The rules are in payments/service.py.

Owner and member do this work alike (PRD section 3), so every route needs only a signed-in
user (require_login). There is no route that edits or deletes a payment (PAY-9).

- POST /bills/{bill_id}/payments: record a payment. The form is sent by htmx and the answer
  replaces the Payments card, and the status badge at the top of the page (PAY-7). Without
  htmx it is a normal post that comes back to the bill page.
- GET /bills/{bill_id}/payments/preview: the live "Ksh 1,600 will still be owed." line under
  the amount.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_login
from app.core.errors import AppError, render_error, unexpected_error
from app.core.templating import is_htmx_request, templates
from app.features.payments import service as payments
from app.models import User

router = APIRouter()

CARD = "payments/views/_card.html"
OWED_LINE = "payments/views/_owed_line.html"
DETAIL_PAGE = "bills/views/detail.html"


def typed_payment_form(
    amount: str = Form(""),
    payment_date: str = Form(""),
    mpesa_reference: str = Form(""),
) -> dict[str, str]:
    """The boxes of the payment form, by name. Anything else sent with it is ignored (IN-7):
    the amount paid and the status are never taken from the form."""
    return {"amount": amount, "payment_date": payment_date, "mpesa_reference": mpesa_reference}


@router.get("/bills/{bill_id}/payments/preview")
def owed_preview(
    request: Request, bill_id: int, amount: str = "", user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    try:
        return templates.TemplateResponse(request, OWED_LINE, {"owed": payments.owed_after(db, bill_id, amount)})
    except AppError as exc:
        # the problem shows on the line under the amount as soon as it is typed
        return render_error(request, exc, OWED_LINE)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "work out what is still owed"))


@router.post("/bills/{bill_id}/payments")
def save_payment(
    request: Request,
    bill_id: int,
    typed: dict[str, str] = Depends(typed_payment_form),
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    is_htmx = is_htmx_request(request)
    template = CARD if is_htmx else DETAIL_PAGE
    context: dict[str, Any] = {"user": user, "form": typed, "today": payments.today()}
    try:
        context["details"] = payments.payment_details(db, bill_id)
        payments.record_payment(db, user, bill_id, typed)
        if not is_htmx:
            # a plain form post: back to the bill page, so a refresh does not record it again
            return RedirectResponse(f"/bills/{bill_id}?notice=payment_saved", status_code=303)
        # A fresh read, so the card shows the new totals and the payment in its list. The form
        # starts empty again. `oob` also sends the status badge at the top of the page.
        saved = {"details": payments.payment_details(db, bill_id), "today": context["today"],
                 "payment_saved": True, "oob": True}
        return templates.TemplateResponse(request, CARD, saved)
    except AppError as exc:
        if "details" not in context:
            # no such bill: there is no form to show again
            return render_error(request, exc)
        return render_error(request, exc, template, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "record the payment"))
