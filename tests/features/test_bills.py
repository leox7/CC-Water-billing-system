"""bills/service.py (TASK-042, TASK-043, BILL-1 to BILL-7, PAY-5). The SMS provider is the
stand-in from conftest.py."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import BusinessRuleError, NotFoundError
from app.features.bills import service as bills
from app.models import AuditLog, Bill, MessageLog, MeterReading, Payment


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel")


@pytest.fixture
def mary(make_customer):
    return make_customer(first_name="Mary")


def add_reading(db_session, customer, user, period, previous, current):
    # to the cent, as the readings service saves them
    previous, current = Decimal(previous).quantize(Decimal("0.01")), Decimal(current).quantize(Decimal("0.01"))
    reading = MeterReading(customer_id=customer.customer_id, billing_period=period,
                           previous_reading=previous, current_reading=current, units_consumed=current - previous,
                           reading_date=date.fromisoformat(period + "-28"), recorded_by=user.user_id)
    db_session.add(reading)
    db_session.flush()
    return reading


def bill_for(db_session, owner, customer, period, previous, current):
    return bills.generate_bill(db_session, owner, add_reading(db_session, customer, owner, period, previous, current))


def pay(db_session, owner, bill, amount):
    """A payment as Phase 5 will record it: the row, the amount paid and the status."""
    db_session.add(Payment(bill_id=bill.bill_id, amount=Decimal(amount), payment_date=date(2026, 9, 2),
                           recorded_by=owner.user_id))
    bill.amount_paid += Decimal(amount)
    bill.status = bills.bill_status(bill.total_due, bill.amount_paid)
    db_session.flush()


# ---- generate_bill ----

def test_units_times_rate_plus_service_charge(db_session, owner, mary):
    # BILL-1 to BILL-3: 120 to 135 is 15 units, 15 x 130 = 1,950, plus 150 = 2,100
    bill = bill_for(db_session, owner, mary, "2026-08", "120", "135")

    assert bill.units_consumed == Decimal("15")
    assert bill.consumption_charge == Decimal("1950.00")
    assert bill.service_charge == Decimal("150.00")
    assert bill.previous_balance == Decimal("0.00")
    assert bill.total_due == Decimal("2100.00")
    # BILL-7
    assert (bill.status, bill.amount_paid) == ("unpaid", Decimal("0.00"))


def test_the_first_bill_starts_from_a_balance_of_zero(db_session, owner, mary):
    bill = bill_for(db_session, owner, mary, "2026-08", "0", "12")

    assert bill.previous_balance == Decimal("0.00")
    assert bill.total_due == Decimal("1710.00")


def test_the_worked_example_carries_the_unpaid_part_forward(db_session, owner, mary):
    # PRD 4.5: 2,100 plus 500 carried is 2,600. 1,000 paid leaves 1,600, which carries on
    july = bill_for(db_session, owner, mary, "2026-07", "100", "103")
    pay(db_session, owner, july, "40")
    august = bill_for(db_session, owner, mary, "2026-08", "120", "135")
    assert august.previous_balance == Decimal("500.00")
    assert august.total_due == Decimal("2600.00")

    pay(db_session, owner, august, "1000")
    september = bill_for(db_session, owner, mary, "2026-09", "135", "135")

    assert august.status == "partial"
    assert september.previous_balance == Decimal("1600.00")


def test_three_unpaid_months_carry_only_the_latest_bill(db_session, owner, mary):
    # BILL-4: the previous balance is the latest earlier bill's balance, never a sum of older bills
    june = bill_for(db_session, owner, mary, "2026-06", "0", "15")
    july = bill_for(db_session, owner, mary, "2026-07", "15", "30")
    august = bill_for(db_session, owner, mary, "2026-08", "30", "45")

    assert june.total_due == Decimal("2100.00")
    assert july.previous_balance == Decimal("2100.00")
    assert july.total_due == Decimal("4200.00")
    assert august.previous_balance == Decimal("4200.00")
    assert august.total_due == Decimal("6300.00")


def test_a_fully_paid_bill_carries_nothing(db_session, owner, mary):
    july = bill_for(db_session, owner, mary, "2026-07", "0", "15")
    pay(db_session, owner, july, "2100")

    august = bill_for(db_session, owner, mary, "2026-08", "15", "30")

    assert july.status == "paid"
    assert august.previous_balance == Decimal("0.00")


def test_an_overpayment_is_not_carried_as_credit(db_session, owner, mary):
    july = bill_for(db_session, owner, mary, "2026-07", "0", "15")
    pay(db_session, owner, july, "3000")

    august = bill_for(db_session, owner, mary, "2026-08", "15", "30")

    assert august.previous_balance == Decimal("0.00")
    assert august.total_due == Decimal("2100.00")


def test_zero_consumption_is_billed_the_service_charge_and_the_balance(db_session, owner, mary):
    july = bill_for(db_session, owner, mary, "2026-07", "0", "10")
    august = bill_for(db_session, owner, mary, "2026-08", "10", "10")

    assert august.units_consumed == Decimal("0.00")
    assert august.consumption_charge == Decimal("0.00")
    assert august.total_due == Decimal("150.00") + july.total_due


def test_the_rate_and_service_charge_are_copied_onto_the_bill(db_session, owner, mary):
    # BILL-5: changing the customer later never changes an old bill
    august = bill_for(db_session, owner, mary, "2026-08", "0", "10")
    pay(db_session, owner, august, "1450")
    mary.rate_per_unit = Decimal("160.00")
    mary.service_charge = Decimal("200.00")
    db_session.flush()

    september = bill_for(db_session, owner, mary, "2026-09", "10", "20")
    db_session.refresh(august)

    assert (august.rate_per_unit, august.service_charge, august.total_due) == (Decimal("130.00"), Decimal("150.00"),
                                                                              Decimal("1450.00"))
    assert (september.rate_per_unit, september.service_charge) == (Decimal("160.00"), Decimal("200.00"))
    assert september.total_due == Decimal("1800.00")


def test_a_charge_is_rounded_to_the_cent_once(db_session, owner, make_customer):
    grace = make_customer(first_name="Grace", rate_per_unit="130.50")

    bill = bill_for(db_session, owner, grace, "2026-08", "0", "100.25")

    # 100.25 x 130.50 = 13,082.625
    assert bill.consumption_charge == Decimal("13082.63")
    assert bill.total_due == Decimal("13232.63")


def test_only_bills_from_earlier_months_are_carried(db_session, owner, mary, make_customer):
    grace = make_customer(first_name="Grace")
    bill_for(db_session, owner, grace, "2026-07", "0", "50")

    august = bill_for(db_session, owner, mary, "2026-08", "0", "10")

    # another customer's bill is never carried, and nothing comes from August itself
    assert august.previous_balance == Decimal("0.00")
    assert bills.outstanding_balance(db_session, mary.customer_id, "2026-08") == Decimal("0.00")
    assert bills.outstanding_balance(db_session, mary.customer_id, "2026-09") == Decimal("1450.00")


def test_a_bill_too_large_for_the_column_is_refused(db_session, owner, make_customer):
    costly = make_customer(rate_per_unit="99999999.99")

    with pytest.raises(BusinessRuleError) as raised:
        bill_for(db_session, owner, costly, "2026-08", "0", "99999999.99")

    assert raised.value.field == "current_reading"
    assert "too large" in raised.value.message


def test_bill_generated_is_recorded_with_the_figures(db_session, owner, mary):
    bill = bill_for(db_session, owner, mary, "2026-08", "120", "135")

    row = audit_rows(db_session, "bill.generated")[0]
    assert (row.level, row.entity_type, row.entity_id, row.customer_id) == ("info", "bill", bill.bill_id,
                                                                           mary.customer_id)
    assert row.actor_user_id == owner.user_id
    assert row.details == {"period": "2026-08", "units": "15.00", "rate": "130.00", "service_charge": "150.00",
                           "previous_balance": "0.00", "total_due": "2100.00"}
    assert row.summary == f"Bill of Ksh 2,100 for {mary.meter_number} (2026-08)"


# ---- the status rule (PAY-5) ----

@pytest.mark.parametrize("paid,expected", [("0", "unpaid"), ("0.01", "partial"), ("2099.99", "partial"),
                                          ("2100", "paid"), ("2500", "paid")])
def test_the_status_comes_from_the_amount_paid(paid, expected):
    assert bills.bill_status(Decimal("2100"), Decimal(paid)) == expected


# ---- recalculate_bill (TASK-043) ----

def test_a_recalculated_bill_has_the_right_total_and_keeps_its_payments(db_session, owner, mary):
    july = bill_for(db_session, owner, mary, "2026-07", "100", "103")
    pay(db_session, owner, july, "40")
    reading = add_reading(db_session, mary, owner, "2026-08", "120", "135")
    august = bills.generate_bill(db_session, owner, reading)
    pay(db_session, owner, august, "1000")

    # the reading was 140, not 135: 20 units
    reading.current_reading = Decimal("140.00")
    reading.units_consumed = Decimal("20.00")
    bills.recalculate_bill(db_session, owner, august, reading)

    # 20 x 130 = 2,600, plus 150, plus the 500 carried = 3,250
    assert august.consumption_charge == Decimal("2600.00")
    assert august.previous_balance == Decimal("500.00")
    assert august.total_due == Decimal("3250.00")
    assert august.amount_paid == Decimal("1000.00")
    assert august.status == "partial"
    payments = db_session.scalars(select(Payment).where(Payment.bill_id == august.bill_id)).all()
    assert [payment.amount for payment in payments] == [Decimal("1000.00")]


def test_a_recalculated_bill_keeps_its_own_rate(db_session, owner, mary):
    reading = add_reading(db_session, mary, owner, "2026-08", "0", "10")
    august = bills.generate_bill(db_session, owner, reading)
    mary.rate_per_unit = Decimal("160.00")
    reading.current_reading, reading.units_consumed = Decimal("12"), Decimal("12")

    bills.recalculate_bill(db_session, owner, august, reading)

    assert august.rate_per_unit == Decimal("130.00")
    assert august.total_due == Decimal("1710.00")


def test_a_lower_total_can_turn_a_partial_bill_paid(db_session, owner, mary):
    reading = add_reading(db_session, mary, owner, "2026-08", "0", "15")
    august = bills.generate_bill(db_session, owner, reading)
    pay(db_session, owner, august, "1500")
    assert august.status == "partial"

    reading.current_reading, reading.units_consumed = Decimal("10"), Decimal("10")
    bills.recalculate_bill(db_session, owner, august, reading)

    assert august.total_due == Decimal("1450.00")
    assert august.status == "paid"


def test_bill_recalculated_is_recorded_with_old_and_new_totals(db_session, owner, mary):
    reading = add_reading(db_session, mary, owner, "2026-08", "120", "135")
    august = bills.generate_bill(db_session, owner, reading)
    pay(db_session, owner, august, "1000")
    reading.current_reading, reading.units_consumed = Decimal("140"), Decimal("20")

    bills.recalculate_bill(db_session, owner, august, reading)

    row = audit_rows(db_session, "bill.recalculated")[0]
    assert (row.level, row.entity_type, row.entity_id, row.customer_id) == ("info", "bill", august.bill_id,
                                                                           mary.customer_id)
    assert row.details == {"old_total_due": "2100.00", "new_total_due": "2750.00", "amount_paid": "1000.00",
                           "new_status": "partial"}


# ---- delete_bill ----

def test_a_deleted_bill_keeps_its_messages_with_the_bill_link_emptied(db_session, owner, mary):
    # COR-4
    reading = add_reading(db_session, mary, owner, "2026-08", "120", "135")
    august = bills.generate_bill(db_session, owner, reading)
    bill_id = august.bill_id
    db_session.add(MessageLog(bill_id=bill_id, customer_id=mary.customer_id, message_type="bill",
                              content="Hi Mary,", status="failed"))
    db_session.flush()

    bills.delete_bill(db_session, owner, august)
    db_session.expire_all()

    assert db_session.get(Bill, bill_id) is None
    kept = db_session.scalars(select(MessageLog).where(MessageLog.customer_id == mary.customer_id)).all()
    assert [(message.bill_id, message.status) for message in kept] == [(None, "failed")]
    row = audit_rows(db_session, "bill.deleted")[0]
    assert (row.level, row.entity_type, row.entity_id, row.customer_id) == ("info", "bill", bill_id,
                                                                           mary.customer_id)
    assert row.details == {"units": "15.00", "rate": "130.00", "service_charge": "150.00",
                           "previous_balance": "0.00", "total_due": "2100.00"}


# ---- the bill's SMS ----

def test_the_bill_sms_goes_to_the_customer_with_the_bill_figures(db_session, owner, mary, sms):
    bill = bill_for(db_session, owner, mary, "2026-08", "120", "135")

    message = bills.send_bill_sms(db_session, owner, bill)

    assert message.status == "sent"
    phone, text = sms.sent[0]
    assert phone == mary.phone
    assert "Hi Mary,\nPrevious Reading:120\nCurrent Reading:135\n" in text
    assert "Amount Due: Ksh 2,100" in text
    assert bills.sms_was_sent(db_session, bill.bill_id)


def test_a_failed_sms_does_not_count_as_sent(db_session, owner, mary, sms):
    bill = bill_for(db_session, owner, mary, "2026-08", "120", "135")
    sms.failure = "Could not reach Africa's Talking (ConnectError)"

    assert bills.send_bill_sms(db_session, owner, bill).status == "failed"
    assert not bills.sms_was_sent(db_session, bill.bill_id)


def test_the_correction_sms_shows_what_is_still_owed(db_session, owner, mary, sms):
    reading = add_reading(db_session, mary, owner, "2026-08", "120", "135")
    bill = bills.generate_bill(db_session, owner, reading)
    pay(db_session, owner, bill, "1000")

    message = bills.send_correction_sms(db_session, owner, bill)

    assert message.message_type == "correction"
    assert "Paid so far: Ksh 1,000\nBalance: Ksh 1,100\n" in sms.sent[0][1]


# ---- the bills list and the bill page (TASK-049, TASK-050) ----

@pytest.fixture
def three_bills(db_session, owner, mary, make_customer):
    """Mary's July and August bills, and Grace's August bill (paid)."""
    grace = make_customer(first_name="Grace", last_name="Njeri")
    july = bill_for(db_session, owner, mary, "2026-07", "0", "10")
    august = bill_for(db_session, owner, mary, "2026-08", "10", "20")
    grace_august = bill_for(db_session, owner, grace, "2026-08", "0", "5")
    pay(db_session, owner, grace_august, "800")
    return july, august, grace_august


def test_the_bills_list_is_newest_month_first_then_by_name(db_session, three_bills):
    july, august, grace_august = three_bills

    rows = bills.list_bills(db_session)

    assert [row.bill.bill_id for row in rows] == [grace_august.bill_id, august.bill_id, july.bill_id]
    assert (rows[0].customer.first_name, rows[0].reading.billing_period) == ("Grace", "2026-08")


def test_the_bills_list_filters_by_month_and_status(db_session, three_bills):
    july, august, grace_august = three_bills

    assert [row.bill.bill_id for row in bills.list_bills(db_session, period="2026-07")] == [july.bill_id]
    assert [row.bill.bill_id for row in bills.list_bills(db_session, status="paid")] == [grace_august.bill_id]
    assert [row.bill.bill_id for row in bills.list_bills(db_session, "2026-08", "unpaid")] == [august.bill_id]
    assert bills.list_bills(db_session, "2026-06") == []


@pytest.mark.parametrize("period,status,field", [
    ("2026-13", "", "month"), ("August", "", "month"), ("2026-8", "", "month"),
    ("", "carried", "status"), ("", "PAID", "status"),
])
def test_a_filter_value_not_on_the_list_is_refused(db_session, period, status, field):
    # IN-6
    with pytest.raises(BusinessRuleError) as raised:
        bills.list_bills(db_session, period, status)

    assert raised.value.field == field


def test_the_months_with_bills_are_newest_first(db_session, three_bills):
    assert bills.bill_periods(db_session) == ["2026-08", "2026-07"]


def test_the_bill_details_hold_the_customer_reading_and_messages_oldest_first(db_session, owner, mary, sms):
    bill = bill_for(db_session, owner, mary, "2026-08", "120", "135")
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    bills.send_bill_sms(db_session, owner, bill)
    sms.failure = None
    bills.resend_sms(db_session, owner, bill.bill_id)

    details = bills.bill_details(db_session, bill.bill_id)

    assert (details.customer.customer_id, details.reading.reading_id) == (mary.customer_id, bill.reading_id)
    assert [message.status for message in details.messages] == ["failed", "sent"]


def test_an_unknown_bill_is_not_found(db_session, owner):
    with pytest.raises(NotFoundError):
        bills.bill_details(db_session, 999999)
    with pytest.raises(NotFoundError):
        bills.resend_sms(db_session, owner, 999999)


# ---- the opening balance on a first bill (CUS-5) ----

def test_a_first_bill_carries_the_opening_balance(db_session, owner, mary):
    reading = add_reading(db_session, mary, owner, "2026-07", "3400", "3420")

    bill = bills.generate_bill(db_session, owner, reading, Decimal("1200.00"))

    assert (bill.previous_balance, bill.total_due) == (Decimal("1200.00"), Decimal("3950.00"))


def test_an_opening_balance_is_ignored_once_there_is_an_earlier_bill(db_session, owner, mary):
    bill_for(db_session, owner, mary, "2026-07", "0", "10")
    reading = add_reading(db_session, mary, owner, "2026-08", "10", "20")

    august = bills.generate_bill(db_session, owner, reading, Decimal("9999.00"))

    # BILL-4 only: July's unpaid 1,450
    assert august.previous_balance == Decimal("1450.00")


def test_a_recalculated_first_bill_takes_a_corrected_opening_balance_and_later_bills_never_do(db_session, owner,
                                                                                            mary):
    july_reading = add_reading(db_session, mary, owner, "2026-07", "0", "10")
    july = bills.generate_bill(db_session, owner, july_reading, Decimal("500.00"))
    august_reading = add_reading(db_session, mary, owner, "2026-08", "10", "20")
    august = bills.generate_bill(db_session, owner, august_reading)

    bills.recalculate_bill(db_session, owner, july, july_reading, Decimal("300.00"))
    bills.recalculate_bill(db_session, owner, august, august_reading, Decimal("1.00"))

    assert (july.previous_balance, july.total_due) == (Decimal("300.00"), Decimal("1750.00"))
    assert august.previous_balance == Decimal("1950.00")
