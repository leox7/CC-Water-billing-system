"""The home screen at / (DASH-1). The figures come from reports/service.py, the same
functions as the reports, so the two always agree. Owner and member see the same dashboard
(PRD section 3), so it needs only a signed-in user. With the phone method the "Needs
attention" card also says how many messages wait to be sent (DASH-2)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_login
from app.core.errors import AppError, render_error, unexpected_error
from app.core.templating import templates
from app.features.messaging import service as messaging
from app.features.reports import service as reports
from app.models import User

router = APIRouter()

DASHBOARD_PAGE = "dashboard/views/index.html"


@router.get("/")
def dashboard_page(request: Request, user: User = Depends(require_login), db: Session = Depends(get_db)) -> Response:
    try:
        context = {"user": user, "dashboard": reports.dashboard(db), "waiting_count": 0}
        # DASH-2: only with the phone method
        if messaging.sends_from_users_phone():
            context["waiting_count"] = messaging.waiting_count(db)
        return templates.TemplateResponse(request, DASHBOARD_PAGE, context)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the dashboard"))
