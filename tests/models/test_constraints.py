"""The database is the second safety net behind the service checks (DB-6).
These tests prove MySQL itself refuses what the rules forbid."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

# MySQL reports a violated CHECK constraint as error 3819, which pymysql raises as
# OperationalError. Unique and foreign key violations are IntegrityError.
CHECK_VIOLATED = "3819"

from app.models import Bill, Customer, MeterReading, Payment, User


def make_user(db_session, phone="254722000001", email=None):
    user = User(first_name="Test", last_name="Owner", phone=phone, email=email,
                password_hash="not-a-real-hash", role="owner")
    db_session.add(user)
    db_session.flush()
    return user


def make_customer(db_session, meter_number="MTR-0001"):
    customer = Customer(first_name="Mary", last_name="Wanjiru", phone="254722000002",
                        location="Kiawara", meter_number=meter_number)
    db_session.add(customer)
    db_session.flush()
    return customer


def make_reading(db_session, customer, user, period="2026-08", previous="0", current="10"):
    reading = MeterReading(
        customer_id=customer.customer_id, billing_period=period,
        previous_reading=Decimal(previous), current_reading=Decimal(current),
        units_consumed=Decimal(current) - Decimal(previous),
        reading_date=date(2026, 8, 20), recorded_by=user.user_id,
    )
    db_session.add(reading)
    db_session.flush()
    return reading


def make_bill(db_session, reading):
    bill = Bill(reading_id=reading.reading_id, customer_id=reading.customer_id,
                rate_per_unit=Decimal("130"), service_charge=Decimal("150"),
                units_consumed=reading.units_consumed, consumption_charge=Decimal("1300"),
                total_due=Decimal("1450"))
    db_session.add(bill)
    db_session.flush()
    return bill


def test_new_rows_get_the_documented_defaults(db_session):
    customer = make_customer(db_session)
    user = make_user(db_session)
    bill = make_bill(db_session, make_reading(db_session, customer, user))
    db_session.refresh(customer)
    db_session.refresh(bill)

    assert customer.rate_per_unit == Decimal("130.00")      # CUS-1
    assert customer.service_charge == Decimal("150.00")     # CUS-1
    assert customer.status == "active"
    assert bill.status == "unpaid"                          # BILL-7
    assert bill.amount_paid == Decimal("0.00")
    assert bill.previous_balance == Decimal("0.00")


def test_a_user_needs_a_phone_or_an_email(db_session):
    with pytest.raises(OperationalError, match=CHECK_VIOLATED):
        make_user(db_session, phone=None, email=None)


def test_phone_and_email_must_be_unique(db_session):
    make_user(db_session, phone="254722000001", email="a@example.com")
    with pytest.raises(IntegrityError):
        make_user(db_session, phone="254722000001")


def test_meter_numbers_are_unique(db_session):
    make_customer(db_session, "MTR-0001")
    with pytest.raises(IntegrityError):
        make_customer(db_session, "MTR-0001")


def test_one_reading_per_customer_per_period(db_session):
    customer, user = make_customer(db_session), make_user(db_session)
    make_reading(db_session, customer, user, period="2026-08")
    with pytest.raises(IntegrityError):
        make_reading(db_session, customer, user, period="2026-08")


def test_a_reading_cannot_be_lower_than_the_previous_one(db_session):
    customer, user = make_customer(db_session), make_user(db_session)
    with pytest.raises(OperationalError, match=CHECK_VIOLATED):
        make_reading(db_session, customer, user, previous="200", current="150")


def test_one_bill_per_reading(db_session):
    customer, user = make_customer(db_session), make_user(db_session)
    reading = make_reading(db_session, customer, user)
    make_bill(db_session, reading)
    with pytest.raises(IntegrityError):
        make_bill(db_session, reading)


def test_a_payment_must_be_more_than_zero(db_session):
    customer, user = make_customer(db_session), make_user(db_session)
    bill = make_bill(db_session, make_reading(db_session, customer, user))
    db_session.add(Payment(bill_id=bill.bill_id, amount=Decimal("0"),
                           payment_date=date(2026, 9, 1), recorded_by=user.user_id))
    with pytest.raises(OperationalError, match=CHECK_VIOLATED):
        db_session.flush()


def test_an_mpesa_reference_can_only_be_recorded_once(db_session):
    customer, user = make_customer(db_session), make_user(db_session)
    bill = make_bill(db_session, make_reading(db_session, customer, user))
    for _ in range(2):
        db_session.add(Payment(bill_id=bill.bill_id, amount=Decimal("500"),
                               payment_date=date(2026, 9, 1), mpesa_reference="QWE123ABC",
                               recorded_by=user.user_id))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_payments_without_a_reference_can_repeat(db_session):
    customer, user = make_customer(db_session), make_user(db_session)
    bill = make_bill(db_session, make_reading(db_session, customer, user))
    for _ in range(2):
        db_session.add(Payment(bill_id=bill.bill_id, amount=Decimal("500"),
                               payment_date=date(2026, 9, 1), recorded_by=user.user_id))
    db_session.flush()


def test_test_data_is_rolled_back_after_each_test(db_session):
    # the tests above each committed their rows into a savepoint only
    assert db_session.query(Customer).count() == 0
    assert db_session.query(User).count() == 0
