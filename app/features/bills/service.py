"""Bills (PRD section 4.5): making the bill for a reading, the balance carried from the bill
before, recalculating after a correction, and sending a bill's SMS.

Two rules live here and nowhere else (architecture.md section 4):
- outstanding_balance(): what is carried into a new bill (BILL-4).
- bill_status(): paid, partial or unpaid, from the amount paid (PAY-5). Payments use it too;
  it lives here because bills cannot import payments, and a recalculated bill needs it.

A bill is made or changed inside the caller's transaction: the caller commits, so a reading
and its bill are saved together or not at all (DB-7). Its SMS is sent after that commit.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, BusinessRuleError, NotFoundError, unexpected_error
from app.core.templating import ksh
from app.features.audit import service as audit
from app.features.messaging import service as messaging
from app.features.messaging import templates as sms_templates
from app.models import Bill, Customer, MessageLog, MeterReading, Payment, User

CENTS = Decimal("0.01")
NOTHING = Decimal("0.00")
# total_due is DECIMAL(12, 2): the most one bill can hold
BILL_MAX = Decimal("9999999999.99")
# IN-6: the filters of the bills list are checked against these
BILL_STATUSES = ("unpaid", "partial", "paid")
PERIOD_FORMAT = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# REQ-3: a list never returns more rows than this
LIST_LIMIT = 200


@dataclass
class BillRow:
    """One bill in the list, with what the list shows about it."""

    bill: Bill
    customer: Customer
    reading: MeterReading


@dataclass
class BillDetails:
    bill: Bill
    customer: Customer
    reading: MeterReading
    # every SMS attempt for this bill, oldest first
    messages: list[MessageLog]
    # every payment recorded on this bill, oldest first (PAY-3)
    payments: list[Payment]
    # PAY-3: payments are recorded on the customer's latest bill only
    latest_bill: Bill
    # what is still owed on this bill, never below 0
    still_owed: Decimal


def list_bills(db: Session, period: str = "", status: str = "") -> list[BillRow]:
    """Bills newest month first, then by customer name. `period` (YYYY-MM) and `status`
    narrow the list; an empty one means all."""
    try:
        _check_filters(period, status)
        query = (
            select(Bill, Customer, MeterReading)
            .join(Customer, Customer.customer_id == Bill.customer_id)
            .join(MeterReading, MeterReading.reading_id == Bill.reading_id)
            .order_by(MeterReading.billing_period.desc(), Customer.first_name, Customer.last_name)
        )
        if period:
            query = query.where(MeterReading.billing_period == period)
        if status:
            query = query.where(Bill.status == status)
        return [BillRow(bill, customer, reading) for bill, customer, reading in db.execute(query.limit(LIST_LIMIT))]
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "list the bills") from exc


def bill_periods(db: Session) -> list[str]:
    """The months that have bills, newest first, for the month filter."""
    try:
        query = (select(MeterReading.billing_period).join(Bill, Bill.reading_id == MeterReading.reading_id)
                 .distinct().order_by(MeterReading.billing_period.desc()))
        return list(db.scalars(query))
    except Exception as exc:
        raise unexpected_error(exc, "list the bill months") from exc


def get_bill(db: Session, bill_id: int) -> Bill:
    try:
        bill = db.get(Bill, bill_id)
        if bill is None:
            raise NotFoundError("We could not find that bill.")
        return bill
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the bill") from exc


def bill_details(db: Session, bill_id: int) -> BillDetails:
    """A bill with its customer, its reading, its SMS attempts and its payments, for the bill page."""
    try:
        bill = get_bill(db, bill_id)
        messages = db.scalars(select(MessageLog).where(MessageLog.bill_id == bill_id).order_by(MessageLog.message_id))
        payments = db.scalars(select(Payment).where(Payment.bill_id == bill_id)
                              .order_by(Payment.payment_date, Payment.payment_id))
        return BillDetails(bill, db.get(Customer, bill.customer_id), db.get(MeterReading, bill.reading_id),
                           list(messages), list(payments), latest_bill(db, bill.customer_id), amount_unpaid(bill))
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the bill") from exc


def latest_bill(db: Session, customer_id: int) -> Bill | None:
    """PAY-3: the bill for the customer's latest month, the only one that takes payments.
    None when the customer has no bill yet."""
    try:
        query = (
            select(Bill).join(MeterReading, MeterReading.reading_id == Bill.reading_id)
            .where(Bill.customer_id == customer_id).order_by(MeterReading.billing_period.desc())
        )
        return db.scalars(query.limit(1)).first()
    except Exception as exc:
        raise unexpected_error(exc, "load the latest bill") from exc


def resend_sms(db: Session, actor: User, bill_id: int) -> MessageLog:
    """SMS-4: Send again on the bill page. Only the latest SMS, only if it failed, and never to
    an inactive customer; messaging/service.py checks and records each refusal."""
    try:
        return messaging.resend_message(db, actor, get_bill(db, bill_id))
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "send the SMS again") from exc


def amount_unpaid(bill: Bill) -> Decimal:
    """What is still owed on a bill. Never below 0: an overpayment is not credit (PRD section 12)."""
    return max(bill.total_due - bill.amount_paid, NOTHING)


def bill_status(total_due: Decimal, amount_paid: Decimal) -> str:
    """PAY-5: worked out from the amount paid, never set by hand."""
    if amount_paid >= total_due:
        return "paid"
    if amount_paid > 0:
        return "partial"
    return "unpaid"


def outstanding_balance(db: Session, customer_id: int, before_period: str) -> Decimal:
    """BILL-4: what is unpaid on the customer's latest bill before `before_period` (YYYY-MM).
    That bill's total already holds everything carried into it, so older bills are never
    added again. 0 when there is no earlier bill (the first bill starts from zero)."""
    try:
        latest_earlier_bill = _latest_earlier_bill(db, customer_id, before_period)
        if latest_earlier_bill is None:
            return NOTHING
        return amount_unpaid(latest_earlier_bill)
    except Exception as exc:
        raise unexpected_error(exc, "work out the balance") from exc


def generate_bill(db: Session, actor: User, reading: MeterReading, opening_balance: Decimal = NOTHING) -> Bill:
    """BILL-1 to BILL-7: the bill for a reading that was just added. The caller commits.
    `opening_balance` is what the customer owed on paper (CUS-5). It is used only for the
    customer's first bill: after that, BILL-4 carries the balance from the bill before."""
    try:
        customer = db.get(Customer, reading.customer_id)
        # BILL-5: the customer's rate and service charge today are copied onto the bill
        consumption_charge = _to_the_cent(reading.units_consumed * customer.rate_per_unit)
        if _latest_earlier_bill(db, customer.customer_id, reading.billing_period) is None:
            previous_balance = opening_balance
        else:
            previous_balance = outstanding_balance(db, customer.customer_id, reading.billing_period)
        # BILL-3
        total_due = _check_fits(consumption_charge + customer.service_charge + previous_balance)
        bill = Bill(
            reading_id=reading.reading_id, customer_id=customer.customer_id,
            rate_per_unit=customer.rate_per_unit, service_charge=customer.service_charge,
            units_consumed=reading.units_consumed, consumption_charge=consumption_charge,
            previous_balance=previous_balance, total_due=total_due,
            # BILL-7: a new bill starts unpaid
            amount_paid=NOTHING, status="unpaid",
        )
        db.add(bill)
        db.flush()
        audit.record_event(
            db, "bill.generated", f"Bill of {ksh(total_due)} for {customer.meter_number} ({reading.billing_period})",
            actor=actor, entity=bill, customer_id=customer.customer_id,
            details={"period": reading.billing_period, "units": bill.units_consumed, "rate": bill.rate_per_unit,
                     "service_charge": bill.service_charge, "previous_balance": previous_balance,
                     "total_due": total_due},
        )
        return bill
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "make the bill") from exc


def recalculate_bill(db: Session, actor: User, bill: Bill, reading: MeterReading,
                     opening_balance: Decimal | None = None) -> Bill:
    """After a reading is corrected in place (PRD 4.4, third case). The bill keeps its own
    rate, service charge and carried balance, and every payment already recorded. The
    caller commits. A customer's first bill may also get a corrected `opening_balance`
    (CUS-5); any later bill's carried balance comes from the bill before it and never changes."""
    try:
        old_total = bill.total_due
        if opening_balance is not None and _latest_earlier_bill(db, bill.customer_id, reading.billing_period) is None:
            bill.previous_balance = opening_balance
        bill.units_consumed = reading.units_consumed
        bill.consumption_charge = _to_the_cent(reading.units_consumed * bill.rate_per_unit)
        bill.total_due = _check_fits(bill.consumption_charge + bill.service_charge + bill.previous_balance)
        bill.status = bill_status(bill.total_due, bill.amount_paid)
        audit.record_event(
            db, "bill.recalculated", f"Bill {bill.bill_id} recalculated: {ksh(old_total)} to {ksh(bill.total_due)}",
            actor=actor, entity=bill, customer_id=bill.customer_id,
            details={"old_total_due": old_total, "new_total_due": bill.total_due,
                     "amount_paid": bill.amount_paid, "new_status": bill.status},
        )
        return bill
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "recalculate the bill") from exc


def delete_bill(db: Session, actor: User, bill: Bill) -> None:
    """PRD 4.4, second case: a bill whose SMS was not sent and that has no payment. Its
    messages_log rows are kept, with the bill link emptied by the database (COR-4). The
    caller commits."""
    try:
        bill_id, customer_id = bill.bill_id, bill.customer_id
        old_figures = {"units": bill.units_consumed, "rate": bill.rate_per_unit, "service_charge": bill.service_charge,
                       "previous_balance": bill.previous_balance, "total_due": bill.total_due}
        db.delete(bill)
        db.flush()
        audit.record_event(
            db, "bill.deleted", f"Bill {bill_id} of {ksh(old_figures['total_due'])} deleted",
            actor=actor, entity=bill, customer_id=customer_id, details=old_figures,
        )
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "delete the bill") from exc


def bill_for_reading(db: Session, reading_id: int) -> Bill | None:
    """BILL-6: the one bill made from a reading, or None when there is none."""
    try:
        return db.scalars(select(Bill).where(Bill.reading_id == reading_id)).first()
    except Exception as exc:
        raise unexpected_error(exc, "load the bill") from exc


def sms_was_sent(db: Session, bill_id: int) -> bool:
    """True once any SMS for this bill went out (a bill or a correction SMS)."""
    try:
        query = select(MessageLog.message_id).where(MessageLog.bill_id == bill_id, MessageLog.status == "sent")
        return db.scalars(query.limit(1)).first() is not None
    except Exception as exc:
        raise unexpected_error(exc, "check the SMS") from exc


def send_bill_sms(db: Session, actor: User, bill: Bill) -> MessageLog:
    """SMS-2: the bill SMS, after the bill is saved. Returns the attempt, sent or failed."""
    try:
        customer = db.get(Customer, bill.customer_id)
        reading = db.get(MeterReading, bill.reading_id)
        return messaging.send_message(db, actor, bill, "bill", sms_templates.bill_sms(customer, reading, bill))
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "send the bill SMS") from exc


def send_correction_sms(db: Session, actor: User, bill: Bill) -> MessageLog:
    """SMS-5, SMS-12: after a corrected reading on a bill whose SMS was already sent."""
    try:
        customer = db.get(Customer, bill.customer_id)
        reading = db.get(MeterReading, bill.reading_id)
        text = sms_templates.correction_sms(customer, reading, bill, amount_unpaid(bill))
        return messaging.send_message(db, actor, bill, "correction", text)
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "send the correction SMS") from exc


def _latest_earlier_bill(db: Session, customer_id: int, before_period: str) -> Bill | None:
    query = (
        select(Bill).join(MeterReading, MeterReading.reading_id == Bill.reading_id)
        .where(Bill.customer_id == customer_id, MeterReading.billing_period < before_period)
        .order_by(MeterReading.billing_period.desc())
    )
    return db.scalars(query.limit(1)).first()


def _to_the_cent(amount: Decimal) -> Decimal:
    # units and rate both have 2 decimal places, so the product can have 4. The bill stores
    # cents, so it is rounded once, here, in the usual way (0.005 goes up).
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def _check_fits(total_due: Decimal) -> Decimal:
    # IN-2: a total too large for the column is refused, never cut short
    if total_due > BILL_MAX:
        raise BusinessRuleError("This reading makes a bill too large to save. Please check the number.",
                                field="current_reading")
    return total_due


def _check_filters(period: str, status: str) -> None:
    # IN-6, REQ-3: a filter value that is not on the list is refused, not ignored
    if period and not PERIOD_FORMAT.match(period):
        raise BusinessRuleError("Please pick a month from the list.", field="month")
    if status and status not in BILL_STATUSES:
        raise BusinessRuleError("Please pick a status from the list: unpaid, partial or paid.", field="status")
