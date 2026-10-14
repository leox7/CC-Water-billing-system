"""Routes for the Readings screen. The rules are in readings/service.py.

Owner and member do this work alike (PRD section 3), so every route needs only a signed-in
user (require_login).

- /readings: pick a customer (not read this month first), and the recent readings.
- /readings/new/{customer_id}: enter the reading. The form is sent by htmx and the answer
  replaces it with what happened and a "Next customer" button. Without htmx the same answer
  comes back as a whole page.
- /readings/preview: the live "That is 24 units used." line under the field.
- /readings/{reading_id}/edit and /delete: fix a reading (PRD 4.4). The service decides which
  of the three cases runs.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_login
from app.core.errors import AppError, render_error, unexpected_error
from app.core.templating import is_htmx_request, plain_number, templates
from app.features.readings import service as readings
from app.models import User

router = APIRouter()

INDEX_PAGE = "readings/views/index.html"
ENTER_PAGE = "readings/views/enter.html"
ENTER_PANEL = "readings/views/_enter_panel.html"
UNITS_LINE = "readings/views/_units_line.html"
PERIOD_LINE = "readings/views/_period_line.html"
EDIT_PAGE = "readings/views/edit.html"
FIX_PANEL = "readings/views/_fix_panel.html"


def typed_reading_form(
    current_reading: str = Form(""),
    reading_date: str = Form(""),
    previous_reading: str = Form(""),
    opening_balance: str = Form(""),
) -> dict[str, str]:
    """The boxes of the reading form, by name. Anything else sent with it is ignored (IN-7).
    The previous reading and opening balance count only on a first reading (CUS-5)."""
    return {"current_reading": current_reading, "reading_date": reading_date,
            "previous_reading": previous_reading, "opening_balance": opening_balance}


@router.get("/readings")
def readings_page(
    request: Request, search: str = "", user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    context: dict[str, Any] = {"user": user, "search": search, "rows": [], "recent": [], "progress": (0, 0),
                               "period": "", "list_limit": readings.LIST_LIMIT}
    try:
        context["period"] = readings.current_period()
        context["progress"] = readings.reading_progress(db)
        context["recent"] = readings.recent_readings(db)
        context["rows"] = readings.customers_to_read(db, search)
        return templates.TemplateResponse(request, INDEX_PAGE, context)
    except AppError as exc:
        # a search that is refused comes back on the page, with the message
        return render_error(request, exc, INDEX_PAGE, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the readings"))


@router.get("/readings/new/{customer_id}")
def enter_page(
    request: Request, customer_id: int, notice: str = "", user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    try:
        context = {"user": user, "entry": readings.entry_details(db, customer_id), "today": readings.today(),
                   "form": {"current_reading": "", "reading_date": readings.today().isoformat(),
                            "previous_reading": "0", "opening_balance": "0"},
                   "period": readings.period_preview(""), "deleted": notice == "deleted"}
        return templates.TemplateResponse(request, ENTER_PAGE, context)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the reading form"))


@router.get("/readings/preview")
def units_preview(
    request: Request, customer_id: int, current_reading: str = "", previous_reading: str = "",
    user: User = Depends(require_login), db: Session = Depends(get_db),
) -> Response:
    try:
        units = readings.units_preview(db, customer_id, current_reading, previous_reading)
        return templates.TemplateResponse(request, UNITS_LINE, {"units": units})
    except AppError as exc:
        # the problem shows on the line under the field as soon as it is typed
        return render_error(request, exc, UNITS_LINE)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "work out the units"))


@router.get("/readings/period")
def period_line(
    request: Request, reading_date: str = "", user: User = Depends(require_login)
) -> Response:
    try:
        return templates.TemplateResponse(request, PERIOD_LINE, {"period": readings.period_preview(reading_date)})
    except AppError as exc:
        # a date that cannot be used shows on the line under the date as soon as it is picked
        return render_error(request, exc, PERIOD_LINE)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "work out the billing month"))


@router.post("/readings")
def save_reading(
    request: Request,
    customer_id: int = Form(...),
    typed: dict[str, str] = Depends(typed_reading_form),
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    template = ENTER_PANEL if is_htmx_request(request) else ENTER_PAGE
    context: dict[str, Any] = {"user": user, "form": typed}
    try:
        context["today"] = readings.today()
        context["period"] = readings.period_if_valid(typed["reading_date"])
        context["entry"] = readings.entry_details(db, customer_id)
        context["result"] = readings.create_reading(db, user, customer_id, typed)
        context["next_customer"] = readings.next_customer_to_read(db)
        return templates.TemplateResponse(request, template, context)
    except AppError as exc:
        if "entry" not in context:
            # no such customer: there is no form to show again
            return render_error(request, exc)
        return render_error(request, exc, template, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "save the reading"))


@router.get("/readings/{reading_id}/edit")
def fix_page(
    request: Request, reading_id: int, user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    try:
        fix = readings.fix_details(db, reading_id)
        form = {"current_reading": plain_number(fix.reading.current_reading),
                "previous_reading": plain_number(fix.reading.previous_reading),
                "opening_balance": plain_number(fix.bill.previous_balance) if fix.bill else "0"}
        context = {"user": user, "fix": fix, "customer": fix.customer, "period": fix.reading.billing_period,
                   "form": form}
        return templates.TemplateResponse(request, EDIT_PAGE, context)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the reading"))


@router.post("/readings/{reading_id}/edit")
def save_fix(
    request: Request,
    reading_id: int,
    current_reading: str = Form(""),
    previous_reading: str = Form(""),
    opening_balance: str = Form(""),
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    template = FIX_PANEL if is_htmx_request(request) else EDIT_PAGE
    # the previous reading and opening balance count only on a first reading (CUS-5)
    context: dict[str, Any] = {"user": user, "form": {"current_reading": current_reading,
                                                      "previous_reading": previous_reading,
                                                      "opening_balance": opening_balance}}
    try:
        fix = readings.fix_details(db, reading_id)
        # kept as plain values: in the first two cases the old reading is gone after the save
        context.update({"fix": fix, "customer": fix.customer, "period": fix.reading.billing_period})
        context["result"] = readings.correct_reading(db, user, reading_id, context["form"])
        return templates.TemplateResponse(request, template, context)
    except AppError as exc:
        if "fix" not in context:
            return render_error(request, exc)
        return render_error(request, exc, template, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "correct the reading"))


@router.post("/readings/{reading_id}/delete")
def delete_reading(
    request: Request, reading_id: int, user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    try:
        deleted = readings.delete_reading(db, user, reading_id)
        # straight back to the form, so the reading can be entered again
        url = f"/readings/new/{deleted.customer.customer_id}?notice=deleted"
        if is_htmx_request(request):
            # htmx loads this page as a whole, instead of swapping it into the dialog
            return Response(headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "delete the reading"))
