"""readings/service.py: entering a reading (TASK-044, TASK-045, RD-1 to RD-7, DB-7). The SMS
provider is the stand-in from conftest.py. Corrections are in test_corrections.py."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import BusinessRuleError, ConflictError, InternalError, NotFoundError
from app.core.logging import EAT
from app.features.readings import service as readings
from app.models import AuditLog, Bill, MessageLog, MeterReading


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)
                              .order_by(AuditLog.audit_id)).all()


def all_readings(db_session, customer):
    return db_session.scalars(select(MeterReading).where(MeterReading.customer_id == customer.customer_id)
                              .order_by(MeterReading.billing_period)).all()


def typed(current, reading_date="2025-08-30"):
    return {"current_reading": current, "reading_date": reading_date}


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel")


@pytest.fixture
def member(make_user):
    return make_user(role="member", first_name="Peter")


@pytest.fixture
def mary(make_customer):
    return make_customer(first_name="Mary")


@pytest.fixture
def mary_read_in_july(db_session, owner, mary, sms):
    """Mary's July reading of 120, with its bill of Ksh 15,750 unpaid."""
    return readings.create_reading(db_session, owner, mary.customer_id, typed("120", "2025-07-30"))


# ---- a reading that is saved ----

def test_a_reading_is_saved_with_the_previous_reading_supplied(db_session, member, mary, mary_read_in_july, sms):
    result = readings.create_reading(db_session, member, mary.customer_id, typed("135", "2025-08-30"))

    reading = result.reading
    # RD-2: last period's current reading, never typed
    assert (reading.previous_reading, reading.current_reading) == (Decimal("120.00"), Decimal("135.00"))
    assert reading.units_consumed == Decimal("15.00")
    # RD-5: the period comes from the date
    assert (reading.reading_date, reading.billing_period) == (date(2025, 8, 30), "2025-08")
    # RD-7: who entered it
    assert reading.recorded_by == member.user_id


def test_the_first_reading_starts_from_zero(db_session, owner, mary, sms):
    # CUS-5
    result = readings.create_reading(db_session, owner, mary.customer_id, typed("12"))

    assert result.reading.previous_reading == Decimal("0.00")
    assert result.reading.units_consumed == Decimal("12.00")
    assert result.bill.total_due == Decimal("1710.00")


def test_the_reading_date_defaults_to_today(db_session, owner, mary, sms):
    today = datetime.now(EAT).date()

    result = readings.create_reading(db_session, owner, mary.customer_id, typed("12", reading_date=""))

    assert result.reading.reading_date == today
    assert result.reading.billing_period == today.strftime("%Y-%m")


def test_a_reading_can_have_decimals(db_session, owner, mary, sms):
    result = readings.create_reading(db_session, owner, mary.customer_id, typed("12.5"))

    assert result.reading.current_reading == Decimal("12.50")
    assert result.bill.consumption_charge == Decimal("1625.00")


def test_reading_created_is_recorded_with_the_figures(db_session, member, mary, mary_read_in_july, sms):
    result = readings.create_reading(db_session, member, mary.customer_id, typed("135"))

    row = audit_rows(db_session, "reading.created")[-1]
    assert (row.level, row.entity_type, row.entity_id) == ("info", "reading", result.reading.reading_id)
    assert (row.customer_id, row.actor_user_id) == (mary.customer_id, member.user_id)
    assert row.details == {"period": "2025-08", "previous": "120.00", "current": "135.00", "units": "15.00",
                           "recorded_by": member.user_id}
    assert row.summary == f"Reading 135 entered for {mary.meter_number}"


# ---- one save does the whole flow (TASK-045, Flow A) ----

def test_one_save_makes_the_reading_the_bill_and_a_logged_sms(db_session, owner, mary, mary_read_in_july, sms):
    result = readings.create_reading(db_session, owner, mary.customer_id, typed("135"))

    bill = db_session.scalars(select(Bill).where(Bill.reading_id == result.reading.reading_id)).one()
    assert bill.bill_id == result.bill.bill_id
    # 15 x 130 = 1,950, plus 150, plus July's 15,750 unpaid
    assert bill.previous_balance == Decimal("15750.00")
    assert bill.total_due == Decimal("17850.00")
    message = db_session.scalars(select(MessageLog).where(MessageLog.bill_id == bill.bill_id)).one()
    assert (message.status, message.message_type) == ("sent", "bill")
    assert "Previous Balance: Ksh 15,750\nAmount Due: Ksh 17,850\n" in message.content
    assert result.sms_status == "sent"
    assert sms.sent[-1] == (mary.phone, message.content)
    events = [row.event_type for row in db_session.scalars(
        select(AuditLog).where(AuditLog.customer_id == mary.customer_id).order_by(AuditLog.audit_id))]
    assert events[-3:] == ["reading.created", "bill.generated", "sms.sent"]


def test_a_failed_sms_leaves_the_reading_and_the_bill_saved(db_session, owner, mary, sms):
    sms.failure = "Africa's Talking did not send it: InsufficientBalance"

    result = readings.create_reading(db_session, owner, mary.customer_id, typed("12"))

    assert result.sms_status == "failed"
    db_session.expire_all()
    assert db_session.get(MeterReading, result.reading.reading_id) is not None
    assert db_session.get(Bill, result.bill.bill_id) is not None
    message = db_session.scalars(select(MessageLog).where(MessageLog.bill_id == result.bill.bill_id)).one()
    assert message.status == "failed"
    assert audit_rows(db_session, "sms.failed")[0].details["error"] == (
        "Africa's Talking did not send it: InsufficientBalance")


def test_a_crash_in_the_provider_code_still_leaves_the_reading_the_bill_and_a_failed_message(
        db_session, owner, mary, monkeypatch):
    # SMS-4a: the user is told the SMS failed, never shown a 500 page
    def broken(settings, phone, text):
        raise RuntimeError("a bug in the provider code")

    monkeypatch.setattr("app.features.messaging.providers.send_sms", broken)

    result = readings.create_reading(db_session, owner, mary.customer_id, typed("12"))

    assert result.sms_status == "failed"
    db_session.expire_all()
    assert db_session.get(MeterReading, result.reading.reading_id) is not None
    assert db_session.get(Bill, result.bill.bill_id) is not None
    message = db_session.scalars(select(MessageLog).where(MessageLog.bill_id == result.bill.bill_id)).one()
    assert message.status == "failed"
    assert audit_rows(db_session, "system.error")[0].details["error_type"] == "RuntimeError"


def test_the_reading_and_the_bill_are_saved_together_or_not_at_all(db_session, owner, mary, sms, monkeypatch):
    # DB-7: the bill fails, so the reading is not saved either
    def broken(db, actor, reading):
        raise RuntimeError("the bill could not be made")

    monkeypatch.setattr("app.features.bills.service.generate_bill", broken)

    with pytest.raises(InternalError):
        readings.create_reading(db_session, owner, mary.customer_id, typed("12"))

    assert all_readings(db_session, mary) == []
    assert audit_rows(db_session, "reading.created") == []
    assert sms.sent == []


# ---- readings that are refused (RD-3, RD-4) ----

def test_a_reading_lower_than_last_month_is_refused_beside_the_field(db_session, owner, mary, mary_read_in_july, sms):
    with pytest.raises(BusinessRuleError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, typed("119"))

    assert raised.value.status_code == 422
    assert raised.value.field == "current_reading"
    assert raised.value.message == "This is lower than last month's reading (120). Please check the number."
    assert len(all_readings(db_session, mary)) == 1


def test_the_same_reading_as_last_month_is_zero_units_not_an_error(db_session, owner, mary, mary_read_in_july, sms):
    result = readings.create_reading(db_session, owner, mary.customer_id, typed("120"))

    assert result.reading.units_consumed == Decimal("0.00")


def test_a_second_reading_for_the_month_is_refused_as_a_duplicate(db_session, owner, mary, mary_read_in_july, sms):
    with pytest.raises(ConflictError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, typed("130", "2025-07-31"))

    assert raised.value.status_code == 409
    assert raised.value.message == ("This customer already has a reading for July 2025. "
                                    "To change it, use Fix on it under Recent readings.")
    assert len(all_readings(db_session, mary)) == 1


def test_the_database_refuses_a_duplicate_the_service_check_missed(db_session, owner, mary, mary_read_in_july, sms,
                                                                    monkeypatch):
    # RD-4: two saves at the same moment both pass the friendly check; the unique constraint stops one
    monkeypatch.setattr("app.features.readings.service.latest_reading", lambda db, customer_id: None)

    with pytest.raises(ConflictError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, typed("130", "2025-07-31"))

    assert "already has a reading for July 2025" in raised.value.message
    assert audit_rows(db_session, "reading.rejected")[-1].details["reason"] == raised.value.message
    assert len(all_readings(db_session, mary)) == 1


def test_a_reading_for_a_month_before_the_latest_is_refused(db_session, owner, mary, mary_read_in_july, sms):
    with pytest.raises(BusinessRuleError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, typed("150", "2025-06-30"))

    assert raised.value.field == "reading_date"
    assert raised.value.message == ("This customer's last reading is for July 2025. "
                                    "A new reading must be for a later month.")


def test_a_reading_dated_in_the_future_is_refused(db_session, owner, mary, sms):
    tomorrow = (datetime.now(EAT).date() + timedelta(days=1)).isoformat()

    with pytest.raises(BusinessRuleError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, typed("12", tomorrow))

    assert raised.value.field == "reading_date"
    assert "cannot be in the future" in raised.value.message


@pytest.mark.parametrize("bad_date", ["30/08/2025", "2025-02-30", "yesterday"])
def test_a_date_that_is_not_a_real_date_is_refused(db_session, owner, mary, sms, bad_date):
    with pytest.raises(BusinessRuleError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, typed("12", bad_date))

    assert raised.value.field == "reading_date"
    assert raised.value.message == "Please enter the reading date, for example 2026-08-30."


@pytest.mark.parametrize("bad,message", [
    ("", "Please enter the reading, for example 224."),
    ("two hundred", "Please enter the reading as a number, for example 224."),
    ("-5", "The reading cannot be less than 0."),
    ("12.345", "Use at most 2 decimal places for the reading."),
    ("100000000", "The reading is too large. Please check the number."),
])
def test_a_reading_that_is_not_a_sensible_number_is_refused(db_session, owner, mary, sms, bad, message):
    with pytest.raises(BusinessRuleError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, typed(bad))

    assert raised.value.field == "current_reading"
    assert raised.value.message == message
    assert all_readings(db_session, mary) == []


def test_an_inactive_customer_gets_no_new_reading(db_session, owner, make_customer, sms):
    # CUS-4
    gone = make_customer(status="inactive")

    with pytest.raises(BusinessRuleError) as raised:
        readings.create_reading(db_session, owner, gone.customer_id, typed("12"))

    assert raised.value.message == "This customer is inactive, so no bill can be made. Reactivate the customer first."
    assert all_readings(db_session, gone) == []
    assert sms.sent == []


def test_an_unknown_customer_is_not_found(db_session, owner, sms):
    with pytest.raises(NotFoundError):
        readings.create_reading(db_session, owner, 999999, typed("12"))


def test_a_refused_reading_is_recorded_with_the_reason_and_what_was_typed(db_session, member, mary, mary_read_in_july,
                                                                          sms):
    with pytest.raises(BusinessRuleError):
        readings.create_reading(db_session, member, mary.customer_id, typed("119", "2025-08-30"))

    row = audit_rows(db_session, "reading.rejected")[0]
    assert (row.level, row.customer_id, row.actor_user_id) == ("warning", mary.customer_id, member.user_id)
    assert row.details == {"reason": "This is lower than last month's reading (120). Please check the number.",
                           "current_reading": "119", "reading_date": "2025-08-30"}
    # rule 7: the customer is named by meter number only
    assert "Mary" not in row.summary and mary.meter_number in row.summary


def test_what_was_typed_is_cut_short_in_the_audit_row(db_session, owner, mary, sms):
    with pytest.raises(BusinessRuleError):
        readings.create_reading(db_session, owner, mary.customer_id, typed("x" * 500))

    assert audit_rows(db_session, "reading.rejected")[0].details["current_reading"] == "x" * 30


# ---- the live "units used" line ----

def test_the_preview_gives_the_units_a_reading_would_bill(db_session, owner, mary, mary_read_in_july):
    assert readings.units_preview(db_session, mary.customer_id, "135") == Decimal("15.00")


def test_the_preview_refuses_a_lower_reading_and_records_nothing(db_session, owner, mary, mary_read_in_july):
    with pytest.raises(BusinessRuleError) as raised:
        readings.units_preview(db_session, mary.customer_id, "100")

    assert raised.value.message == "This is lower than last month's reading (120). Please check the number."
    assert audit_rows(db_session, "reading.rejected") == []


# ---- what the readings screen shows ----

def test_the_picker_lists_customers_not_read_this_month_first(db_session, owner, make_customer, sms):
    alice = make_customer(first_name="Alice")
    bob = make_customer(first_name="Bob")
    make_customer(first_name="Carol", status="inactive")
    readings.create_reading(db_session, owner, alice.customer_id, typed("12", reading_date=""))

    rows = readings.customers_to_read(db_session)

    # Bob is not read yet, so he comes before Alice; Carol is inactive and not listed
    assert [(row.customer.first_name, row.read_this_month) for row in rows] == [("Bob", False), ("Alice", True)]
    assert readings.reading_progress(db_session) == (1, 2)
    assert readings.next_customer_to_read(db_session).customer_id == bob.customer_id


def test_a_reading_for_last_month_does_not_count_as_read_this_month(db_session, owner, mary, mary_read_in_july):
    assert readings.customers_to_read(db_session)[0].read_this_month is False


def test_there_is_no_next_customer_once_everyone_is_read(db_session, owner, mary, sms):
    readings.create_reading(db_session, owner, mary.customer_id, typed("12", reading_date=""))

    assert readings.next_customer_to_read(db_session) is None


@pytest.mark.parametrize("search", ["Gra", "mtr-0002", "Muthaiga"])
def test_the_picker_searches_by_name_meter_number_or_location(db_session, make_customer, search):
    make_customer(first_name="Mary")
    grace = make_customer(first_name="Grace")
    grace.location = "Muthaiga Rd"
    db_session.flush()

    assert [row.customer.first_name for row in readings.customers_to_read(db_session, search)] == ["Grace"]


def test_an_unreasonable_search_is_refused(db_session):
    with pytest.raises(BusinessRuleError) as raised:
        readings.customers_to_read(db_session, "x" * 101)

    assert raised.value.field == "search"


def test_the_entry_shows_the_previous_reading_and_any_reading_this_month(db_session, owner, mary, mary_read_in_july,
                                                                         make_customer, sms):
    entry = readings.entry_details(db_session, mary.customer_id)
    assert (entry.previous, entry.this_month) == (Decimal("120.00"), None)

    # CUS-5: a new customer starts from 0
    assert readings.entry_details(db_session, make_customer().customer_id).previous == Decimal("0.00")

    today = readings.create_reading(db_session, owner, mary.customer_id, typed("130", reading_date=""))
    assert readings.entry_details(db_session, mary.customer_id).this_month.reading_id == today.reading.reading_id


def test_the_entry_for_an_unknown_customer_is_not_found(db_session):
    with pytest.raises(NotFoundError):
        readings.entry_details(db_session, 999999)


def test_recent_readings_are_newest_first_and_only_the_latest_can_be_fixed(db_session, owner, mary, mary_read_in_july,
                                                                           make_customer, sms):
    august = readings.create_reading(db_session, owner, mary.customer_id, typed("135"))
    grace = make_customer(first_name="Grace")
    first = readings.create_reading(db_session, owner, grace.customer_id, typed("8"))

    recent = readings.recent_readings(db_session)

    assert [(row.customer.first_name, row.reading.current_reading, row.is_latest) for row in recent] == [
        ("Grace", Decimal("8.00"), True), ("Mary", Decimal("135.00"), True), ("Mary", Decimal("120.00"), False)]
    assert recent[0].reading.reading_id == first.reading.reading_id
    assert recent[1].reading.reading_id == august.reading.reading_id


def test_the_fix_details_say_which_case_will_run(db_session, owner, mary, mary_read_in_july, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    august = readings.create_reading(db_session, owner, mary.customer_id, typed("135"))

    fix = readings.fix_details(db_session, august.reading.reading_id)
    assert (fix.case, fix.is_latest, fix.sms_was_sent) == (readings.BILL_NOT_SENT, True, False)
    assert fix.bill.bill_id == august.bill.bill_id

    july = readings.fix_details(db_session, mary_read_in_july.reading.reading_id)
    assert (july.case, july.is_latest, july.sms_was_sent) == (readings.BILL_SENT_OR_PAID, False, True)


# ---- a customer's first reading starts from the paper records (CUS-5) ----

def first(current, previous="", opening="", reading_date="2025-07-30"):
    return {"current_reading": current, "reading_date": reading_date, "previous_reading": previous,
            "opening_balance": opening}


def test_a_first_reading_takes_the_typed_previous_reading_and_opening_balance(db_session, owner, mary, sms):
    result = readings.create_reading(db_session, owner, mary.customer_id, first("3420", "3400", "1,200"))

    assert (result.reading.previous_reading, result.reading.units_consumed) == (Decimal("3400.00"), Decimal("20.00"))
    # 20 x 130 + 150 + 1,200 owed on paper
    assert (result.bill.previous_balance, result.bill.total_due) == (Decimal("1200.00"), Decimal("3950.00"))
    text = sms.sent[0][1]
    assert "Previous Reading:3400\nCurrent Reading:3420\n" in text
    assert "Previous Balance: Ksh 1,200\nAmount Due: Ksh 3,950\n" in text
    assert audit_rows(db_session, "bill.generated")[0].details["previous_balance"] == "1200.00"


def test_empty_boxes_on_a_first_reading_mean_zero(db_session, owner, mary, sms):
    result = readings.create_reading(db_session, owner, mary.customer_id, first("12"))

    assert result.reading.previous_reading == Decimal("0.00")
    assert result.bill.previous_balance == Decimal("0.00")


def test_the_opening_balance_carries_only_into_the_first_bill(db_session, owner, mary, sms):
    readings.create_reading(db_session, owner, mary.customer_id, first("3420", "3400", "1200"))

    august = readings.create_reading(db_session, owner, mary.customer_id,
                                     first("3430", "0", "5000", reading_date="2025-08-30"))

    # RD-2: the typed boxes are ignored after the first reading; BILL-4 carries July's unpaid 3,950
    assert august.reading.previous_reading == Decimal("3420.00")
    assert august.bill.previous_balance == Decimal("3950.00")


def test_a_first_reading_below_the_typed_previous_is_refused(db_session, owner, mary, sms):
    with pytest.raises(BusinessRuleError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, first("3390", "3400"))

    assert raised.value.field == "current_reading"
    assert raised.value.message == "This is lower than the previous reading (3400). Please check both numbers."
    assert all_readings(db_session, mary) == []


@pytest.mark.parametrize("previous,opening,field,message", [
    ("abc", "", "previous_reading", "Please enter the previous reading as a number, for example 200."),
    ("-1", "", "previous_reading", "The previous reading cannot be less than 0."),
    ("", "lots", "opening_balance", "Please enter the opening balance as a number, for example 500."),
    ("", "-50", "opening_balance", "The opening balance cannot be less than 0."),
    ("", "10.555", "opening_balance", "Use at most 2 decimal places for the opening balance."),
])
def test_a_bad_previous_reading_or_opening_balance_is_refused(db_session, owner, mary, sms, previous, opening, field,
                                                             message):
    with pytest.raises(BusinessRuleError) as raised:
        readings.create_reading(db_session, owner, mary.customer_id, first("12", previous, opening))

    assert (raised.value.field, raised.value.message) == (field, message)
    assert audit_rows(db_session, "reading.rejected")[0].details["reason"] == message


def test_the_preview_uses_the_typed_previous_reading_on_a_first_reading(db_session, mary):
    assert readings.units_preview(db_session, mary.customer_id, "3420", "3400") == Decimal("20.00")
    assert readings.units_preview(db_session, mary.customer_id, "12", "") == Decimal("12.00")
    with pytest.raises(BusinessRuleError):
        readings.units_preview(db_session, mary.customer_id, "3390", "3400")


def test_the_preview_ignores_a_typed_previous_reading_after_the_first(db_session, owner, mary, mary_read_in_july):
    assert readings.units_preview(db_session, mary.customer_id, "135", "0") == Decimal("15.00")


def test_the_fix_details_say_whether_it_is_the_first_reading(db_session, owner, mary, sms):
    july = readings.create_reading(db_session, owner, mary.customer_id, first("3420", "3400"))
    august = readings.create_reading(db_session, owner, mary.customer_id, first("3430", reading_date="2025-08-30"))

    assert readings.fix_details(db_session, july.reading.reading_id).is_first is True
    assert readings.fix_details(db_session, august.reading.reading_id).is_first is False
