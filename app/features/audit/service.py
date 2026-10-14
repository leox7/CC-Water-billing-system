"""The audit log writer (docs/AUDIT_LOG.md).

Every service that changes data calls record_event() for a success, or
record_problem() for a rejection or an error. Writing an audit row must never stop
the user's action (rule 4), so a failure here is logged and then ignored.
"""

import json
import logging
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.database import Base, get_session_factory
from app.core.logging import client_ip_var, mask_phone_numbers, request_id_var
from app.models import AuditLog, User

logger = logging.getLogger("app.audit")

# AUDIT_LOG.md section 4: the only events that may be recorded, and the level of each.
# To add an event, add it to that document first (rule 10).
EVENT_LEVELS = {
    # sign in and accounts
    "auth.owner_registered": "info",
    "auth.login_success": "info",
    "auth.login_failed": "warning",
    "auth.login_locked": "warning",
    "auth.logout": "info",
    "auth.session_rejected": "warning",
    "auth.password_changed": "info",
    "auth.access_denied": "warning",
    "auth.csrf_failed": "warning",
    "user.created": "info",
    "user.deactivated": "info",
    "user.reactivated": "info",
    "user.deleted": "info",
    "user.password_reset": "info",
    # customers
    "customer.created": "info",
    "customer.updated": "info",
    "customer.deactivated": "info",
    "customer.reactivated": "info",
    "customer.removed": "info",
    # readings
    "reading.created": "info",
    "reading.rejected": "warning",
    "reading.updated": "info",
    "reading.deleted": "info",
    # bills
    "bill.generated": "info",
    "bill.recalculated": "info",
    "bill.deleted": "info",
    "bill.status_changed": "info",
    # messages
    "sms.sent": "info",
    "sms.failed": "warning",
    "sms.resend_requested": "info",
    "sms.send_blocked": "warning",
    "sms.resend_refused": "warning",
    # payments
    "payment.recorded": "info",
    "payment.rejected": "warning",
    # system
    "system.started": "info",
    "system.error": "error",
}

ENTITY_TYPES = {
    "users": "user",
    "customers": "customer",
    "meter_readings": "reading",
    "bills": "bill",
    "payments": "payment",
    "messages_log": "message",
}

# LOG-2: these never go into an audit row. A detail whose name contains one of
# these words is dropped.
FORBIDDEN_DETAIL_WORDS = (
    "password", "hash", "token", "secret", "api_key", "cookie", "csrf", "authorization", "phone",
)
SUMMARY_MAX_LENGTH = 255
ACTOR_LABEL_MAX_LENGTH = 100


def record_event(
    db: Session,
    event_type: str,
    summary: str,
    actor: User | None = None,
    entity: Base | None = None,
    customer_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a success inside the caller's transaction (rule 2).

    The row is written in a savepoint, so if the audit insert fails only the audit
    row is lost and the caller's own work carries on (rule 4). The entity must
    already have its id, so flush a new record before calling.
    """
    # The caller's own pending changes are saved first, outside the try below, so
    # an error in the caller's work is raised to the caller and not hidden here.
    db.flush()
    try:
        row = _build_audit_row(event_type, summary, actor, entity, customer_id, details)
        with db.begin_nested():
            db.add(row)
    except Exception:
        # Rule 4: the audit log must never stop the user's action. Say so in the log.
        logger.error("Audit event %s could not be saved", event_type, exc_info=True)


def record_problem(
    event_type: str,
    summary: str,
    actor: User | None = None,
    entity: Base | None = None,
    customer_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a rejection or an error in its own short session (rule 3).

    It is committed on its own, so it survives the rollback that follows a failure.
    """
    try:
        row = _build_audit_row(event_type, summary, actor, entity, customer_id, details)
        session = _open_problem_session()
        try:
            session.add(row)
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.error("Audit problem %s could not be saved", event_type, exc_info=True)


def _open_problem_session() -> Session:
    # A function of its own so tests can point it at the test transaction.
    return get_session_factory()()


def _build_audit_row(
    event_type: str,
    summary: str,
    actor: User | None,
    entity: Base | None,
    customer_id: int | None,
    details: dict[str, Any] | None,
) -> AuditLog:
    if event_type not in EVENT_LEVELS:
        raise ValueError(f"{event_type} is not an event in the audit catalogue")

    entity_type, entity_id = _describe_entity(entity)
    return AuditLog(
        level=EVENT_LEVELS[event_type],
        event_type=event_type,
        summary=mask_phone_numbers(summary)[:SUMMARY_MAX_LENGTH],
        actor_user_id=actor.user_id if actor else None,
        actor_label=_actor_label(actor),
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        request_id=request_id_var.get(),
        ip_address=client_ip_var.get(),
        details=_clean_details(details),
    )


def _actor_label(actor: User | None) -> str | None:
    if actor is None:
        return None
    # a snapshot, so the row still reads right if the user is renamed later
    label = f"{actor.first_name} ({actor.role})"
    return label[:ACTOR_LABEL_MAX_LENGTH]


def _describe_entity(entity: Base | None) -> tuple[str | None, int | None]:
    if entity is None:
        return None, None
    entity_type = ENTITY_TYPES[entity.__tablename__]
    # identity does not load anything, so this is safe even after a rollback
    identity = inspect(entity).identity
    return entity_type, identity[0] if identity else None


def _clean_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if details is None:
        return None
    # Money, dates and other values become text, so they fit in a JSON column
    json_safe = json.loads(json.dumps(details, default=str))
    return _remove_secrets(json_safe)


def _remove_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        kept = {}
        for name, item in value.items():
            if any(word in name.lower() for word in FORBIDDEN_DETAIL_WORDS):
                continue
            kept[name] = _remove_secrets(item)
        return kept
    if isinstance(value, list):
        return [_remove_secrets(item) for item in value]
    if isinstance(value, str):
        return mask_phone_numbers(value)
    return value
