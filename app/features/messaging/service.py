"""Sending an SMS and keeping a record of every attempt (PRD section 4.6, SECURITY.md section 11).

Only bills/service.py calls this, for a saved bill or a correction (SMS-S1). The number is
always the one stored on the bill's customer, and nothing here can send any other text to
any other number (SMS-S2).

Every attempt is a messages_log row: written as pending before the provider is asked, then
marked sent or failed (SMS-3). A failed SMS is not an error for the user: it is recorded,
and the action that sent it still succeeds, so it can be sent again later (SMS-4). That
includes a crash inside the provider code (SMS-4a): no row is ever left pending by it.

With the phone method (SMS_BACKEND=phone, SMS-14 to SMS-22) nothing is sent from here. The
message is written as ready, a user sends it from their own phone and marks it sent, and a
ready message whose bill is deleted becomes cancelled. bills/service.py builds the text each
time, because a correction's balance is a bills rule.
"""

import logging
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    AppError,
    BusinessRuleError,
    ConflictError,
    InternalError,
    NotFoundError,
    TooManyAttemptsError,
    unexpected_error,
)
from app.core.logging import EAT
from app.features.audit import service as audit
from app.features.messaging import providers
from app.features.messaging.templates import text_fingerprint
from app.models import Bill, Customer, MessageLog, User

logger = logging.getLogger("app.messaging")

# SMS-S3: attempts per bill per day, counting every type and failed attempts too, so a bug
# or a repeated click cannot run up SMS charges
DAILY_LIMIT_PER_BILL = 5
# REQ-3: the Messages to send screen never lists more than this
WAITING_LIST_LIMIT = 200


class BillChangedError(ConflictError):
    """SMS-17: the bill or its text changed after the Messages to send screen was shown."""

    default_message = "This bill changed. Open the message again."


class MessageNotQueuedError(InternalError):
    """SMS-14: the ready message could not be written. The reading and bill were saved
    before, so the caller says so instead of showing a 500 page. The stack trace is logged
    and system.error is recorded before this is raised."""

    default_message = ("The reading and bill are saved, but the SMS could not be prepared. "
                       "Open the bill and press Send again.")


def send_message(db: Session, actor: User, bill: Bill, message_type: str, text: str) -> MessageLog:
    """Send `text` to the bill's customer. Returns the messages_log row, marked sent or failed.
    Raises TooManyAttemptsError when the bill has reached today's limit. With the phone
    method the message is queued as ready instead (queue_message)."""
    try:
        if providers.sends_from_users_phone(get_settings()):
            return queue_message(db, actor, bill, message_type, text)
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


def sends_from_users_phone() -> bool:
    """SMS-1: True when SMS_BACKEND=phone, for the screens and for bills/service.py."""
    return providers.sends_from_users_phone(get_settings())


def queue_message(db: Session, actor: User, bill: Bill, message_type: str, text: str) -> MessageLog:
    """SMS-14: after a reading or a correction is saved, with the phone method. Writes a ready
    row and calls no provider. When a ready message of this type already waits for the bill,
    that one is returned and nothing new is written: its text is rebuilt from the bill each
    time it is shown (SMS-19). Raises TooManyAttemptsError at the daily limit, and
    MessageNotQueuedError when the row could not be written."""
    try:
        waiting = waiting_message(db, bill.bill_id, message_type)
        if waiting is not None:
            return waiting
        return _write_ready_row(db, actor, bill, message_type, text)
    except TooManyAttemptsError:
        raise
    except Exception as exc:
        db.rollback()
        raise _not_queued(actor, bill, exc) from exc


def resend_from_phone(db: Session, actor: User, bill: Bill, message_type: str, text: str) -> MessageLog:
    """SMS-18: Send again with the phone method writes a new ready row. Refused for an
    inactive customer (CUS-4) and while a ready message of the same type waits for the bill.
    `text` is built from the bill by bills/service.py, never taken from the browser (SMS-S2)."""
    try:
        latest = latest_message(db, bill.bill_id)
        customer = db.get(Customer, bill.customer_id)
        if customer.status != "active":
            _record_refused_resend(actor, bill, latest, "customer inactive")
            raise BusinessRuleError("This customer is inactive, so no SMS is sent. Reactivate the customer first.")
        if waiting_message(db, bill.bill_id, message_type) is not None:
            _record_refused_resend(actor, bill, latest, "message already waiting")
            raise ConflictError("A message is already waiting to be sent.")
        audit.record_event(
            db, "sms.resend_requested", f"Send again pressed for bill {bill.bill_id} (phone method)",
            actor=actor, entity=latest or bill, customer_id=bill.customer_id,
            details={"message_id": latest.message_id if latest else None},
        )
        db.commit()
        return _write_ready_row(db, actor, bill, message_type, text)
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "prepare the SMS again") from exc


def mark_sent(db: Session, actor: User, message_id: int, text: str, shown_total: str,
              shown_fingerprint: str) -> MessageLog:
    """SMS-17, SMS-S4: the user sent the text from their phone and pressed Mark as sent.
    `text` is the message rebuilt from the bill now; `shown_total` and `shown_fingerprint`
    are what the screen showed. The row must still be ready, the customer active, and the
    bill unchanged, so the text stored is exactly the one the user saw (SMS-19). It runs
    once: the row is locked, so a second press at the same moment waits and is then refused."""
    try:
        message = db.get(MessageLog, message_id, with_for_update=True, populate_existing=True)
        if message is None:
            raise NotFoundError("We could not find that message.")
        bill = db.get(Bill, message.bill_id) if message.bill_id is not None else None
        _check_can_mark(db, actor, message, bill, text, shown_total, shown_fingerprint)
        message.status = "sent"
        message.content = text
        message.sent_by_user_id = actor.user_id
        audit.record_event(
            db, "sms.marked_sent",
            f"{message.message_type.capitalize()} SMS {message.message_id} marked as sent for bill {bill.bill_id}",
            actor=actor, entity=message, customer_id=message.customer_id,
            details={"message_id": message.message_id, "type": message.message_type, "bill_id": bill.bill_id,
                     "marked_by": actor.user_id},
        )
        db.commit()
        return message
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "mark the SMS as sent") from exc


def cancel_waiting(db: Session, actor: User, bill: Bill) -> None:
    """SMS-20, COR-4: the bill is being deleted, so its ready messages will never be sent.
    They become cancelled and are kept. Called before the bill is deleted, inside the
    caller's transaction; the caller commits."""
    try:
        query = select(MessageLog).where(MessageLog.bill_id == bill.bill_id, MessageLog.status == "ready")
        for message in db.scalars(query).all():
            message.status = "cancelled"
            audit.record_event(
                db, "sms.cancelled", f"SMS {message.message_id} cancelled: bill {bill.bill_id} was deleted",
                actor=actor, entity=message, customer_id=message.customer_id,
                details={"message_id": message.message_id, "bill_id": bill.bill_id},
            )
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "cancel the waiting SMS") from exc


def waiting_message(db: Session, bill_id: int, message_type: str) -> MessageLog | None:
    """The ready message of this type waiting for the bill, or None."""
    try:
        query = select(MessageLog).where(MessageLog.bill_id == bill_id, MessageLog.message_type == message_type,
                                         MessageLog.status == "ready")
        return db.scalars(query.order_by(MessageLog.message_id).limit(1)).first()
    except Exception as exc:
        raise unexpected_error(exc, "load the waiting SMS") from exc


def waiting_messages(db: Session) -> list[MessageLog]:
    """SMS-15: every ready message of an active customer, oldest first."""
    try:
        query = _waiting_query(select(MessageLog)).order_by(MessageLog.created_at, MessageLog.message_id)
        return list(db.scalars(query.limit(WAITING_LIST_LIMIT)))
    except Exception as exc:
        raise unexpected_error(exc, "list the messages to send") from exc


def waiting_count(db: Session) -> int:
    """How many messages the Messages to send screen lists, for its buttons and DASH-2."""
    try:
        return db.scalar(_waiting_query(select(func.count()).select_from(MessageLog))) or 0
    except Exception as exc:
        raise unexpected_error(exc, "count the messages to send") from exc


def latest_message(db: Session, bill_id: int) -> MessageLog | None:
    """The newest SMS attempt for a bill, or None when there is none yet."""
    try:
        query = select(MessageLog).where(MessageLog.bill_id == bill_id).order_by(MessageLog.message_id.desc())
        return db.scalars(query.limit(1)).first()
    except Exception as exc:
        raise unexpected_error(exc, "load the SMS") from exc


def _waiting_query(query: Select) -> Select:
    # SMS-15: ready, of an active customer (CUS-4), and still on a bill
    return (query.join(Customer, Customer.customer_id == MessageLog.customer_id)
            .where(MessageLog.status == "ready", MessageLog.bill_id.is_not(None), Customer.status == "active"))


def _write_ready_row(db: Session, actor: User, bill: Bill, message_type: str, text: str) -> MessageLog:
    # SMS-21: a ready row counts towards the daily limit like any other attempt
    _check_daily_limit(db, actor, bill)
    # SMS-19: today's text is stored, so content is never empty. It is replaced by the text
    # the user saw when the message is marked sent.
    message = MessageLog(bill_id=bill.bill_id, customer_id=bill.customer_id, message_type=message_type,
                         content=text, status="ready")
    db.add(message)
    db.flush()
    audit.record_event(
        db, "sms.queued", f"{message_type.capitalize()} SMS {message.message_id} ready to send for bill {bill.bill_id}",
        actor=actor, entity=message, customer_id=bill.customer_id,
        details={"message_id": message.message_id, "type": message_type, "bill_id": bill.bill_id},
    )
    db.commit()
    return message


def _check_can_mark(db: Session, actor: User, message: MessageLog, bill: Bill | None, text: str,
                    shown_total: str, shown_fingerprint: str) -> None:
    if message.status != "ready" or bill is None:
        # a second press, a message marked from another phone, or a bill deleted since
        raise _refused_mark(db, actor, message, "message not ready", ConflictError(
            "This message is no longer waiting to be sent. Refresh the page to see the list."))
    customer = db.get(Customer, message.customer_id)
    if customer.status != "active":
        # CUS-4
        raise _refused_mark(db, actor, message, "customer inactive", BusinessRuleError(
            "This customer is inactive, so no SMS is sent. Reactivate the customer first."))
    # SMS-17, SMS-19: both the total and the whole text must be the ones on the screen
    if shown_total != str(bill.total_due) or shown_fingerprint != text_fingerprint(text):
        raise _refused_mark(db, actor, message, "bill changed", BillChangedError())


def _refused_mark(db: Session, actor: User, message: MessageLog, reason: str, error: AppError) -> AppError:
    """Lets go of the lock on the row, then records the refusal on its own so it is kept
    (AUDIT_LOG.md rule 3). A refused Mark as sent is recorded as sms.resend_refused.
    Returns the error to raise."""
    message_id, bill_id, customer_id = message.message_id, message.bill_id, message.customer_id
    db.rollback()
    audit.record_problem(
        "sms.resend_refused", f"Mark as sent refused for SMS {message_id}: {reason}",
        actor=actor, entity=message, customer_id=customer_id,
        details={"bill_id": bill_id, "message_id": message_id, "reason": reason},
    )
    return error


def _not_queued(actor: User, bill: Bill, exc: Exception) -> MessageNotQueuedError:
    # The caller shows a plain message instead of a 500 page, so render_error never sees
    # this error. It is logged and recorded here instead, the way render_error would.
    error_type = type(exc).__name__
    logger.error("The SMS for bill %s could not be queued", bill.bill_id, exc_info=exc)
    audit.record_problem(
        "system.error", f"Unexpected error while trying to queue the SMS for bill {bill.bill_id}",
        actor=actor, entity=bill, customer_id=bill.customer_id,
        details={"error_type": error_type, "action": "queue the SMS", "bill_id": bill.bill_id},
    )
    return MessageNotQueuedError(action="queue the SMS", cause_type=error_type)


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
