"""Meter readings (PRD sections 4.3 and 4.4): entering a reading, which makes the bill and
sends the SMS in one save, and correcting a reading.

Flow A (architecture.md section 5): the reading and its bill are saved in one transaction
(DB-7), and only then is the bill SMS sent. A failed SMS leaves both saved (RD-6).

Flow D: correct_reading() holds the three-case rule. The user types the right number, and
the state of the bill decides what happens to the old records (PRD 4.4).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    AppError,
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    TooManyAttemptsError,
    unexpected_error,
)
from app.core.logging import EAT
from app.core.templating import period_label, plain_number
from app.core.validation import clean_text, parse_money
from app.features.audit import service as audit
from app.features.bills import service as bills
from app.features.messaging.service import MessageNotQueuedError
from app.models import Bill, Customer, MeterReading, User

CENTS = Decimal("0.01")
NOTHING = Decimal("0.00")
# how much of what was typed goes into a reading.rejected row
TRIED_MAX_LENGTH = 30
SEARCH_MAX_LENGTH = 100
# REQ-3: the picker never lists more than this. Search finds the others.
LIST_LIMIT = 200
RECENT_LIMIT = 10

# PRD 4.4: the three correction cases, in the words the audit log uses
NO_BILL = "no bill"
BILL_NOT_SENT = "bill not sent"
BILL_SENT_OR_PAID = "bill sent or paid"


@dataclass
class ReadingResult:
    """What a save did, so the screen can say so."""

    reading: MeterReading
    bill: Bill
    customer: Customer
    # the SMS: sent, failed or blocked (the daily limit, SMS-S3); with the phone method ready,
    # or not_queued when the ready message could not be written (SMS-14)
    sms_status: str
    # for a correction, which of the three cases ran
    correction_case: str | None = None


@dataclass
class DeletedReading:
    customer: Customer
    billing_period: str


@dataclass
class CheckedReading:
    """A new reading that passed every check, ready to save."""

    reading_date: date
    billing_period: str
    previous: Decimal
    current: Decimal
    # CUS-5: what the customer owed on paper, for their first bill only
    opening_balance: Decimal = NOTHING


@dataclass
class CorrectedValues:
    """What a correction changes. The previous reading and the opening balance change only on
    a customer's first reading (CUS-5); opening_balance is None otherwise."""

    previous: Decimal
    current: Decimal
    opening_balance: Decimal | None


@dataclass
class CustomerToRead:
    """One line of the customer picker."""

    customer: Customer
    read_this_month: bool


@dataclass
class ReadingEntry:
    """What the screen shows above the reading field."""

    customer: Customer
    # RD-2: what the new reading starts from, never typed
    previous: Decimal
    # CUS-5: no reading yet, so the first one starts from 0
    is_first: bool
    # the reading already saved for this month, if there is one
    this_month: MeterReading | None


@dataclass
class RecentReading:
    reading: MeterReading
    customer: Customer
    # COR-3: only the customer's latest reading can be fixed
    is_latest: bool


@dataclass
class ReadingFix:
    """What the Fix screen needs to say which case will run."""

    reading: MeterReading
    customer: Customer
    bill: Bill | None
    case: str
    is_latest: bool
    sms_was_sent: bool
    # CUS-5: the customer's first reading, whose previous reading and opening balance were typed
    is_first: bool


# ---- what the readings screen shows ----

def today() -> date:
    """Today in Kenya (EAT, no daylight saving)."""
    return datetime.now(EAT).date()


def current_period() -> str:
    """This month in Kenya, as YYYY-MM. "Read this month" means a reading for this period."""
    return today().strftime("%Y-%m")


def customers_to_read(db: Session, search: str = "") -> list[CustomerToRead]:
    """Active customers, the ones not read this month first, each group by name (DESIGN_SYSTEM.md
    section 8). `search` matches the name, the meter number or the location."""
    try:
        term = _clean_search(search)
        query = (select(Customer).where(Customer.status == "active")
                 .order_by(Customer.first_name, Customer.last_name))
        if term:
            full_name = func.concat(Customer.first_name, " ", Customer.last_name)
            # autoescape: a % or _ typed in the box is searched for as itself
            query = query.where(or_(
                full_name.contains(term, autoescape=True),
                Customer.meter_number.contains(term, autoescape=True),
                Customer.location.contains(term, autoescape=True),
            ))
        customers = db.scalars(query.limit(LIST_LIMIT)).all()
        read_ids = _customers_read_this_month(db)
        rows = [CustomerToRead(customer, customer.customer_id in read_ids) for customer in customers]
        # sorted() keeps the name order inside each group
        return sorted(rows, key=lambda row: row.read_this_month)
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "list the customers to read") from exc


def reading_progress(db: Session) -> tuple[int, int]:
    """How many active customers are read this month, out of how many."""
    try:
        active_ids = set(db.scalars(select(Customer.customer_id).where(Customer.status == "active")))
        return len(active_ids & _customers_read_this_month(db)), len(active_ids)
    except Exception as exc:
        raise unexpected_error(exc, "count the readings") from exc


def next_customer_to_read(db: Session) -> Customer | None:
    """The first active customer not read this month, for "Next customer". None when all are read."""
    try:
        for row in customers_to_read(db):
            if not row.read_this_month:
                return row.customer
        return None
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "find the next customer") from exc


def entry_details(db: Session, customer_id: int) -> ReadingEntry:
    try:
        customer = _get_customer(db, customer_id)
        latest = latest_reading(db, customer_id)
        if latest is None:
            return ReadingEntry(customer, NOTHING, True, None)
        this_month = latest if latest.billing_period == current_period() else None
        return ReadingEntry(customer, latest.current_reading, False, this_month)
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the customer") from exc


def recent_readings(db: Session) -> list[RecentReading]:
    """The newest readings entered, newest first, each with its customer."""
    try:
        query = select(MeterReading).order_by(MeterReading.reading_id.desc()).limit(RECENT_LIMIT)
        recent = []
        for reading in db.scalars(query).all():
            latest = latest_reading(db, reading.customer_id)
            recent.append(RecentReading(reading, db.get(Customer, reading.customer_id),
                                        latest.reading_id == reading.reading_id))
        return recent
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "list the recent readings") from exc


def fix_details(db: Session, reading_id: int) -> ReadingFix:
    try:
        reading = get_reading(db, reading_id)
        bill = bills.bill_for_reading(db, reading_id)
        latest = latest_reading(db, reading.customer_id)
        was_sent = bill is not None and bills.sms_was_sent(db, bill.bill_id)
        return ReadingFix(reading, db.get(Customer, reading.customer_id), bill, correction_case(db, reading),
                          latest.reading_id == reading_id, was_sent, _is_first_reading(db, reading))
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the reading") from exc


def _customers_read_this_month(db: Session) -> set[int]:
    query = select(MeterReading.customer_id).where(MeterReading.billing_period == current_period())
    return set(db.scalars(query))


def _clean_search(search: str) -> str:
    # REQ-3: an unreasonable search is refused, not cut short without saying so
    term = clean_text(search)
    if len(term) > SEARCH_MAX_LENGTH:
        raise BusinessRuleError(f"Please search with {SEARCH_MAX_LENGTH} characters or fewer.", field="search")
    return term


# ---- entering a reading (RD-1 to RD-7) ----

def create_reading(db: Session, actor: User, customer_id: int, typed: Mapping[str, str]) -> ReadingResult:
    """Save a reading and its bill together, then send the bill SMS (RD-6)."""
    try:
        customer = _get_customer(db, customer_id)
        try:
            checked = _check_new_reading(db, customer, typed)
            reading, bill = _save_reading_and_bill(db, actor, customer, checked)
            # DB-7: the reading and its bill, together or not at all
            db.commit()
        except IntegrityError as exc:
            # RD-4: the database refused a second reading for the month, sent at the same moment
            raise _refused(db, actor, customer, typed, _duplicate_error(checked.billing_period)) from exc
        except AppError as exc:
            raise _refused(db, actor, customer, typed, exc)
        return ReadingResult(reading, bill, customer, _send_sms(db, actor, bill, "bill"))
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "save the reading") from exc


def units_preview(db: Session, customer_id: int, typed_current: str, typed_previous: str = "") -> Decimal:
    """The units a new reading would bill, for the live line under the field. It runs the
    same checks as a save, so the rule lives in one place, and records nothing.
    `typed_previous` counts only on a customer's first reading (CUS-5)."""
    try:
        customer = _get_customer(db, customer_id)
        latest = latest_reading(db, customer.customer_id)
        if latest is None:
            previous, _ = _typed_start({"previous_reading": typed_previous}, NOTHING, NOTHING)
            current = _parse_reading(typed_current)
            _check_not_lower(current, previous, typed_previous=True)
            return current - previous
        return _check_current_reading(typed_current, latest.current_reading) - latest.current_reading
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "work out the units") from exc


def period_preview(typed_date: str) -> str:
    """RD-5: the month a reading dated `typed_date` is billed to (YYYY-MM), for the line under
    the date. It runs the same date checks as a save, so a bad date is refused here too."""
    try:
        return _parse_reading_date(typed_date).strftime("%Y-%m")
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "work out the billing month") from exc


def period_if_valid(typed_date: str) -> str | None:
    """The same, for showing the form again: None when the date is not acceptable, because
    the save already said why, beside the date."""
    try:
        return period_preview(typed_date)
    except BusinessRuleError:
        return None


def latest_reading(db: Session, customer_id: int) -> MeterReading | None:
    """The customer's reading for their latest month, or None before the first one."""
    try:
        query = (select(MeterReading).where(MeterReading.customer_id == customer_id)
                 .order_by(MeterReading.billing_period.desc()))
        return db.scalars(query.limit(1)).first()
    except Exception as exc:
        raise unexpected_error(exc, "load the latest reading") from exc


def get_reading(db: Session, reading_id: int) -> MeterReading:
    try:
        reading = db.get(MeterReading, reading_id)
        if reading is None:
            raise NotFoundError("We could not find that reading.")
        return reading
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the reading") from exc


# ---- correcting a reading (PRD 4.4, COR-1 to COR-4) ----

def correction_case(db: Session, reading: MeterReading) -> str:
    """Which of the three cases a correction of this reading falls in."""
    try:
        bill = bills.bill_for_reading(db, reading.reading_id)
        if bill is None:
            return NO_BILL
        if bills.sms_was_sent(db, bill.bill_id) or bill.amount_paid > 0:
            return BILL_SENT_OR_PAID
        return BILL_NOT_SENT
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "check the reading") from exc


def correct_reading(db: Session, actor: User, reading_id: int, typed: Mapping[str, str]) -> ReadingResult:
    """The user typed the right number.
    - No bill, or a bill not sent and not paid: the old reading and bill are deleted and the
      reading is entered again, with a new bill and bill SMS.
    - A bill that was sent or has a payment: the reading is edited in place, the bill is
      recalculated with its payments kept, and the customer is told (SMS-12)."""
    try:
        reading = get_reading(db, reading_id)
        customer = db.get(Customer, reading.customer_id)
        try:
            values = _check_correction(db, customer, reading, typed)
            case = correction_case(db, reading)
            if case == BILL_SENT_OR_PAID:
                corrected, bill, message_type = _correct_in_place(db, actor, customer, reading, values)
            else:
                corrected, bill = _enter_again(db, actor, customer, reading, values, case)
                message_type = "bill"
            db.commit()
        except AppError as exc:
            raise _refused(db, actor, customer, typed, exc, reading)
        return ReadingResult(corrected, bill, customer, _send_sms(db, actor, bill, message_type), case)
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "correct the reading") from exc


def delete_reading(db: Session, actor: User, reading_id: int) -> DeletedReading:
    """A reading entered by mistake, with no bill or a bill not sent and not paid. COR-1: a
    reading whose SMS was sent or whose bill has a payment is never deleted."""
    try:
        reading = get_reading(db, reading_id)
        customer = db.get(Customer, reading.customer_id)
        deleted = DeletedReading(customer, reading.billing_period)
        try:
            _check_can_be_corrected(customer)
            _check_is_latest(db, reading)
            case = correction_case(db, reading)
            if case == BILL_SENT_OR_PAID:
                raise BusinessRuleError(
                    "This reading's bill was sent or has a payment, so the reading is kept for the record. "
                    "Type the right number to correct it instead."
                )
            _delete_reading_and_bill(db, actor, customer, reading, case)
            db.commit()
        except AppError as exc:
            raise _refused(db, actor, customer, {}, exc, reading)
        return deleted
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "delete the reading") from exc


def _correct_in_place(db: Session, actor: User, customer: Customer, reading: MeterReading,
                      values: CorrectedValues) -> tuple[MeterReading, Bill, str]:
    """Returns the reading, its bill, and which SMS to send once it is saved."""
    bill = bills.bill_for_reading(db, reading.reading_id)
    was_sent = bills.sms_was_sent(db, bill.bill_id)
    old_previous, old_current, old_units = reading.previous_reading, reading.current_reading, reading.units_consumed
    reading.previous_reading = values.previous
    reading.current_reading = values.current
    reading.units_consumed = values.current - values.previous
    details = {"old_current": old_current, "new_current": values.current, "old_units": old_units,
               "new_units": reading.units_consumed, "case": BILL_SENT_OR_PAID}
    if values.previous != old_previous:
        # CUS-5: only a first reading's previous reading can change
        details.update({"old_previous": old_previous, "new_previous": values.previous})
    audit.record_event(
        db, "reading.updated",
        f"Reading for {customer.meter_number} ({reading.billing_period}) corrected: "
        f"{plain_number(old_current)} to {plain_number(values.current)}",
        actor=actor, entity=reading, customer_id=customer.customer_id, details=details,
    )
    bills.recalculate_bill(db, actor, bill, reading, values.opening_balance)
    # A bill SMS that went out is followed by a correction SMS. One that never went out is
    # sent as the normal bill SMS, with the corrected figures.
    return reading, bill, "correction" if was_sent else "bill"


def _enter_again(db: Session, actor: User, customer: Customer, reading: MeterReading, values: CorrectedValues,
                 case: str) -> tuple[MeterReading, Bill]:
    """The old reading and bill go, and the reading is entered again for the same day."""
    checked = CheckedReading(reading.reading_date, reading.billing_period, values.previous, values.current,
                             values.opening_balance if values.opening_balance is not None else NOTHING)
    _delete_reading_and_bill(db, actor, customer, reading, case)
    return _save_reading_and_bill(db, actor, customer, checked)


def _delete_reading_and_bill(db: Session, actor: User, customer: Customer, reading: MeterReading,
                             case: str) -> None:
    bill = bills.bill_for_reading(db, reading.reading_id)
    if bill is not None:
        # COR-4: its messages_log rows are kept
        bills.delete_bill(db, actor, bill)
    reading_id = reading.reading_id
    old_values = {"period": reading.billing_period, "previous": reading.previous_reading,
                  "current": reading.current_reading, "units": reading.units_consumed, "case": case}
    db.delete(reading)
    # the old reading must be gone before one for the same month can be saved
    db.flush()
    audit.record_event(
        db, "reading.deleted", f"Reading {reading_id} for {customer.meter_number} ({old_values['period']}) deleted",
        actor=actor, entity=reading, customer_id=customer.customer_id, details=old_values,
    )


def _check_correction(db: Session, customer: Customer, reading: MeterReading,
                      typed: Mapping[str, str]) -> CorrectedValues:
    _check_can_be_corrected(customer)
    _check_is_latest(db, reading)
    current = _parse_reading(typed.get("current_reading", ""))
    if not _is_first_reading(db, reading):
        _check_not_lower(current, reading.previous_reading)
        if current == reading.current_reading:
            raise _nothing_changed()
        return CorrectedValues(reading.previous_reading, current, None)
    # CUS-5: on a first reading the previous reading and opening balance can be corrected too
    bill = bills.bill_for_reading(db, reading.reading_id)
    old_opening = bill.previous_balance if bill is not None else NOTHING
    previous, opening_balance = _typed_start(typed, reading.previous_reading, old_opening)
    _check_not_lower(current, previous, typed_previous=True)
    if (previous, current, opening_balance) == (reading.previous_reading, reading.current_reading, old_opening):
        raise _nothing_changed()
    return CorrectedValues(previous, current, opening_balance)


def _nothing_changed() -> BusinessRuleError:
    return BusinessRuleError("That is the reading already saved. Type the corrected number.",
                             field="current_reading")


def _check_can_be_corrected(customer: Customer) -> None:
    # COR-5: an inactive customer's reading is neither edited nor deleted (CUS-4). Reactivate,
    # fix, and deactivate again if needed, so each step is in the audit log.
    if customer.status != "active":
        raise BusinessRuleError("Reactivate the customer first.")


def _check_is_latest(db: Session, reading: MeterReading) -> None:
    # COR-3: fixing an older reading would leave the later readings and bills wrong
    latest = latest_reading(db, reading.customer_id)
    if latest is None or latest.reading_id != reading.reading_id:
        raise BusinessRuleError("Only the latest reading can be corrected.")


# ---- the checks for a new reading ----

def _get_customer(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError("We could not find that customer.")
    return customer


def _check_is_active(customer: Customer) -> None:
    # CUS-4: an inactive customer gets no new bills or SMS
    if customer.status != "active":
        raise BusinessRuleError("This customer is inactive, so no bill can be made. Reactivate the customer first.")


def _check_new_reading(db: Session, customer: Customer, typed: Mapping[str, str]) -> CheckedReading:
    _check_is_active(customer)
    reading_date = _parse_reading_date(typed.get("reading_date", ""))
    current = _parse_reading(typed.get("current_reading", ""))
    # RD-5: the billing period comes from the reading date
    billing_period = reading_date.strftime("%Y-%m")
    latest = latest_reading(db, customer.customer_id)
    if latest is not None and latest.billing_period == billing_period:
        raise _duplicate_error(billing_period)
    if latest is not None and latest.billing_period > billing_period:
        raise BusinessRuleError(
            f"This customer's last reading is for {period_label(latest.billing_period)}. "
            "A new reading must be for a later month.", field="reading_date"
        )
    if latest is None:
        # CUS-5: the first reading in the system starts from the paper records
        previous, opening_balance = _typed_start(typed, NOTHING, NOTHING)
        _check_not_lower(current, previous, typed_previous=True)
        return CheckedReading(reading_date, billing_period, previous, current, opening_balance)
    # RD-2: last period's current reading. Anything typed for these is ignored (IN-7).
    _check_not_lower(current, latest.current_reading)
    return CheckedReading(reading_date, billing_period, latest.current_reading, current)


def _check_current_reading(typed_current: str, previous: Decimal) -> Decimal:
    current = _parse_reading(typed_current)
    _check_not_lower(current, previous)
    return current


def _check_not_lower(current: Decimal, previous: Decimal, typed_previous: bool = False) -> None:
    # RD-3: a meter never goes down (meter rollover is out of scope)
    if current >= previous:
        return
    if typed_previous:
        raise BusinessRuleError(
            f"This is lower than the previous reading ({plain_number(previous)}). Please check both numbers.",
            field="current_reading",
        )
    raise BusinessRuleError(
        f"This is lower than last month's reading ({plain_number(previous)}). Please check the number.",
        field="current_reading",
    )


def _typed_start(typed: Mapping[str, str], previous_default: Decimal,
                 opening_default: Decimal) -> tuple[Decimal, Decimal]:
    """CUS-5: the previous reading and opening balance typed on a first reading. An empty box
    keeps its default (0 for a new reading, the saved value for a correction)."""
    previous_text = clean_text(typed.get("previous_reading", ""))
    opening_text = clean_text(typed.get("opening_balance", ""))
    previous = previous_default
    if previous_text:
        previous = parse_money(previous_text, "previous_reading", "previous reading", "200").quantize(CENTS)
    opening_balance = opening_default
    if opening_text:
        opening_balance = parse_money(opening_text, "opening_balance", "opening balance", "500").quantize(CENTS)
    return previous, opening_balance


def _is_first_reading(db: Session, reading: MeterReading) -> bool:
    # no reading of this customer for an earlier month
    query = select(MeterReading.reading_id).where(MeterReading.customer_id == reading.customer_id,
                                                  MeterReading.billing_period < reading.billing_period)
    return db.scalars(query.limit(1)).first() is None


def _parse_reading(value: str) -> Decimal:
    # A reading has the same shape as an amount: DECIMAL(10, 2), zero or more. It is kept to
    # the cent, as the database stores it.
    return parse_money(value, "current_reading", "reading", "224").quantize(CENTS)


def _parse_reading_date(value: str) -> date:
    typed = clean_text(value)
    if not typed:
        # RD-5: the reading date defaults to today
        return today()
    try:
        reading_date = date.fromisoformat(typed)
    except ValueError as exc:
        raise BusinessRuleError("Please enter the reading date, for example 2026-08-30.",
                                field="reading_date") from exc
    if reading_date > today():
        raise BusinessRuleError("The reading date cannot be in the future. Use today or an earlier day.",
                                field="reading_date")
    return reading_date


def _duplicate_error(billing_period: str) -> ConflictError:
    # RD-4: one reading per customer per month
    return ConflictError(
        f"This customer already has a reading for {period_label(billing_period)}. "
        "To change it, use Fix on it under Recent readings.", field="current_reading"
    )


# ---- saving ----

def _save_reading_and_bill(db: Session, actor: User, customer: Customer,
                           checked: CheckedReading) -> tuple[MeterReading, Bill]:
    reading = MeterReading(
        customer_id=customer.customer_id, billing_period=checked.billing_period,
        previous_reading=checked.previous, current_reading=checked.current,
        # BILL-1
        units_consumed=checked.current - checked.previous, reading_date=checked.reading_date,
        # RD-7: who entered it
        recorded_by=actor.user_id,
    )
    db.add(reading)
    db.flush()
    audit.record_event(
        db, "reading.created", f"Reading {plain_number(checked.current)} entered for {customer.meter_number}",
        actor=actor, entity=reading, customer_id=customer.customer_id,
        details={"period": checked.billing_period, "previous": checked.previous, "current": checked.current,
                 "units": reading.units_consumed, "recorded_by": actor.user_id},
    )
    return reading, bills.generate_bill(db, actor, reading, checked.opening_balance)


def _send_sms(db: Session, actor: User, bill: Bill, message_type: str) -> str:
    """Sends after the data is saved. Returns sent, failed, ready, blocked or not_queued."""
    try:
        if message_type == "correction":
            return bills.send_correction_sms(db, actor, bill).status
        return bills.send_bill_sms(db, actor, bill).status
    except TooManyAttemptsError:
        # SMS-S3: the limit was reached and recorded as sms.send_blocked. The reading and the
        # bill are saved, and the SMS can be sent again from the bill tomorrow.
        return "blocked"
    except MessageNotQueuedError:
        # SMS-14: the reading and bill are saved, so this is not a 500 page. The error is
        # already logged with its stack trace and recorded as system.error.
        return "not_queued"


def _refused(db: Session, actor: User, customer: Customer, typed: Mapping[str, str], error: AppError,
             reading: MeterReading | None = None) -> AppError:
    """Undoes the unfinished save, then, for a refusal (not a 500), records reading.rejected
    on its own, so it is kept (AUDIT_LOG.md rule 3). Returns the error to raise. The reason
    is the message the user saw, which never holds the customer's name."""
    db.rollback()
    if error.status_code >= 500:
        return error
    audit.record_problem(
        "reading.rejected", f"Reading refused for {customer.meter_number}: {error.message}",
        actor=actor, entity=reading, customer_id=customer.customer_id,
        details={"reason": error.message,
                 "current_reading": clean_text(typed.get("current_reading", ""))[:TRIED_MAX_LENGTH],
                 "reading_date": clean_text(typed.get("reading_date", ""))[:TRIED_MAX_LENGTH]},
    )
    return error
