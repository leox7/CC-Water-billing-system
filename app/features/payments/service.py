"""Payments (PRD section 4.7): recording what a customer paid on a bill.

Money arrives by M-Pesa and the owner types it in (PAY-1, PAY-2). A payment goes on the
customer's latest bill only (PAY-3), and the bill's amount paid and status are worked out here
from its payments (PAY-5). Any leftover is not copied anywhere: the next bill finds it through
bills.outstanding_balance() (PAY-6).

Payments are never edited or deleted (PAY-9), so this file has no function that does either.
record_payment() saves the payment, the bill's new totals and the audit events in one
transaction, and commits.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, BusinessRuleError, ConflictError, NotFoundError, unexpected_error
from app.core.logging import EAT
from app.core.templating import date_label, ksh
from app.core.validation import clean_text, parse_money
from app.features.audit import service as audit
from app.features.bills import service as bills
from app.models import Bill, Customer, Payment, User

CENTS = Decimal("0.01")
NOTHING = Decimal("0.00")
# the payments.mpesa_reference column holds 20 characters
MPESA_CODE_MAX_LENGTH = 20
MPESA_CODE_FORMAT = re.compile(r"^[A-Z0-9]+$")
# a date this old can only be a typing mistake, and MySQL cannot store dates before year 1000
EARLIEST_PAYMENT_DATE = date(2000, 1, 1)
# how much of what was typed goes into a payment.rejected row
TRIED_MAX_LENGTH = 30


@dataclass
class CheckedPayment:
    """A payment that passed every check, ready to save."""

    amount: Decimal
    payment_date: date
    mpesa_reference: str | None


@dataclass
class PaymentResult:
    """What a save did, so the screen can say so."""

    payment: Payment
    bill: Bill
    customer: Customer
    old_status: str


@dataclass
class OwedAfter:
    """The live line under the amount: what a payment of that size would leave."""

    # what would still be owed, never below 0
    left: Decimal
    # how much of the payment is more than the bill needs (it is recorded, not carried forward)
    extra: Decimal


def today() -> date:
    """Today in Kenya (EAT, no daylight saving)."""
    return datetime.now(EAT).date()


def payment_details(db: Session, bill_id: int) -> bills.BillDetails:
    """The bill with its payments, for the Payments card. The bills service holds the query,
    so the card and the bill page always agree."""
    try:
        return bills.bill_details(db, bill_id)
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the payments") from exc


# ---- the live line under the amount ----

def owed_after(db: Session, bill_id: int, typed_amount: str) -> OwedAfter:
    """PAY-2: what would still be owed after a payment of `typed_amount`. It runs the same
    checks as a save, so the rule lives in one place, and records nothing."""
    try:
        bill = bills.get_bill(db, bill_id)
        _check_is_latest_bill(db, bill)
        amount = _parse_amount(typed_amount)
        unpaid = bills.amount_unpaid(bill)
        return OwedAfter(left=max(unpaid - amount, NOTHING), extra=max(amount - unpaid, NOTHING))
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "work out what is still owed") from exc


# ---- recording a payment (PAY-1 to PAY-8) ----

def record_payment(db: Session, actor: User, bill_id: int, typed: Mapping[str, str]) -> PaymentResult:
    """Save a payment on the customer's latest bill and update the bill's totals and status."""
    try:
        bill = _load_bill_to_pay(db, bill_id)
        customer = db.get(Customer, bill.customer_id)
        try:
            checked = _check_payment(db, bill, typed)
            result = _save_payment(db, actor, bill, customer, checked)
            db.commit()
        except IntegrityError as exc:
            # PAY-8: the database refused a code that was saved at the same moment
            db.rollback()
            existing = _find_payment_with_code(db, checked.mpesa_reference) if checked.mpesa_reference else None
            if existing is None:
                raise
            raise _refused(db, actor, bill, customer, typed, _duplicate_error(db, existing)) from exc
        except AppError as exc:
            raise _refused(db, actor, bill, customer, typed, exc)
        return result
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "record the payment") from exc


def _load_bill_to_pay(db: Session, bill_id: int) -> Bill:
    # The row is locked until the commit, so two payments typed at the same moment on one
    # bill are saved one after the other, and each adds up the payments that came before it.
    query = select(Bill).where(Bill.bill_id == bill_id).with_for_update().execution_options(populate_existing=True)
    bill = db.scalars(query).first()
    if bill is None:
        raise NotFoundError("We could not find that bill.")
    return bill


def _check_payment(db: Session, bill: Bill, typed: Mapping[str, str]) -> CheckedPayment:
    _check_is_latest_bill(db, bill)
    amount = _parse_amount(typed.get("amount", ""))
    payment_date = _parse_payment_date(typed.get("payment_date", ""))
    mpesa_reference = _parse_mpesa_reference(typed.get("mpesa_reference", ""))
    if mpesa_reference:
        existing = _find_payment_with_code(db, mpesa_reference)
        if existing is not None:
            raise _duplicate_error(db, existing)
    return CheckedPayment(amount, payment_date, mpesa_reference)


def _check_is_latest_bill(db: Session, bill: Bill) -> None:
    # PAY-3: a newer bill already holds this bill's unpaid balance, so a payment recorded here
    # would be counted twice
    latest = bills.latest_bill(db, bill.customer_id)
    if latest.bill_id != bill.bill_id:
        raise BusinessRuleError(
            "This bill was carried forward into a newer bill, so it takes no more payments. "
            "Record the payment on the customer's latest bill."
        )


def _parse_amount(value: str) -> Decimal:
    amount = parse_money(value, "amount", "amount received", "1,600").quantize(CENTS)
    # PAY-4
    if amount <= 0:
        raise BusinessRuleError("The amount received must be more than 0, for example 1,600.", field="amount")
    return amount


def _parse_payment_date(value: str) -> date:
    typed = clean_text(value)
    if not typed:
        # PAY-2: the date defaults to today
        return today()
    try:
        payment_date = date.fromisoformat(typed)
    except ValueError as exc:
        raise BusinessRuleError("Please enter the payment date, for example 2026-08-30.",
                                field="payment_date") from exc
    if payment_date > today():
        raise BusinessRuleError("The payment date cannot be in the future. Use today or an earlier day.",
                                field="payment_date")
    if payment_date < EARLIEST_PAYMENT_DATE:
        raise BusinessRuleError("Please check the year of the payment date.", field="payment_date")
    return payment_date


def _parse_mpesa_reference(value: str) -> str | None:
    """The optional M-Pesa code, in capitals so "qgh7x2k9lm" and "QGH7X2K9LM" are the same code."""
    code = clean_text(value).upper()
    if not code:
        return None
    if len(code) > MPESA_CODE_MAX_LENGTH:
        raise BusinessRuleError(f"The M-Pesa code is too long. Use {MPESA_CODE_MAX_LENGTH} characters or fewer.",
                                field="mpesa_reference")
    if not MPESA_CODE_FORMAT.match(code):
        raise BusinessRuleError("The M-Pesa code can only have letters and numbers, for example QGH7X2K9LM.",
                                field="mpesa_reference")
    return code


def _find_payment_with_code(db: Session, mpesa_reference: str) -> Payment | None:
    return db.scalars(select(Payment).where(Payment.mpesa_reference == mpesa_reference)).first()


def _duplicate_error(db: Session, existing: Payment) -> ConflictError:
    # PAY-8: names the payment it matches, by its amount, date and meter number (the audit
    # log keeps the customer's name out, so the message does too)
    meter_number = db.get(Customer, db.get(Bill, existing.bill_id).customer_id).meter_number
    return ConflictError(
        f"The M-Pesa code {existing.mpesa_reference} is already recorded: a payment of {ksh(existing.amount)} "
        f"on {date_label(existing.payment_date)} for meter {meter_number}. If it is the same payment, "
        "it is already saved.", field="mpesa_reference"
    )


# ---- saving ----

def _save_payment(db: Session, actor: User, bill: Bill, customer: Customer, checked: CheckedPayment) -> PaymentResult:
    payment = Payment(bill_id=bill.bill_id, amount=checked.amount, payment_date=checked.payment_date,
                      method="mpesa", mpesa_reference=checked.mpesa_reference, recorded_by=actor.user_id)
    db.add(payment)
    db.flush()
    old_status = bill.status
    # PAY-5: the amount paid is always the sum of the bill's payments, and the status follows it
    bill.amount_paid = _total_paid(db, bill.bill_id)
    bill.status = bills.bill_status(bill.total_due, bill.amount_paid)
    audit.record_event(
        db, "payment.recorded",
        f"Payment of {ksh(payment.amount)} recorded on bill {bill.bill_id} for {customer.meter_number}",
        actor=actor, entity=payment, customer_id=customer.customer_id,
        details={"amount": payment.amount, "payment_date": payment.payment_date,
                 "mpesa_code": payment.mpesa_reference, "amount_paid": bill.amount_paid,
                 "total_due": bill.total_due, "new_status": bill.status},
    )
    if bill.status != old_status:
        audit.record_event(
            db, "bill.status_changed", f"Bill {bill.bill_id} is now {bill.status} (was {old_status})",
            actor=actor, entity=bill, customer_id=customer.customer_id,
            details={"old_status": old_status, "new_status": bill.status, "amount_paid": bill.amount_paid,
                     "total_due": bill.total_due},
        )
    return PaymentResult(payment, bill, customer, old_status)


def _total_paid(db: Session, bill_id: int) -> Decimal:
    total = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.bill_id == bill_id))
    return Decimal(total).quantize(CENTS)


def _refused(db: Session, actor: User, bill: Bill, customer: Customer, typed: Mapping[str, str],
             error: AppError) -> AppError:
    """Undoes the unfinished save, then, for a refusal (not a 500), records payment.rejected on
    its own, so it is kept (AUDIT_LOG.md rule 3). Returns the error to raise. The reason is the
    message the user saw, which never holds the customer's name."""
    bill_id, customer_id, meter_number = bill.bill_id, customer.customer_id, customer.meter_number
    db.rollback()
    if error.status_code >= 500:
        return error
    audit.record_problem(
        "payment.rejected", f"Payment refused on bill {bill_id} for {meter_number}: {error.message}",
        actor=actor, entity=bill, customer_id=customer_id,
        details={"reason": error.message, "bill_id": bill_id,
                 "amount": clean_text(typed.get("amount", ""))[:TRIED_MAX_LENGTH],
                 "mpesa_code": clean_text(typed.get("mpesa_reference", ""))[:TRIED_MAX_LENGTH]},
    )
    return error
