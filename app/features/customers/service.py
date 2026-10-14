"""Customers (PRD section 4.2): the list, adding and changing a customer, deactivating and
reactivating, and removing a customer who has no history.

Owner and member do this work alike (PRD section 3), so any signed-in user may change a
customer. `actor` is who did it, for the audit log. Customer details are read from the form
one named field at a time, so a form can never set the status or anything else (IN-7).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    AppError,
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    unexpected_error,
)
from app.core.validation import clean_name, clean_text, normalise_phone, parse_money
from app.features.audit import service as audit
from app.models import Bill, Customer, MessageLog, MeterReading, User

LOCATION_MAX_LENGTH = 100
METER_NUMBER_MAX_LENGTH = 20
# IN-5: letters, digits, hyphens and slashes, saved in capitals. There is no fixed format.
METER_NUMBER_FORMAT = re.compile(r"^[A-Z0-9/-]+$")
SEARCH_MAX_LENGTH = 100
# REQ-3: a list never returns more rows than this. Search finds the others.
LIST_LIMIT = 200


@dataclass
class CustomerRow:
    customer: Customer
    # CUS-3: a customer with readings, bills or messages is deactivated, never removed
    has_history: bool


def list_customers(db: Session, search: str = "") -> list[CustomerRow]:
    """Active customers first, then inactive, each by name. `search` matches the name, the
    meter number or the location."""
    try:
        term = _clean_search(search)
        query = select(Customer).order_by(Customer.status, Customer.first_name, Customer.last_name)
        if term:
            full_name = func.concat(Customer.first_name, " ", Customer.last_name)
            # autoescape: a % or _ typed in the box is searched for as itself
            query = query.where(or_(
                full_name.contains(term, autoescape=True),
                Customer.meter_number.contains(term, autoescape=True),
                Customer.location.contains(term, autoescape=True),
            ))
        customers = db.scalars(query.limit(LIST_LIMIT)).all()
        with_history = _customers_with_history(db)
        return [CustomerRow(customer, customer.customer_id in with_history) for customer in customers]
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "list the customers") from exc


def count_active_customers(db: Session) -> int:
    try:
        return db.scalar(select(func.count()).select_from(Customer).where(Customer.status == "active")) or 0
    except Exception as exc:
        raise unexpected_error(exc, "count the customers") from exc


def get_customer(db: Session, customer_id: int) -> Customer:
    try:
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise NotFoundError("We could not find that customer.")
        return customer
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the customer") from exc


def default_charges() -> dict[str, Decimal]:
    """CUS-1: what a new customer pays unless the user types something else. A tiny pure
    helper: it only reads the settings, which are loaded once."""
    settings = get_settings()
    return {"rate_per_unit": settings.default_rate_per_unit, "service_charge": settings.default_service_charge}


def customer_row(db: Session, customer: Customer) -> CustomerRow:
    """One customer with whether they have history, for the row shown after a change."""
    try:
        return CustomerRow(customer, _has_history(db, customer.customer_id))
    except Exception as exc:
        raise unexpected_error(exc, "load the customer") from exc


def create_customer(db: Session, actor: User, typed: Mapping[str, str]) -> Customer:
    """CUS-1: a user adds a customer. A new customer is always active."""
    try:
        details = _clean_customer_details(db, typed)
        customer = Customer(**details)
        db.add(customer)
        db.flush()
        audit.record_event(
            db, "customer.created", f"Customer {customer.meter_number} added",
            actor=actor, entity=customer, customer_id=customer.customer_id,
            details={"meter_number": customer.meter_number, "rate_per_unit": customer.rate_per_unit,
                     "service_charge": customer.service_charge},
        )
        db.commit()
        return customer
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "add the customer") from exc


def update_customer(db: Session, actor: User, customer_id: int, typed: Mapping[str, str]) -> Customer:
    """CUS-2: a user changes any detail. Bills already made keep their own rate (BILL-5)."""
    try:
        customer = get_customer(db, customer_id)
        changes = {}
        for name, new_value in _clean_customer_details(db, typed, customer_id).items():
            old_value = getattr(customer, name)
            if old_value != new_value:
                changes[name] = (old_value, new_value)
                setattr(customer, name, new_value)
        if not changes:
            return customer
        audit.record_event(
            db, "customer.updated", f"Customer {customer.meter_number} changed: {', '.join(changes)}",
            actor=actor, entity=customer, customer_id=customer.customer_id, details=_describe_changes(changes),
        )
        db.commit()
        return customer
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "save the customer") from exc


def deactivate_customer(db: Session, actor: User, customer_id: int) -> Customer:
    """CUS-3, CUS-4: an inactive customer keeps their history and gets no new bills or SMS."""
    try:
        customer = get_customer(db, customer_id)
        if customer.status == "inactive":
            return customer
        customer.status = "inactive"
        audit.record_event(
            db, "customer.deactivated", f"Customer {customer.meter_number} deactivated",
            actor=actor, entity=customer, customer_id=customer.customer_id,
        )
        db.commit()
        return customer
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "deactivate the customer") from exc


def reactivate_customer(db: Session, actor: User, customer_id: int) -> Customer:
    try:
        customer = get_customer(db, customer_id)
        if customer.status == "active":
            return customer
        customer.status = "active"
        audit.record_event(
            db, "customer.reactivated", f"Customer {customer.meter_number} reactivated",
            actor=actor, entity=customer, customer_id=customer.customer_id,
        )
        db.commit()
        return customer
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "reactivate the customer") from exc


def remove_customer(db: Session, actor: User, customer_id: int) -> str:
    """CUS-3: only a customer with no readings, bills or messages may be removed. Returns
    their name, for the message."""
    try:
        customer = get_customer(db, customer_id)
        name = f"{customer.first_name} {customer.last_name}"
        if _has_history(db, customer_id):
            raise BusinessRuleError(
                f"{name} has readings or bills, so the record cannot be removed. Deactivate the customer instead."
            )
        meter_number = customer.meter_number
        db.delete(customer)
        db.flush()
        audit.record_event(
            db, "customer.removed", f"Customer {meter_number} removed",
            actor=actor, entity=customer, customer_id=customer_id, details={"meter_number": meter_number},
        )
        db.commit()
        return name
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "remove the customer") from exc


def _clean_customer_details(db: Session, typed: Mapping[str, str], customer_id: int | None = None) -> dict[str, Any]:
    """Checks every field, in the order the form shows them, and returns the clean values.
    Only the fields named here are read."""
    defaults = default_charges()
    details: dict[str, Any] = {
        "first_name": clean_name(typed.get("first_name", ""), "first_name", "first name"),
        "last_name": clean_name(typed.get("last_name", ""), "last_name", "last name"),
        "phone": normalise_phone(typed.get("phone", "")),
        "location": _clean_location(typed.get("location", "")),
        "meter_number": _clean_meter_number(typed.get("meter_number", "")),
    }
    _check_meter_number_free(db, details["meter_number"], customer_id)
    details["rate_per_unit"] = _money_or_default(
        typed.get("rate_per_unit", ""), defaults["rate_per_unit"], "rate_per_unit", "rate per unit", "130"
    )
    if details["rate_per_unit"] == 0:
        raise BusinessRuleError("The rate per unit must be more than 0.", field="rate_per_unit")
    details["service_charge"] = _money_or_default(
        typed.get("service_charge", ""), defaults["service_charge"], "service_charge", "service charge", "150"
    )
    return details


def _money_or_default(value: str, default: Decimal, field: str, label: str, example: str) -> Decimal:
    # CUS-1: an empty box takes the default from the settings (Ksh 130 per unit, Ksh 150)
    if clean_text(value) == "":
        return default
    return parse_money(value, field, label, example)


def _clean_location(value: str) -> str:
    location = clean_text(value)
    if not location:
        raise BusinessRuleError("Please enter the location, for example Kiawara Estate.", field="location")
    if len(location) > LOCATION_MAX_LENGTH:
        raise BusinessRuleError(
            f"The location is too long. Use {LOCATION_MAX_LENGTH} characters or fewer.", field="location"
        )
    return location


def _clean_meter_number(value: str) -> str:
    # IN-5: mtr-0032 is saved as MTR-0032
    meter_number = clean_text(value).upper()
    if not meter_number:
        raise BusinessRuleError("Please enter the meter number, for example MTR-0032.", field="meter_number")
    if len(meter_number) > METER_NUMBER_MAX_LENGTH:
        raise BusinessRuleError(
            f"The meter number is too long. Use {METER_NUMBER_MAX_LENGTH} characters or fewer.", field="meter_number"
        )
    if not METER_NUMBER_FORMAT.match(meter_number):
        raise BusinessRuleError(
            "A meter number can only have letters, numbers, - and /, for example MTR-0032.", field="meter_number"
        )
    return meter_number


def _check_meter_number_free(db: Session, meter_number: str, customer_id: int | None) -> None:
    # CUS-1: each meter belongs to one customer. The customer being changed may keep their own.
    customer_with_meter = db.scalar(select(Customer).where(Customer.meter_number == meter_number))
    if customer_with_meter is not None and customer_with_meter.customer_id != customer_id:
        raise ConflictError(
            f"Meter number {meter_number} already belongs to {customer_with_meter.first_name} {customer_with_meter.last_name}.",
            field="meter_number",
        )


def _clean_search(search: str) -> str:
    # REQ-3: an unreasonable search is refused, not cut short without saying so
    term = clean_text(search)
    if len(term) > SEARCH_MAX_LENGTH:
        raise BusinessRuleError(f"Please search with {SEARCH_MAX_LENGTH} characters or fewer.", field="search")
    return term


def _describe_changes(changes: dict[str, tuple[Any, Any]]) -> dict[str, Any]:
    # AUDIT_LOG.md: only what changed, old and new. A phone number is never written to the
    # audit log (rule 7), so a new phone shows only as a name in "fields".
    details: dict[str, Any] = {"fields": list(changes)}
    for name, (old_value, new_value) in changes.items():
        if name != "phone":
            details[name] = {"old": str(old_value), "new": str(new_value)}
    return details


def _customers_with_history(db: Session) -> set[int]:
    with_history = set(db.scalars(select(MeterReading.customer_id).distinct()))
    with_history |= set(db.scalars(select(Bill.customer_id).distinct()))
    with_history |= set(db.scalars(select(MessageLog.customer_id).distinct()))
    return with_history


def _has_history(db: Session, customer_id: int) -> bool:
    # a message can outlive its reading and bill (COR-4), so it counts as history too
    for model in (MeterReading, Bill, MessageLog):
        if db.scalar(select(model.customer_id).where(model.customer_id == customer_id).limit(1)):
            return True
    return False
