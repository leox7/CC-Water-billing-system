"""Reports and the dashboard figures (PRD section 4.8). Nothing here changes data, so nothing
is written to the audit log (AUDIT_LOG.md section 4, "Not recorded").

What a customer owes is always bills.amount_unpaid() on their latest bill (BILL-4): that
bill's total already holds everything carried into it, so older bills are never added again.
The dashboard uses these same functions, so its figures always agree with the reports (DASH-1).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, BusinessRuleError, unexpected_error
from app.core.logging import EAT
from app.features.bills import service as bills
from app.models import Bill, Customer, MeterReading, Payment

NOTHING = Decimal("0.00")
# the customer list's status filter: the statuses a bill can have
BILL_STATUSES = bills.BILL_STATUSES
# REQ-3: a report never shows more rows than this. Its total always counts every row.
LIST_LIMIT = 200
# how many people owing money the dashboard lists before pointing to the arrears report
NEEDS_ATTENTION_LIMIT = 10
# a customer id typed into the address: digits only, and not absurdly long (REQ-3)
CUSTOMER_ID_FORMAT = re.compile(r"^\d{1,10}$")
# the month a payment is dated in, YYYY-MM (MySQL's DATE_FORMAT)
PAYMENT_MONTH = func.date_format(Payment.payment_date, "%Y-%m")


@dataclass
class BillInMonth:
    bill: Bill
    # the bill's month, YYYY-MM
    period: str


@dataclass
class ArrearsRow:
    customer: Customer
    # the customer's latest bill, where the money is owed and where a payment goes (PAY-3)
    bill: Bill
    amount_owed: Decimal
    # REP-1: the month the debt began (YYYY-MM)
    since: str


@dataclass
class ArrearsReport:
    rows: list[ArrearsRow]
    total_owed: Decimal
    # every customer who owes, even past LIST_LIMIT
    customer_count: int
    partial_count: int
    unpaid_count: int


@dataclass
class PaymentRow:
    payment: Payment
    customer: Customer
    # the month of the bill the payment went on
    bill_period: str


@dataclass
class PaymentHistory:
    rows: list[PaymentRow]
    total: Decimal
    # every payment that matches the filters, even past LIST_LIMIT
    payment_count: int


@dataclass
class CustomerReportRow:
    customer: Customer
    # None before the customer's first bill
    latest: BillInMonth | None
    amount_owed: Decimal


@dataclass
class CustomerReport:
    rows: list[CustomerReportRow]
    # every customer that matches the filter, even past LIST_LIMIT
    customer_count: int


@dataclass
class MonthlySummary:
    period: str
    # REP-4: new charges only (water plus fixed charge), without carried balances
    billed: Decimal
    bill_count: int
    # payments dated in the month, whichever bill they went on
    collected: Decimal
    payment_count: int
    # still unpaid on that month's bills
    outstanding: Decimal


@dataclass
class Dashboard:
    period: str
    active_customers: int
    # how many places the active customers live in
    location_count: int
    summary: MonthlySummary
    arrears: ArrearsReport
    # the first people in the arrears report
    needs_attention: list[ArrearsRow]


def today() -> date:
    """Today in Kenya (EAT, no daylight saving)."""
    return datetime.now(EAT).date()


def current_period() -> str:
    """This month, YYYY-MM. A reading is billed to the month of its date (RD-5)."""
    return today().strftime("%Y-%m")


# ---- REP-1 arrears ----

def arrears(db: Session) -> ArrearsReport:
    """Every customer whose latest bill still has something owed, the largest debt first."""
    try:
        customers = {customer.customer_id: customer for customer in db.scalars(select(Customer))}
        rows = []
        for customer_id, history in _bills_by_customer(db).items():
            latest = history[-1]
            amount_owed = bills.amount_unpaid(latest.bill)
            if amount_owed > 0:
                rows.append(ArrearsRow(customers[customer_id], latest.bill, amount_owed, _debt_began(history)))
        rows.sort(key=lambda row: (-row.amount_owed, row.customer.first_name, row.customer.last_name))
        return ArrearsReport(
            rows=rows[:LIST_LIMIT],
            total_owed=sum((row.amount_owed for row in rows), NOTHING),
            customer_count=len(rows),
            partial_count=len([row for row in rows if row.bill.status == "partial"]),
            unpaid_count=len([row for row in rows if row.bill.status == "unpaid"]),
        )
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "make the arrears report") from exc


def _bills_by_customer(db: Session) -> dict[int, list[BillInMonth]]:
    """Every bill with its month, oldest first, for each customer who has a bill. The last
    one is the customer's latest bill, in the same order as bills.latest_bill()."""
    query = (select(Bill, MeterReading.billing_period)
             .join(MeterReading, MeterReading.reading_id == Bill.reading_id)
             .order_by(Bill.customer_id, MeterReading.billing_period))
    history: dict[int, list[BillInMonth]] = {}
    for bill, period in db.execute(query):
        history.setdefault(bill.customer_id, []).append(BillInMonth(bill, period))
    return history


def _debt_began(history: list[BillInMonth]) -> str:
    # REP-1: follow the previous balance back. A bill that carried something in owes money
    # from the bill before it, so the debt began at the first bill of that unbroken run. A
    # first bill with an opening balance (CUS-5) is as far back as the app can see.
    index = len(history) - 1
    while index > 0 and history[index].bill.previous_balance > 0:
        index -= 1
    return history[index].period


# ---- REP-2 payment history ----

def payment_history(db: Session, customer: str = "", month: str = "") -> PaymentHistory:
    """Every payment, newest first. `customer` (a customer id) and `month` (YYYY-MM, the
    payment's date) narrow it; an empty one means all."""
    try:
        customer_id = _check_customer_filter(db, customer)
        _check_month(month)
        conditions = []
        if customer_id is not None:
            conditions.append(Bill.customer_id == customer_id)
        if month:
            conditions.append(PAYMENT_MONTH == month)
        query = (select(Payment, Customer, MeterReading.billing_period)
                 .join(Bill, Bill.bill_id == Payment.bill_id)
                 .join(Customer, Customer.customer_id == Bill.customer_id)
                 .join(MeterReading, MeterReading.reading_id == Bill.reading_id)
                 .where(*conditions)
                 .order_by(Payment.payment_date.desc(), Payment.payment_id.desc()))
        rows = [PaymentRow(payment, customer_row, period)
                for payment, customer_row, period in db.execute(query.limit(LIST_LIMIT))]
        totals_query = (select(func.count(), func.coalesce(func.sum(Payment.amount), 0))
                        .select_from(Payment).join(Bill, Bill.bill_id == Payment.bill_id).where(*conditions))
        payment_count, total = db.execute(totals_query).one()
        return PaymentHistory(rows, Decimal(total), payment_count)
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "make the payment history") from exc


def payment_months(db: Session) -> list[str]:
    """The months that have payments, newest first, for the month filter."""
    try:
        return list(db.scalars(select(PAYMENT_MONTH).distinct().order_by(PAYMENT_MONTH.desc())))
    except Exception as exc:
        raise unexpected_error(exc, "list the payment months") from exc


def customer_choices(db: Session) -> list[Customer]:
    """Every customer by name, for the customer filter."""
    try:
        return list(db.scalars(select(Customer).order_by(Customer.first_name, Customer.last_name)))
    except Exception as exc:
        raise unexpected_error(exc, "list the customers") from exc


def _check_customer_filter(db: Session, customer: str) -> int | None:
    # REQ-3: a customer that is not on the list is refused, not ignored
    if not customer:
        return None
    if not CUSTOMER_ID_FORMAT.match(customer) or db.get(Customer, int(customer)) is None:
        raise BusinessRuleError("Please pick a customer from the list.", field="customer")
    return int(customer)


def _check_month(month: str) -> None:
    # REQ-3: a month that is not YYYY-MM is refused, not ignored
    if month and not bills.PERIOD_FORMAT.match(month):
        raise BusinessRuleError("Please pick a month from the list.", field="month")


# ---- REP-3 customer list ----

def customer_report(db: Session, status: str = "") -> CustomerReport:
    """Every customer by name, with their latest bill. `status` (unpaid, partial or paid)
    keeps only the customers whose latest bill has it."""
    try:
        if status and status not in BILL_STATUSES:
            raise BusinessRuleError("Please pick a status from the list: unpaid, partial or paid.", field="status")
        history = _bills_by_customer(db)
        rows = []
        for customer in db.scalars(select(Customer).order_by(Customer.first_name, Customer.last_name)):
            latest = history[customer.customer_id][-1] if customer.customer_id in history else None
            if status and (latest is None or latest.bill.status != status):
                continue
            amount_owed = bills.amount_unpaid(latest.bill) if latest else NOTHING
            rows.append(CustomerReportRow(customer, latest, amount_owed))
        return CustomerReport(rows[:LIST_LIMIT], len(rows))
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "make the customer list") from exc


# ---- REP-4 monthly summary ----

def monthly_summary(db: Session, period: str) -> MonthlySummary:
    """REP-4: one month's billed, collected and outstanding amounts."""
    try:
        if not bills.PERIOD_FORMAT.match(period):
            raise BusinessRuleError("Please pick a month from the list.", field="month")
        query = (select(Bill).join(MeterReading, MeterReading.reading_id == Bill.reading_id)
                 .where(MeterReading.billing_period == period))
        month_bills = list(db.scalars(query))
        payments_query = (select(func.count(), func.coalesce(func.sum(Payment.amount), 0))
                          .where(PAYMENT_MONTH == period))
        payment_count, collected = db.execute(payments_query).one()
        return MonthlySummary(
            period=period,
            billed=sum((bill.consumption_charge + bill.service_charge for bill in month_bills), NOTHING),
            bill_count=len(month_bills),
            collected=Decimal(collected),
            payment_count=payment_count,
            outstanding=sum((bills.amount_unpaid(bill) for bill in month_bills), NOTHING),
        )
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "make the monthly summary") from exc


def summary_months(db: Session) -> list[str]:
    """Months with bills or payments, newest first, for the month picker."""
    try:
        return sorted(set(bills.bill_periods(db)) | set(payment_months(db)), reverse=True)
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "list the months") from exc


# ---- DASH-1 dashboard ----

def dashboard(db: Session) -> Dashboard:
    """The four stat cards and the "Needs attention" list, from the same functions as the
    reports. Outstanding balance and the counts use each customer's latest bill only."""
    try:
        period = current_period()
        active = select(Customer).where(Customer.status == "active").subquery()
        active_customers, location_count = db.execute(
            select(func.count(), func.count(func.distinct(active.c.location))).select_from(active)
        ).one()
        owing = arrears(db)
        return Dashboard(period, active_customers, location_count, monthly_summary(db, period), owing,
                         owing.rows[:NEEDS_ATTENTION_LIMIT])
    except AppError:
        raise
    except Exception as exc:
        raise unexpected_error(exc, "load the dashboard") from exc
