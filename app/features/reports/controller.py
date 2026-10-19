"""Routes for the Reports screen. The figures are worked out in reports/service.py.

Owner and member see the same reports (PRD section 3), so every route needs only a signed-in
user (require_login). Every report is a plain page: its filters are a GET form, so a filtered
report has its own address and can be refreshed or bookmarked.

- /reports: the menu of the four reports.
- /reports/arrears: who owes money (REP-1).
- /reports/payments: payment history, by customer and month (REP-2).
- /reports/customers: every customer with their latest bill's status (REP-3).
- /reports/summary: one month at a glance (REP-4). With no month chosen, the newest one.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_login
from app.core.errors import AppError, render_error, unexpected_error
from app.core.templating import templates
from app.features.reports import service as reports
from app.models import User

router = APIRouter()

MENU_PAGE = "reports/views/index.html"
ARREARS_PAGE = "reports/views/arrears.html"
PAYMENTS_PAGE = "reports/views/payment_history.html"
CUSTOMERS_PAGE = "reports/views/customer_list.html"
SUMMARY_PAGE = "reports/views/monthly_summary.html"


@router.get("/reports")
def report_menu(request: Request, user: User = Depends(require_login)) -> Response:
    try:
        return templates.TemplateResponse(request, MENU_PAGE, {"user": user, "period": reports.current_period()})
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the reports"))


@router.get("/reports/arrears")
def arrears_report(request: Request, user: User = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    try:
        context = {"user": user, "report": reports.arrears(db), "list_limit": reports.LIST_LIMIT}
        return templates.TemplateResponse(request, ARREARS_PAGE, context)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the arrears report"))


@router.get("/reports/payments")
def payment_history_report(
    request: Request, customer: str = "", month: str = "", user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    context: dict[str, Any] = {"user": user, "history": None, "customer": customer, "month": month,
                               "customers": [], "months": [], "list_limit": reports.LIST_LIMIT}
    try:
        context["customers"] = reports.customer_choices(db)
        context["months"] = reports.payment_months(db)
        context["history"] = reports.payment_history(db, customer, month)
        return templates.TemplateResponse(request, PAYMENTS_PAGE, context)
    except AppError as exc:
        # a refused filter comes back on the report, with the message beside it
        return render_error(request, exc, PAYMENTS_PAGE, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the payment history"))


@router.get("/reports/customers")
def customer_list_report(
    request: Request, status: str = "", user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    context: dict[str, Any] = {"user": user, "report": None, "status": status, "statuses": reports.BILL_STATUSES,
                               "list_limit": reports.LIST_LIMIT}
    try:
        context["report"] = reports.customer_report(db, status)
        return templates.TemplateResponse(request, CUSTOMERS_PAGE, context)
    except AppError as exc:
        return render_error(request, exc, CUSTOMERS_PAGE, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the customer list"))


@router.get("/reports/summary")
def monthly_summary_report(
    request: Request, month: str | None = None, user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    context: dict[str, Any] = {"user": user, "summary": None, "months": [], "month": month or ""}
    try:
        context["months"] = reports.summary_months(db)
        if not month:
            # nothing chosen yet: the newest month with bills or payments, or this month
            context["month"] = context["months"][0] if context["months"] else reports.current_period()
        context["summary"] = reports.monthly_summary(db, context["month"])
        return templates.TemplateResponse(request, SUMMARY_PAGE, context)
    except AppError as exc:
        return render_error(request, exc, SUMMARY_PAGE, context)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the monthly summary"))
