"""Routes for the Messages to send screen, the phone method (PRD SMS-15 to SMS-17).

Owner and member both send messages (PRD section 3), so every route needs only a signed-in
user (require_login). The texts and links are built by bills/service.py from the saved
bills; nothing here takes a message text or a number from the browser (SMS-S2).

- /messages/to-send: every ready message of an active customer, oldest first. The page holds
  names, numbers and amounts, so the browser is told not to keep a copy (SMS-S4).
- /messages/{message_id}/mark-sent: Mark as sent, sent by htmx with the CSRF token. The form
  carries the total and the fingerprint of the text that was shown. The answer replaces the
  card: a success message, the card again with the new text when the bill changed, or the
  refusal. Without htmx it is a normal post that comes back to the list.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_login
from app.core.errors import AppError, render_error, unexpected_error
from app.core.templating import is_htmx_request, templates
from app.features.bills import service as bills
from app.features.messaging import service as messaging
from app.models import User

router = APIRouter()

TO_SEND_PAGE = "messaging/views/to_send.html"
CARD = "messaging/views/_card.html"
MARKED = "messaging/views/_marked.html"
# SMS-S4: customer names, numbers and amounts are not kept by the browser or a proxy
NO_STORE = {"Cache-Control": "no-store"}
# how a Mark as sent that was a normal form post ended, from the address of the list
MARKED_NOTICE = "marked"


@router.get("/messages/to-send")
def to_send_page(
    request: Request, notice: str = "", user: User = Depends(require_login), db: Session = Depends(get_db)
) -> Response:
    try:
        context = {"user": user, "cards": bills.messages_to_send(db),
                   "sends_from_phone": messaging.sends_from_users_phone(),
                   # only a known word is used, never what was typed in the address
                   "marked": notice == MARKED_NOTICE}
        return templates.TemplateResponse(request, TO_SEND_PAGE, context, headers=NO_STORE)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "show the messages to send"))


@router.post("/messages/{message_id}/mark-sent")
def mark_sent(
    request: Request,
    message_id: int,
    total_due: str = Form(""),
    fingerprint: str = Form(""),
    user: User = Depends(require_login),
    db: Session = Depends(get_db),
) -> Response:
    try:
        message = bills.mark_sent(db, user, message_id, total_due, fingerprint)
        if not is_htmx_request(request):
            # a plain form post: back to the list, so a refresh does not post it again
            return RedirectResponse(f"/messages/to-send?notice={MARKED_NOTICE}", status_code=303)
        context = {"customer": bills.bill_details(db, message.bill_id).customer}
        return templates.TemplateResponse(request, MARKED, context, headers=NO_STORE)
    except messaging.BillChangedError as exc:
        if not is_htmx_request(request):
            return render_error(request, exc)
        # the card comes back with the new text and the warning on it
        context = {"card": bills.message_to_send(db, message_id), "card_error": exc.message}
        return templates.TemplateResponse(request, CARD, context, status_code=exc.status_code, headers=NO_STORE)
    except AppError as exc:
        return render_error(request, exc)
    except Exception as exc:
        return render_error(request, unexpected_error(exc, "mark the SMS as sent"))
