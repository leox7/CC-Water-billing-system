"""readings/service.py: correcting and deleting a reading (TASK-046, PRD 4.4, COR-1 to COR-4).

The three cases:
1. no bill: the reading is deleted and entered again;
2. a bill not sent and not paid: the bill and the reading are deleted and entered again;
3. a bill sent or paid: the reading is edited in place and the bill recalculated.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import BusinessRuleError, NotFoundError
from app.features.bills import service as bills
from app.features.readings import service as readings
from app.models import AuditLog, Bill, MessageLog, MeterReading, Payment


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)
                              .order_by(AuditLog.audit_id)).all()


def readings_of(db_session, customer):
    return db_session.scalars(select(MeterReading).where(MeterReading.customer_id == customer.customer_id)
                              .order_by(MeterReading.billing_period)).all()


def typed(current, reading_date="2025-08-30"):
    return {"current_reading": current, "reading_date": reading_date}


def pay(db_session, owner, bill, amount):
    """A payment as Phase 5 will record it."""
    db_session.add(Payment(bill_id=bill.bill_id, amount=Decimal(amount), payment_date=date(2025, 9, 2),
                           recorded_by=owner.user_id))
    bill.amount_paid += Decimal(amount)
    bill.status = bills.bill_status(bill.total_due, bill.amount_paid)
    db_session.commit()


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel")


@pytest.fixture
def mary(make_customer):
    return make_customer(first_name="Mary")


@pytest.fixture
def july(db_session, owner, mary, sms):
    """Mary's July reading of 120 (bill sent)."""
    return readings.create_reading(db_session, owner, mary.customer_id, typed("120", "2025-07-30")).reading


def reading_without_a_bill(db_session, owner, mary, july):
    reading = MeterReading(customer_id=mary.customer_id, billing_period="2025-08", previous_reading=Decimal("120"),
                           current_reading=Decimal("135"), units_consumed=Decimal("15"),
                           reading_date=date(2025, 8, 30), recorded_by=owner.user_id)
    db_session.add(reading)
    db_session.commit()
    return reading


def reading_with_a_failed_sms(db_session, owner, mary, july, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    result = readings.create_reading(db_session, owner, mary.customer_id, typed("135"))
    sms.failure = None
    return result


def reading_with_a_sent_sms(db_session, owner, mary, july, sms):
    return readings.create_reading(db_session, owner, mary.customer_id, typed("135"))


# ---- which case applies ----

def test_the_case_follows_how_far_the_bill_got(db_session, owner, mary, july, sms):
    no_bill = reading_without_a_bill(db_session, owner, mary, july)
    assert readings.correction_case(db_session, no_bill) == readings.NO_BILL

    db_session.delete(no_bill)
    db_session.commit()
    not_sent = reading_with_a_failed_sms(db_session, owner, mary, july, sms)
    assert readings.correction_case(db_session, not_sent.reading) == readings.BILL_NOT_SENT

    pay(db_session, owner, not_sent.bill, "100")
    assert readings.correction_case(db_session, not_sent.reading) == readings.BILL_SENT_OR_PAID


def test_a_sent_sms_makes_it_the_third_case(db_session, owner, mary, july, sms):
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)

    assert readings.correction_case(db_session, sent.reading) == readings.BILL_SENT_OR_PAID


# ---- case 1: no bill ----

def test_a_reading_with_no_bill_is_deleted_and_entered_again(db_session, owner, mary, july, sms):
    old = reading_without_a_bill(db_session, owner, mary, july)
    old_id = old.reading_id

    result = readings.correct_reading(db_session, owner, old_id, typed("140"))

    assert result.correction_case == readings.NO_BILL
    assert db_session.get(MeterReading, old_id) is None
    new = result.reading
    assert (new.billing_period, new.reading_date) == ("2025-08", date(2025, 8, 30))
    assert (new.previous_reading, new.current_reading, new.units_consumed) == (
        Decimal("120.00"), Decimal("140.00"), Decimal("20.00"))
    # it gets its bill and bill SMS, like any new reading
    assert result.bill.reading_id == new.reading_id
    assert result.sms_status == "sent"
    deleted = audit_rows(db_session, "reading.deleted")[0]
    assert (deleted.entity_type, deleted.entity_id, deleted.customer_id) == ("reading", old_id, mary.customer_id)
    assert deleted.details == {"period": "2025-08", "previous": "120.00", "current": "135.00", "units": "15.00",
                               "case": "no bill"}
    assert audit_rows(db_session, "bill.deleted") == []


# ---- case 2: a bill not sent and not paid ----

def test_a_bill_not_sent_is_deleted_with_its_reading_and_both_are_entered_again(db_session, owner, mary, july, sms):
    old = reading_with_a_failed_sms(db_session, owner, mary, july, sms)
    old_reading_id, old_bill_id = old.reading.reading_id, old.bill.bill_id

    result = readings.correct_reading(db_session, owner, old_reading_id, typed("140"))
    db_session.expire_all()

    assert result.correction_case == readings.BILL_NOT_SENT
    assert db_session.get(MeterReading, old_reading_id) is None
    assert db_session.get(Bill, old_bill_id) is None
    assert result.bill.total_due == Decimal("2750.00") + result.bill.previous_balance
    # COR-4: the failed attempt is kept, with its bill link emptied
    kept = db_session.scalars(select(MessageLog).where(MessageLog.customer_id == mary.customer_id,
                                                       MessageLog.bill_id.is_(None))).all()
    assert [message.status for message in kept] == ["failed"]
    # the new bill went out as a normal bill SMS
    new_message = db_session.scalars(select(MessageLog).where(MessageLog.bill_id == result.bill.bill_id)).one()
    assert (new_message.message_type, new_message.status) == ("bill", "sent")
    assert audit_rows(db_session, "bill.deleted")[0].entity_id == old_bill_id
    assert audit_rows(db_session, "reading.deleted")[0].details["case"] == "bill not sent"


# ---- case 3: a bill sent or paid ----

def test_a_sent_bill_is_corrected_in_place_with_a_correction_sms(db_session, owner, mary, july, sms):
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)

    result = readings.correct_reading(db_session, owner, sent.reading.reading_id, typed("140"))

    assert result.correction_case == readings.BILL_SENT_OR_PAID
    # the same reading and bill, changed
    assert result.reading.reading_id == sent.reading.reading_id
    assert result.bill.bill_id == sent.bill.bill_id
    assert (result.reading.current_reading, result.reading.units_consumed) == (Decimal("140.00"), Decimal("20.00"))
    assert result.bill.consumption_charge == Decimal("2600.00")
    # SMS-12: the customer is told
    assert result.sms_status == "sent"
    phone, text = sms.sent[-1]
    # SMS-12: the full corrected bill
    assert text.startswith("C&C H2O WATER SUPPLIES\nHi Mary,\nWe corrected your meter reading.\n"
                           "Previous Reading:120\nCurrent Reading:140\nConsumption (m3):20\n"
                           "Consumption(130ksh/m3):2,600\n")
    messages = db_session.scalars(select(MessageLog).where(MessageLog.bill_id == sent.bill.bill_id)
                                  .order_by(MessageLog.message_id)).all()
    assert [message.message_type for message in messages] == ["bill", "correction"]

    updated = audit_rows(db_session, "reading.updated")[0]
    assert (updated.entity_type, updated.entity_id) == ("reading", sent.reading.reading_id)
    assert updated.details == {"old_current": "135.00", "new_current": "140.00", "old_units": "15.00",
                               "new_units": "20.00", "case": "bill sent or paid"}
    assert audit_rows(db_session, "bill.recalculated")[0].entity_id == sent.bill.bill_id
    assert audit_rows(db_session, "reading.deleted") == []


def test_a_paid_bill_keeps_its_payments_and_the_correction_sms_shows_them(db_session, owner, mary, july, sms):
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)
    pay(db_session, owner, sent.bill, "1000")

    result = readings.correct_reading(db_session, owner, sent.reading.reading_id, typed("140"))

    assert result.bill.amount_paid == Decimal("1000.00")
    assert result.bill.status == "partial"
    payments = db_session.scalars(select(Payment).where(Payment.bill_id == sent.bill.bill_id)).all()
    assert [payment.amount for payment in payments] == [Decimal("1000.00")]
    balance = result.bill.total_due - Decimal("1000")
    assert f"Paid so far: Ksh 1,000\nBalance: Ksh {balance:,.0f}\n" in sms.sent[-1][1]


def test_a_paid_bill_whose_sms_never_went_gets_the_normal_bill_sms(db_session, owner, mary, july, sms):
    not_sent = reading_with_a_failed_sms(db_session, owner, mary, july, sms)
    pay(db_session, owner, not_sent.bill, "500")

    result = readings.correct_reading(db_session, owner, not_sent.reading.reading_id, typed("140"))

    assert result.correction_case == readings.BILL_SENT_OR_PAID
    assert result.reading.reading_id == not_sent.reading.reading_id
    newest = db_session.scalars(select(MessageLog).where(MessageLog.bill_id == not_sent.bill.bill_id)
                                .order_by(MessageLog.message_id.desc())).first()
    assert (newest.message_type, newest.status) == ("bill", "sent")
    assert "Current Reading:140\n" in newest.content and "We corrected" not in newest.content


def test_a_correction_still_saves_when_the_daily_sms_limit_is_reached(db_session, owner, mary, july, sms):
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)
    for _ in range(4):
        db_session.add(MessageLog(bill_id=sent.bill.bill_id, customer_id=mary.customer_id, message_type="bill",
                                  content="Hi Mary,", status="failed"))
    db_session.commit()

    result = readings.correct_reading(db_session, owner, sent.reading.reading_id, typed("140"))

    assert result.sms_status == "blocked"
    assert result.reading.current_reading == Decimal("140.00")
    assert audit_rows(db_session, "sms.send_blocked")[0].entity_id == sent.bill.bill_id


# ---- corrections that are refused ----

def test_only_the_latest_reading_can_be_corrected(db_session, owner, mary, july, sms):
    # COR-3
    reading_with_a_sent_sms(db_session, owner, mary, july, sms)

    with pytest.raises(BusinessRuleError) as raised:
        readings.correct_reading(db_session, owner, july.reading_id, typed("118", "2025-07-30"))

    assert raised.value.message == "Only the latest reading can be corrected."
    db_session.refresh(july)
    assert july.current_reading == Decimal("120.00")
    row = audit_rows(db_session, "reading.rejected")[0]
    assert (row.level, row.entity_type, row.entity_id) == ("warning", "reading", july.reading_id)
    assert row.details["reason"] == "Only the latest reading can be corrected."


def test_a_correction_lower_than_the_previous_reading_is_refused(db_session, owner, mary, july, sms):
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)

    with pytest.raises(BusinessRuleError) as raised:
        readings.correct_reading(db_session, owner, sent.reading.reading_id, typed("110"))

    assert raised.value.message == "This is lower than last month's reading (120). Please check the number."
    db_session.refresh(sent.reading)
    assert sent.reading.current_reading == Decimal("135.00")


def test_the_same_number_again_is_not_a_correction(db_session, owner, mary, july, sms):
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)

    with pytest.raises(BusinessRuleError) as raised:
        readings.correct_reading(db_session, owner, sent.reading.reading_id, typed("135.00"))

    assert raised.value.message == "That is the reading already saved. Type the corrected number."
    assert len(sms.sent) == 2


def test_an_inactive_customer_s_reading_is_not_corrected(db_session, owner, mary, july, sms):
    # COR-5
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)
    mary.status = "inactive"
    db_session.commit()

    with pytest.raises(BusinessRuleError) as raised:
        readings.correct_reading(db_session, owner, sent.reading.reading_id, typed("140"))

    assert raised.value.message == "Reactivate the customer first."
    db_session.refresh(sent.reading)
    assert sent.reading.current_reading == Decimal("135.00")
    assert len(sms.sent) == 2
    row = audit_rows(db_session, "reading.rejected")[0]
    assert (row.entity_id, row.details["reason"]) == (sent.reading.reading_id, "Reactivate the customer first.")


def test_an_inactive_customer_s_reading_is_not_deleted(db_session, owner, mary, july, sms):
    # COR-5: even in a case where an active customer's reading could be deleted
    not_sent = reading_with_a_failed_sms(db_session, owner, mary, july, sms)
    mary.status = "inactive"
    db_session.commit()

    with pytest.raises(BusinessRuleError) as raised:
        readings.delete_reading(db_session, owner, not_sent.reading.reading_id)

    assert raised.value.message == "Reactivate the customer first."
    assert db_session.get(MeterReading, not_sent.reading.reading_id) is not None
    assert db_session.get(Bill, not_sent.bill.bill_id) is not None
    assert audit_rows(db_session, "reading.deleted") == []
    assert audit_rows(db_session, "reading.rejected")[0].details["reason"] == "Reactivate the customer first."


def test_a_correction_too_large_for_a_bill_changes_nothing(db_session, owner, mary, july, sms):
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)
    mary_bill = sent.bill
    mary_bill.rate_per_unit = Decimal("99999999.99")
    db_session.commit()

    with pytest.raises(BusinessRuleError):
        readings.correct_reading(db_session, owner, sent.reading.reading_id, typed("99999999"))

    db_session.refresh(sent.reading)
    assert sent.reading.current_reading == Decimal("135.00")
    assert audit_rows(db_session, "reading.updated") == []
    assert "too large" in audit_rows(db_session, "reading.rejected")[0].details["reason"]


def test_an_unknown_reading_is_not_found(db_session, owner):
    with pytest.raises(NotFoundError):
        readings.correct_reading(db_session, owner, 999999, typed("140"))


# ---- deleting a reading ----

def test_a_reading_with_no_bill_can_be_deleted(db_session, owner, mary, july, sms):
    old = reading_without_a_bill(db_session, owner, mary, july)
    old_id = old.reading_id

    deleted = readings.delete_reading(db_session, owner, old_id)

    assert (deleted.customer.customer_id, deleted.billing_period) == (mary.customer_id, "2025-08")
    assert db_session.get(MeterReading, old_id) is None
    assert audit_rows(db_session, "reading.deleted")[0].details["case"] == "no bill"


def test_a_reading_whose_bill_was_not_sent_is_deleted_with_its_bill(db_session, owner, mary, july, sms):
    old = reading_with_a_failed_sms(db_session, owner, mary, july, sms)
    bill_id = old.bill.bill_id

    readings.delete_reading(db_session, owner, old.reading.reading_id)
    db_session.expire_all()

    assert db_session.get(Bill, bill_id) is None
    assert [reading.billing_period for reading in readings_of(db_session, mary)] == ["2025-07"]
    # COR-4
    kept = db_session.scalars(select(MessageLog).where(MessageLog.customer_id == mary.customer_id,
                                                       MessageLog.bill_id.is_(None))).all()
    assert len(kept) == 1
    assert audit_rows(db_session, "bill.deleted")[0].entity_id == bill_id


def test_after_a_delete_the_month_can_be_entered_again(db_session, owner, mary, july, sms):
    old = reading_with_a_failed_sms(db_session, owner, mary, july, sms)
    readings.delete_reading(db_session, owner, old.reading.reading_id)

    again = readings.create_reading(db_session, owner, mary.customer_id, typed("138"))

    assert (again.reading.billing_period, again.reading.previous_reading) == ("2025-08", Decimal("120.00"))


def test_a_reading_whose_sms_was_sent_is_never_deleted(db_session, owner, mary, july, sms):
    # COR-1
    sent = reading_with_a_sent_sms(db_session, owner, mary, july, sms)

    with pytest.raises(BusinessRuleError) as raised:
        readings.delete_reading(db_session, owner, sent.reading.reading_id)

    assert "kept for the record" in raised.value.message
    assert db_session.get(MeterReading, sent.reading.reading_id) is not None
    assert db_session.get(Bill, sent.bill.bill_id) is not None
    assert audit_rows(db_session, "reading.deleted") == []
    assert audit_rows(db_session, "reading.rejected")[0].entity_id == sent.reading.reading_id


def test_a_reading_whose_bill_has_a_payment_is_never_deleted(db_session, owner, mary, july, sms):
    # COR-1
    not_sent = reading_with_a_failed_sms(db_session, owner, mary, july, sms)
    pay(db_session, owner, not_sent.bill, "500")

    with pytest.raises(BusinessRuleError):
        readings.delete_reading(db_session, owner, not_sent.reading.reading_id)

    assert db_session.get(MeterReading, not_sent.reading.reading_id) is not None


def test_only_the_latest_reading_can_be_deleted(db_session, owner, mary, july, sms):
    reading_without_a_bill(db_session, owner, mary, july)

    with pytest.raises(BusinessRuleError) as raised:
        readings.delete_reading(db_session, owner, july.reading_id)

    assert raised.value.message == "Only the latest reading can be corrected."
    assert len(readings_of(db_session, mary)) == 2



# ---- correcting a customer's first reading (CUS-5) ----

def first_reading(db_session, owner, mary, sms, previous="3400", opening="1200", fail=False):
    sms.failure = "Could not reach Africa's Talking (ConnectError)" if fail else None
    result = readings.create_reading(db_session, owner, mary.customer_id,
                                     {"current_reading": "3420", "reading_date": "2025-07-30",
                                      "previous_reading": previous, "opening_balance": opening})
    sms.failure = None
    return result


def test_a_sent_first_reading_can_have_its_previous_reading_and_opening_balance_corrected(db_session, owner, mary,
                                                                                          sms):
    sent = first_reading(db_session, owner, mary, sms)

    result = readings.correct_reading(db_session, owner, sent.reading.reading_id,
                                      {"current_reading": "3420", "previous_reading": "3405", "opening_balance": "1000"})

    assert result.correction_case == readings.BILL_SENT_OR_PAID
    assert (result.reading.previous_reading, result.reading.units_consumed) == (Decimal("3405.00"), Decimal("15.00"))
    # 15 x 130 + 150 + the corrected 1,000 opening balance
    assert (result.bill.previous_balance, result.bill.total_due) == (Decimal("1000.00"), Decimal("3100.00"))
    assert "Previous Reading:3405\n" in sms.sent[-1][1] and "Previous Balance: Ksh 1,000\n" in sms.sent[-1][1]
    details = audit_rows(db_session, "reading.updated")[0].details
    assert (details["old_previous"], details["new_previous"]) == ("3400.00", "3405.00")


def test_a_first_reading_not_sent_keeps_its_opening_balance_when_entered_again(db_session, owner, mary, sms):
    not_sent = first_reading(db_session, owner, mary, sms, fail=True)

    result = readings.correct_reading(db_session, owner, not_sent.reading.reading_id, {"current_reading": "3430"})

    assert result.correction_case == readings.BILL_NOT_SENT
    # empty boxes keep the saved values
    assert result.reading.previous_reading == Decimal("3400.00")
    assert result.bill.previous_balance == Decimal("1200.00")
    assert result.bill.total_due == Decimal("30") * Decimal("130") + Decimal("150") + Decimal("1200")


def test_a_later_reading_never_changes_its_previous_reading(db_session, owner, mary, sms):
    first_reading(db_session, owner, mary, sms)
    august = readings.create_reading(db_session, owner, mary.customer_id,
                                     {"current_reading": "3440", "reading_date": "2025-08-30"})

    result = readings.correct_reading(db_session, owner, august.reading.reading_id,
                                      {"current_reading": "3445", "previous_reading": "0", "opening_balance": "99"})

    assert result.reading.previous_reading == Decimal("3420.00")
    assert result.bill.previous_balance == august.bill.previous_balance


def test_a_first_reading_correction_with_the_current_below_the_typed_previous_is_refused(db_session, owner, mary,
                                                                                        sms):
    sent = first_reading(db_session, owner, mary, sms)

    with pytest.raises(BusinessRuleError) as raised:
        readings.correct_reading(db_session, owner, sent.reading.reading_id,
                                 {"current_reading": "3420", "previous_reading": "3500"})

    assert raised.value.message == "This is lower than the previous reading (3500). Please check both numbers."


def test_changing_only_the_opening_balance_is_a_correction(db_session, owner, mary, sms):
    sent = first_reading(db_session, owner, mary, sms)

    result = readings.correct_reading(db_session, owner, sent.reading.reading_id,
                                      {"current_reading": "3420", "opening_balance": "0"})

    assert result.bill.previous_balance == Decimal("0.00")
    assert "Previous Balance" not in sms.sent[-1][1]
