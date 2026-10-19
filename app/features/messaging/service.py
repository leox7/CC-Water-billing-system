"""Sending an SMS and keeping a record of every attempt (PRD section 4.6, SECURITY.md section 11).

Only bills/service.py calls this, for a saved bill or a correction (SMS-S1). The number is
always the one stored on the bill's customer, and nothing here can send any other text to
any other number (SMS-S2).

Every attempt is a messages_log row: written as pending before the provider is asked, then
marked sent or failed (SMS-3). A failed SMS is not an error for the user: it is recorded,
and the action that sent it still succeeds, so it can be sent again later (SMS-4). That
includes a crash inside the provider code (SMS-4a): no row is ever left pending by it.
"""

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, BusinessRuleError, ConflictError, TooManyAttemptsError, unexpected_error
from app.core.logging import EAT
from app.features.audit import service as audit
from app.features.messaging import providers
from app.models import Bill, Customer, MessageLog, User

logger = logging.getLogger("app.messaging")

# SMS-S3: attempts per bill per day, counting every type and failed attempts too, so a bug
# or a repeated click cannot run up SMS charges
DAILY_LIMIT_PER_BILL = 5


def send_message(db: Session, actor: User, bill: Bill, message_type: str, text: str) -> MessageLog:
    """Send `text` to the bill's customer. Returns the messages_log row, marked sent or failed.
    Raises TooManyAttemptsError when the bill has reached today's limit."""
    try:
        customer = db.get(Customer, bill.customer_id)
        _check_daily_limit(db, actor, bill)
        message = MessageLog(bill_id=bill.bill_id, customer_id=bill.customer_id, message_type=message_type,
                             content=text, status="pending")
        db.add(message)
        # SMS-3: the attempt is on record before the provider is asked, so it is never lost
        db.commit()
        try:
            provider_message_id = providers.send_sms(get_settings(), customer.phone, text)
        except providers.SmsSendError as exc:
            return _mark_failed(db, actor, message, str(exc))
        except Exception as exc:
            # SMS-4a: the one broad catch in the app (architecture.md section 5). A bug in the
            # provider code is a failed send, not a 500 page: the reading and bill are already
            # saved. Nothing is hidden: the row is marked failed, the stack trace is logged,
            # and sms.failed and system.error are recorded.
            return _mark_crashed(db, actor, message, exc)
        return _mark_sent(db, actor, message, provider_message_id)
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "send the SMS") from exc


def resend_message(db: Session, actor: User, bill: Bill) -> MessageLog:
    """SMS-4: send the bill's latest SMS again, only if it failed and the customer is active.
    It goes out with the same text and type, which are still right: anything that changes the
    bill sends a newer SMS."""
    try:
        latest = latest_message(db, bill.bill_id)
        customer = db.get(Customer, bill.customer_id)
        if customer.status != "active":
            # CUS-4: an inactive customer gets no SMS
            _record_refused_resend(actor, bill, latest, "customer inactive")
            raise BusinessRuleError("This customer is inactive, so no SMS is sent. Reactivate the customer first.")
        if latest is None or latest.status != "failed":
            _record_refused_resend(actor, bill, latest, "last message did not fail")
            raise ConflictError("Only an SMS that failed can be sent again, and this bill's last SMS did not fail. "
                                "Refresh the page to see where it is.")
        audit.record_event(
            db, "sms.resend_requested", f"Send again pressed for SMS {latest.message_id} on bill {bill.bill_id}",
            actor=actor, entity=latest, customer_id=bill.customer_id, details={"message_id": latest.message_id},
        )
        db.commit()
        return send_message(db, actor, bill, latest.message_type, latest.content)
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "send the SMS again") from exc


def latest_message(db: Session, bill_id: int) -> MessageLog | None:
    """The newest SMS attempt for a bill, or None when there is none yet."""
    try:
        query = select(MessageLog).where(MessageLog.bill_id == bill_id).order_by(MessageLog.message_id.desc())
        return db.scalars(query.limit(1)).first()
    except Exception as exc:
        raise unexpected_error(exc, "load the SMS") from exc


def _record_refused_resend(actor: User, bill: Bill, latest: MessageLog | None, reason: str) -> None:
    message_id = latest.message_id if latest is not None else None
    audit.record_problem(
        "sms.resend_refused", f"Send again refused for bill {bill.bill_id}: {reason}",
        actor=actor, entity=bill, customer_id=bill.customer_id,
        details={"bill_id": bill.bill_id, "message_id": message_id, "reason": reason},
    )


def _check_daily_limit(db: Session, actor: User, bill: Bill) -> None:
    # messages_log times are written in EAT, so the day starts at midnight in Kenya
    start_of_today = datetime.now(EAT).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    attempts_today = db.scalar(
        select(func.count()).select_from(MessageLog)
        .where(MessageLog.bill_id == bill.bill_id, MessageLog.created_at >= start_of_today)
    )
    if attempts_today < DAILY_LIMIT_PER_BILL:
        return
    audit.record_problem(
        "sms.send_blocked", f"SMS for bill {bill.bill_id} blocked: {DAILY_LIMIT_PER_BILL} already sent today",
        actor=actor, entity=bill, customer_id=bill.customer_id,
        details={"bill_id": bill.bill_id, "cap": DAILY_LIMIT_PER_BILL},
    )
    raise TooManyAttemptsError(
        f"This bill has already had {DAILY_LIMIT_PER_BILL} SMS today. Please send it again tomorrow."
    )


def _mark_sent(db: Session, actor: User, message: MessageLog, provider_message_id: str | None) -> MessageLog:
    message.status = "sent"
    message.provider_message_id = provider_message_id
    audit.record_event(
        db, "sms.sent", f"{message.message_type.capitalize()} SMS {message.message_id} sent for bill {message.bill_id}",
        actor=actor, entity=message, customer_id=message.customer_id,
        details={"message_id": message.message_id, "type": message.message_type,
                 "provider_message_id": provider_message_id},
    )
    db.commit()
    return message


def _mark_failed(db: Session, actor: User, message: MessageLog, reason: str) -> MessageLog:
    message.status = "failed"
    message.failure_reason = reason
    audit.record_event(
        db, "sms.failed", f"{message.message_type.capitalize()} SMS {message.message_id} failed for bill {message.bill_id}",
        actor=actor, entity=message, customer_id=message.customer_id,
        details={"message_id": message.message_id, "type": message.message_type, "error": reason},
    )
    db.commit()
    return message


def _mark_crashed(db: Session, actor: User, message: MessageLog, exc: Exception) -> MessageLog:
    # whatever the crash left unfinished in the session is undone; the pending row was
    # committed before the provider was asked, so it is still there to mark
    db.rollback()
    error_type = type(exc).__name__
    logger.error("The SMS code failed while sending message %s", message.message_id, exc_info=exc)
    audit.record_problem(
        "system.error", f"Unexpected error while trying to send SMS {message.message_id}",
        actor=actor, entity=message, customer_id=message.customer_id,
        # the stack trace stays in the application log, not here
        details={"error_type": error_type, "action": "send the SMS", "message_id": message.message_id},
    )
    return _mark_failed(db, actor, message, f"The app failed while sending ({error_type}). See the server log.")
