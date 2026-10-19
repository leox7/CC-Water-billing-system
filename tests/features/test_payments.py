"""payments/service.py (TASK-053, TASK-056, PAY-3 to PAY-8). The SMS provider is the stand-in
from conftest.py."""

import inspect
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.errors import BusinessRuleError, ConflictError, InternalError, NotFoundError
from app.features.bills import service as bills
from app.features.payments import service as payments
from app.features.readings import service as readings
from app.models import AuditLog, Bill, Payment

CODE = "QGH7X2K9LM"


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)
                              .order_by(AuditLog.audit_id)).all()


def payment_count(db_session):
    return db_session.scalar(select(func.count()).select_from(Payment))


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel")


@pytest.fixture
def mary(make_customer):
    return make_customer(first_name="Mary", last_name="Wanjiru")


@pytest.fixture
def grace(make_customer):
    return make_customer(first_name="Grace", last_name="Njeri")


def bill_in(db_session, owner, customer, current, reading_date):
    """The bill a reading makes, through the real flow. 10 units on the first reading is a bill
    of 1,450 (10 x 130 + 150), and each further 10 units adds 1,450 again."""
    result = readings.create_reading(db_session, owner, customer.customer_id,
                                     {"current_reading": current, "reading_date": reading_date})
    return result.bill


@pytest.fixture
def july_bill(db_session, owner, mary, sms):
    return bill_in(db_session, owner, mary, "10", "2025-07-30")


def pay(db_session, owner, bill, amount, code="", payment_date="2025-08-05"):
    typed = {"amount": amount, "payment_date": payment_date, "mpesa_reference": code}
    return payments.record_payment(db_session, owner, bill.bill_id, typed)


# ---- the status is worked out from the payments (PAY-5) ----

def test_paying_the_exact_amount_makes_the_bill_paid(db_session, owner, july_bill):
    result = pay(db_session, owner, july_bill, "1450")

    assert (result.old_status, result.bill.status) == ("unpaid", "paid")
    assert july_bill.amount_paid == Decimal("1450.00")
    assert bills.amount_unpaid(july_bill) == Decimal("0.00")


def test_a_partial_payment_makes_the_bill_partial(db_session, owner, july_bill):
    pay(db_session, owner, july_bill, "500")

    assert july_bill.status == "partial"
    assert july_bill.amount_paid == Decimal("500.00")
    assert bills.amount_unpaid(july_bill) == Decimal("950.00")


def test_several_partial_payments_add_up_to_paid(db_session, owner, july_bill):
    pay(db_session, owner, july_bill, "500")
    pay(db_session, owner, july_bill, "450")
    assert (july_bill.status, july_bill.amount_paid) == ("partial", Decimal("950.00"))

    pay(db_session, owner, july_bill, "500")

    assert (july_bill.status, july_bill.amount_paid) == ("paid", Decimal("1450.00"))
    assert payment_count(db_session) == 3
    # PAY-5: the amount paid is always the sum of the payments
    assert july_bill.amount_paid == db_session.scalar(select(func.sum(Payment.amount)))


def test_an_overpayment_is_recorded_as_it_is_and_not_carried_as_credit(db_session, owner, mary, july_bill):
    # PRD section 12: no credit balances
    pay(db_session, owner, july_bill, "2000")
    august = bill_in(db_session, owner, mary, "20", "2025-08-30")

    assert (july_bill.status, july_bill.amount_paid) == ("paid", Decimal("2000.00"))
    assert august.previous_balance == Decimal("0.00")
    assert august.total_due == Decimal("1450.00")


@pytest.mark.parametrize("typed,expected", [("1,000", "1000.00"), ("1000.5", "1000.50"), (" 250 ", "250.00")])
def test_the_amount_may_have_a_comma_cents_and_spaces(db_session, owner, july_bill, typed, expected):
    result = pay(db_session, owner, july_bill, typed)

    assert result.payment.amount == Decimal(expected)


def test_a_payment_keeps_who_recorded_it_and_is_mpesa(db_session, owner, july_bill):
    result = pay(db_session, owner, july_bill, "500", CODE)

    saved = db_session.get(Payment, result.payment.payment_id)
    assert (saved.recorded_by, saved.method, saved.mpesa_reference) == (owner.user_id, "mpesa", CODE)
    assert (saved.bill_id, str(saved.payment_date)) == (july_bill.bill_id, "2025-08-05")


def test_a_customer_who_is_inactive_can_still_pay(db_session, owner, mary, july_bill):
    # CUS-4 stops new bills and SMS. Money that arrived must still be recorded.
    mary.status = "inactive"
    db_session.flush()

    pay(db_session, owner, july_bill, "1450")

    assert july_bill.status == "paid"


# ---- the amount (PAY-4) ----

@pytest.mark.parametrize("typed,part_of_message", [
    ("0", "must be more than 0"),
    ("0.00", "must be more than 0"),
    ("-100", "cannot be less than 0"),
    ("", "Please enter the amount received"),
    ("abc", "as a number"),
    ("NaN", "as a number"),
    ("10.555", "at most 2 decimal places"),
    ("999999999999", "too large"),
])
def test_a_bad_amount_is_refused_and_recorded(db_session, owner, july_bill, typed, part_of_message):
    with pytest.raises(BusinessRuleError) as refused:
        pay(db_session, owner, july_bill, typed, CODE)

    assert refused.value.status_code == 422
    assert refused.value.field == "amount"
    assert part_of_message in refused.value.message
    # nothing was saved
    assert payment_count(db_session) == 0
    assert (july_bill.status, july_bill.amount_paid) == ("unpaid", Decimal("0.00"))
    row = audit_rows(db_session, "payment.rejected")[0]
    assert row.level == "warning" and row.actor_user_id == owner.user_id
    assert row.entity_type == "bill" and row.entity_id == july_bill.bill_id
    assert row.customer_id == july_bill.customer_id
    assert part_of_message in row.details["reason"]
    assert row.details["mpesa_code"] == CODE


# ---- the date ----

def test_the_date_defaults_to_today(db_session, owner, july_bill):
    result = pay(db_session, owner, july_bill, "500", payment_date="")

    assert result.payment.payment_date == payments.today()


@pytest.mark.parametrize("typed,part_of_message", [
    ("30/08/2025", "Please enter the payment date"),
    ("2999-01-01", "cannot be in the future"),
    ("1999-12-31", "Please check the year"),
])
def test_a_bad_date_is_refused_beside_the_date(db_session, owner, july_bill, typed, part_of_message):
    with pytest.raises(BusinessRuleError) as refused:
        pay(db_session, owner, july_bill, "500", payment_date=typed)

    assert refused.value.field == "payment_date"
    assert part_of_message in refused.value.message
    assert payment_count(db_session) == 0


# ---- the M-Pesa code (PAY-2, PAY-8) ----

def test_the_code_is_optional(db_session, owner, july_bill):
    pay(db_session, owner, july_bill, "100")
    result = pay(db_session, owner, july_bill, "100")

    # several payments with no code are fine: only a code that is given must be unique
    assert result.payment.mpesa_reference is None
    assert payment_count(db_session) == 2


def test_the_code_is_saved_in_capitals(db_session, owner, july_bill):
    result = pay(db_session, owner, july_bill, "500", " qgh7x2k9lm ")

    assert result.payment.mpesa_reference == CODE


@pytest.mark.parametrize("typed,part_of_message", [
    ("QGH7X2K9LM1234567890X", "too long"),
    ("QGH 7X2K9", "only have letters and numbers"),
    ("QGH-7X2K9", "only have letters and numbers"),
])
def test_a_badly_shaped_code_is_refused(db_session, owner, july_bill, typed, part_of_message):
    with pytest.raises(BusinessRuleError) as refused:
        pay(db_session, owner, july_bill, "500", typed)

    assert refused.value.field == "mpesa_reference"
    assert part_of_message in refused.value.message


def test_a_duplicate_code_is_refused_and_names_the_payment_it_matches(db_session, owner, mary, grace, july_bill):
    first = pay(db_session, owner, july_bill, "500", CODE)
    graces = bill_in(db_session, owner, grace, "10", "2025-07-29")

    # the same code in another case, on another customer's bill: it is still the same payment
    with pytest.raises(ConflictError) as refused:
        pay(db_session, owner, graces, "500", CODE.lower())

    assert refused.value.status_code == 409
    assert refused.value.field == "mpesa_reference"
    message = refused.value.message
    assert CODE in message and "Ksh 500" in message and "5 Aug 2025" in message
    assert mary.meter_number in message
    assert payment_count(db_session) == 1
    assert (graces.status, graces.amount_paid) == ("unpaid", Decimal("0.00"))
    row = audit_rows(db_session, "payment.rejected")[0]
    assert row.details["reason"] == message
    assert mary.first_name not in row.summary and mary.first_name not in row.details["reason"]
    assert first.payment.mpesa_reference == CODE


def test_a_duplicate_code_that_slips_past_the_check_is_still_refused(db_session, owner, july_bill, monkeypatch):
    # PAY-8, DB-6: two payments with one code, saved at the same moment, are stopped by the database
    pay(db_session, owner, july_bill, "500", CODE)
    real_lookup = payments._find_payment_with_code
    lookups = []

    def blind_the_first_time(db, code):
        lookups.append(code)
        return None if len(lookups) == 1 else real_lookup(db, code)

    monkeypatch.setattr(payments, "_find_payment_with_code", blind_the_first_time)

    with pytest.raises(ConflictError) as refused:
        pay(db_session, owner, july_bill, "200", CODE)

    assert refused.value.status_code == 409 and CODE in refused.value.message
    assert payment_count(db_session) == 1
    assert july_bill.amount_paid == Decimal("500.00")
    assert len(audit_rows(db_session, "payment.rejected")) == 1


# ---- only the latest bill takes a payment (PAY-3) ----

def test_a_bill_that_is_no_longer_the_latest_takes_no_payment(db_session, owner, mary, july_bill):
    august = bill_in(db_session, owner, mary, "20", "2025-08-30")

    with pytest.raises(BusinessRuleError) as refused:
        pay(db_session, owner, july_bill, "500")

    assert "carried forward into a newer bill" in refused.value.message
    assert refused.value.status_code == 422 and refused.value.field is None
    assert payment_count(db_session) == 0
    assert (july_bill.amount_paid, july_bill.status) == (Decimal("0.00"), "unpaid")
    row = audit_rows(db_session, "payment.rejected")[0]
    assert row.entity_id == july_bill.bill_id and "carried forward" in row.details["reason"]
    # the latest bill does take it
    assert pay(db_session, owner, august, "500").bill.status == "partial"


def test_a_missing_bill_is_a_404_and_saves_nothing(db_session, owner):
    with pytest.raises(NotFoundError) as refused:
        payments.record_payment(db_session, owner, 999999, {"amount": "500"})

    assert refused.value.status_code == 404
    assert payment_count(db_session) == 0


# ---- the three months (TASK-056, PAY-6) ----

def test_a_partial_payment_each_month_carries_only_what_is_left(db_session, owner, mary, sms):
    # month 1: 10 units, a bill of 1,450. 500 is paid, so 950 is left.
    july = bill_in(db_session, owner, mary, "10", "2025-07-30")
    pay(db_session, owner, july, "500", "JUL0000001")
    assert (july.status, bills.amount_unpaid(july)) == ("partial", Decimal("950.00"))

    # month 2: 1,450 of new charges and the 950 left from July
    august = bill_in(db_session, owner, mary, "20", "2025-08-30")
    assert august.previous_balance == Decimal("950.00")
    assert august.total_due == Decimal("2400.00")
    pay(db_session, owner, august, "1000", "AUG0000001")
    assert (august.status, bills.amount_unpaid(august)) == ("partial", Decimal("1400.00"))

    # month 3: 1,450 of new charges and the 1,400 left from August. July's 950 is not added
    # again, because it is already inside August's total.
    september = bill_in(db_session, owner, mary, "30", "2025-09-30")
    assert september.previous_balance == Decimal("1400.00")
    assert september.total_due == Decimal("2850.00")

    # nothing is counted twice: everything billed as new charges, minus everything paid
    new_charges = sum(bill.total_due - bill.previous_balance for bill in (july, august, september))
    paid = db_session.scalar(select(func.sum(Payment.amount)))
    assert new_charges - paid == september.total_due == Decimal("2850.00")
    # the earlier bills keep what they were paid, and take no more
    assert (july.amount_paid, august.amount_paid) == (Decimal("500.00"), Decimal("1000.00"))
    with pytest.raises(BusinessRuleError):
        pay(db_session, owner, august, "100")


# ---- the audit log (PAY-5, AUD-1) ----

def test_a_payment_is_recorded_in_the_audit_log(db_session, owner, mary, july_bill):
    result = pay(db_session, owner, july_bill, "500", CODE)

    row = audit_rows(db_session, "payment.recorded")[0]
    assert row.level == "info" and row.actor_user_id == owner.user_id
    assert (row.entity_type, row.entity_id) == ("payment", result.payment.payment_id)
    assert row.customer_id == mary.customer_id
    assert row.details == {"amount": "500.00", "payment_date": "2025-08-05", "mpesa_code": CODE,
                           "amount_paid": "500.00", "total_due": "1450.00", "new_status": "partial"}
    assert "Ksh 500" in row.summary and mary.meter_number in row.summary
    assert mary.first_name not in row.summary and mary.phone not in str(row.details)


def test_a_status_change_is_recorded_only_when_the_status_changes(db_session, owner, july_bill):
    pay(db_session, owner, july_bill, "500")
    pay(db_session, owner, july_bill, "450")
    pay(db_session, owner, july_bill, "500")

    changes = audit_rows(db_session, "bill.status_changed")
    # unpaid to partial, then partial to paid. The second payment left it partial.
    assert [(row.details["old_status"], row.details["new_status"]) for row in changes] == [
        ("unpaid", "partial"), ("partial", "paid")]
    assert changes[0].entity_type == "bill" and changes[0].entity_id == july_bill.bill_id
    assert changes[1].details["amount_paid"] == "1450.00" and changes[1].details["total_due"] == "1450.00"
    assert len(audit_rows(db_session, "payment.recorded")) == 3


def test_a_refused_payment_records_no_success_event(db_session, owner, july_bill):
    with pytest.raises(BusinessRuleError):
        pay(db_session, owner, july_bill, "0")

    assert audit_rows(db_session, "payment.recorded") == []
    assert audit_rows(db_session, "bill.status_changed") == []


def test_an_unexpected_error_saves_nothing_and_is_a_safe_500(db_session, owner, july_bill, monkeypatch):
    def explode(total_due, amount_paid):
        raise RuntimeError("something broke")

    monkeypatch.setattr(bills, "bill_status", explode)

    with pytest.raises(InternalError) as failed:
        pay(db_session, owner, july_bill, "500", CODE)

    assert failed.value.status_code == 500
    assert "something broke" not in failed.value.message
    # the payment was rolled back with its audit row
    assert payment_count(db_session) == 0
    assert audit_rows(db_session, "payment.recorded") == []
    assert db_session.get(Bill, july_bill.bill_id).amount_paid == Decimal("0.00")


# ---- the live line (PAY-2) ----

def test_the_live_line_says_what_would_still_be_owed(db_session, owner, july_bill):
    pay(db_session, owner, july_bill, "250")

    owed = payments.owed_after(db_session, july_bill.bill_id, "1,000")

    assert (owed.left, owed.extra) == (Decimal("200.00"), Decimal("0.00"))


def test_the_live_line_says_when_a_payment_is_more_than_the_bill(db_session, owner, july_bill):
    owed = payments.owed_after(db_session, july_bill.bill_id, "2000")

    assert (owed.left, owed.extra) == (Decimal("0.00"), Decimal("550.00"))


def test_the_live_line_records_nothing(db_session, owner, july_bill):
    payments.owed_after(db_session, july_bill.bill_id, "500")
    with pytest.raises(BusinessRuleError):
        payments.owed_after(db_session, july_bill.bill_id, "0")

    assert payment_count(db_session) == 0
    assert audit_rows(db_session, "payment.rejected") == []


def test_the_live_line_runs_the_same_checks_as_a_save(db_session, owner, mary, july_bill):
    bill_in(db_session, owner, mary, "20", "2025-08-30")

    with pytest.raises(BusinessRuleError) as zero:
        payments.owed_after(db_session, bills.latest_bill(db_session, mary.customer_id).bill_id, "0")
    with pytest.raises(BusinessRuleError) as older:
        payments.owed_after(db_session, july_bill.bill_id, "500")
    with pytest.raises(NotFoundError):
        payments.owed_after(db_session, 999999, "500")

    assert zero.value.field == "amount" and "must be more than 0" in zero.value.message
    assert "carried forward" in older.value.message


# ---- what the bill page needs ----

def test_the_bill_details_list_the_payments_and_the_latest_bill(db_session, owner, mary, july_bill):
    pay(db_session, owner, july_bill, "500", CODE, "2025-08-06")
    pay(db_session, owner, july_bill, "100", payment_date="2025-08-04")
    august = bill_in(db_session, owner, mary, "20", "2025-08-30")

    details = payments.payment_details(db_session, july_bill.bill_id)

    # oldest first, by the day the money arrived
    assert [payment.amount for payment in details.payments] == [Decimal("100.00"), Decimal("500.00")]
    assert details.latest_bill.bill_id == august.bill_id
    assert details.still_owed == Decimal("850.00")
    with pytest.raises(NotFoundError):
        payments.payment_details(db_session, 999999)


# ---- PAY-9: payments are never edited or deleted ----

def test_the_payments_service_has_no_way_to_edit_or_delete_a_payment():
    # only the functions written in payments/service.py, not the ones it imports
    names = [name for name, function in inspect.getmembers(payments, inspect.isfunction)
             if function.__module__ == payments.__name__]
    changing_words = ("update", "edit", "delete", "remove", "change")

    assert [name for name in names if any(word in name for word in changing_words)] == []
    assert "record_payment" in names
