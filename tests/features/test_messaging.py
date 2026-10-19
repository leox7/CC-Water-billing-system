"""messaging/service.py (TASK-041, SMS-3, SMS-4, SMS-S1 to SMS-S3). The provider is the
stand-in from conftest.py (the `sms` fixture), so nothing is really sent."""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import BusinessRuleError, ConflictError, TooManyAttemptsError
from app.core.logging import EAT
from app.features.messaging import service as messaging
from app.models import AuditLog, Bill, MessageLog, MeterReading


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


def messages(db_session, bill):
    return db_session.scalars(select(MessageLog).where(MessageLog.bill_id == bill.bill_id)
                              .order_by(MessageLog.message_id)).all()


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel")


@pytest.fixture
def mary(make_customer):
    return make_customer(first_name="Mary", phone="254722123456")


def add_bill(db_session, customer, user, period="2026-08"):
    reading = MeterReading(customer_id=customer.customer_id, billing_period=period, previous_reading=Decimal("120"),
                           current_reading=Decimal("135"), units_consumed=Decimal("15"),
                           reading_date=date(2026, 8, 30), recorded_by=user.user_id)
    db_session.add(reading)
    db_session.flush()
    bill = Bill(reading_id=reading.reading_id, customer_id=customer.customer_id, rate_per_unit=Decimal("130"),
                service_charge=Decimal("150"), units_consumed=Decimal("15"), consumption_charge=Decimal("1950"),
                previous_balance=Decimal("0"), total_due=Decimal("2100"), amount_paid=Decimal("0"), status="unpaid")
    db_session.add(bill)
    db_session.flush()
    return bill


def add_attempt(db_session, bill, status="failed", created_at=None, message_type="bill"):
    message = MessageLog(bill_id=bill.bill_id, customer_id=bill.customer_id, message_type=message_type,
                         content="Hi Mary,", status=status)
    if created_at:
        message.created_at = created_at
    db_session.add(message)
    db_session.flush()
    return message


@pytest.fixture
def bill(db_session, mary, owner):
    return add_bill(db_session, mary, owner)


# ---- pending, then sent or failed (SMS-3) ----

def test_a_message_is_logged_as_pending_before_the_provider_is_asked_then_marked_sent(
        db_session, owner, mary, bill, sms, monkeypatch):
    seen_while_sending = []

    def provider(settings, phone, text):
        seen_while_sending.extend((row.status for row in messages(db_session, bill)))
        return sms.send(settings, phone, text)

    monkeypatch.setattr("app.features.messaging.providers.send_sms", provider)

    message = messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,\nAmount Due: Ksh 2,100")

    assert seen_while_sending == ["pending"]
    assert message.status == "sent"
    assert message.provider_message_id == "ATPid_test_1"
    assert message.message_type == "bill"
    assert message.content == "Hi Mary,\nAmount Due: Ksh 2,100"
    # SMS-S1: to the phone stored on the bill's customer, nothing else
    assert sms.sent == [("254722123456", "Hi Mary,\nAmount Due: Ksh 2,100")]

    row = audit_rows(db_session, "sms.sent")[0]
    assert (row.level, row.entity_type, row.entity_id) == ("info", "message", message.message_id)
    assert row.customer_id == mary.customer_id
    assert row.details == {"message_id": message.message_id, "type": "bill", "provider_message_id": "ATPid_test_1"}


def test_a_failed_send_is_marked_failed_and_recorded_but_raises_nothing(db_session, owner, mary, bill, sms):
    sms.failure = "Africa's Talking balance is too low. Top up and press Send again."

    message = messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,")

    assert message.status == "failed"
    assert message.provider_message_id is None
    assert message.failure_reason == "Africa's Talking balance is too low. Top up and press Send again."
    # SMS-4: the attempt is kept, never dropped
    assert [row.status for row in messages(db_session, bill)] == ["failed"]
    row = audit_rows(db_session, "sms.failed")[0]
    assert (row.level, row.entity_id, row.customer_id) == ("warning", message.message_id, mary.customer_id)
    assert row.details == {"message_id": message.message_id, "type": "bill",
                           "error": "Africa's Talking balance is too low. Top up and press Send again."}
    assert audit_rows(db_session, "sms.sent") == []


@pytest.mark.parametrize("status_code,reason", [
    (405, "Africa's Talking balance is too low. Top up and press Send again."),
    (402, "The sender ID is not registered."),
    (403, "That phone number is not valid."),
    (407, "Africa's Talking could not send this message (code 407)."),
    (None, "Africa's Talking sent an answer that could not be read."),
])
def test_each_plain_reason_is_stored_on_the_message_and_in_the_audit_details(db_session, owner, bill, monkeypatch,
                                                                             status_code, reason):
    # the real Africa's Talking code, answered by a stand-in, so what is stored is what it gives
    import httpx

    from app.core.config import load_settings
    from app.features.messaging import providers

    api_key = "test-key-not-a-real-one"
    live = load_settings({"APP_ENV": "production", "DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1/water_billing",
                          "SESSION_SECRET": "x" * 48, "SMS_BACKEND": "africastalking", "AT_USERNAME": "ccwater",
                          "AT_API_KEY": api_key, "AT_SENDER_ID": "CCH2O"})
    recipients = [] if status_code is None else [{"statusCode": status_code, "number": "+254722123456",
                                                  "status": "SomethingTheProviderSaid"}]
    answer = httpx.Response(201, json={"SMSMessageData": {"Recipients": recipients}})
    client = httpx.Client(transport=httpx.MockTransport(lambda request: answer))
    monkeypatch.setattr("app.features.messaging.providers.send_sms",
                        lambda settings, phone, text: providers.send_with_africastalking(live, phone, text, client))

    message = messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,")

    db_session.expire_all()
    stored = db_session.get(MessageLog, message.message_id)
    assert (stored.status, stored.failure_reason) == ("failed", reason)
    assert audit_rows(db_session, "sms.failed")[0].details["error"] == reason
    for kept in (stored.failure_reason, str(audit_rows(db_session, "sms.failed")[0].details)):
        assert api_key not in kept and "SomethingTheProviderSaid" not in kept and "722123456" not in kept


def test_a_sent_message_has_no_failure_reason(db_session, owner, bill, sms):
    assert messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,").failure_reason is None


def test_a_correction_is_logged_as_its_own_type(db_session, owner, bill, sms):
    message = messaging.send_message(db_session, owner, bill, "correction", "We corrected your meter reading.")

    assert message.message_type == "correction"
    assert audit_rows(db_session, "sms.sent")[0].details["type"] == "correction"


def test_a_crash_in_the_provider_code_is_a_failed_send_not_an_error(db_session, owner, mary, bill, monkeypatch,
                                                                    caplog):
    # SMS-4a: the row is marked failed, never left pending, and nothing is hidden
    def broken(settings, phone, text):
        raise RuntimeError("a bug in the provider code")

    monkeypatch.setattr("app.features.messaging.providers.send_sms", broken)

    with caplog.at_level(logging.ERROR, logger="app.messaging"):
        message = messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,")

    assert message.status == "failed"
    assert [row.status for row in messages(db_session, bill)] == ["failed"]
    # the stack trace is in the application log
    logged = caplog.records[-1]
    assert logged.exc_info and logged.exc_info[0] is RuntimeError
    failed = audit_rows(db_session, "sms.failed")[0]
    assert failed.details == {"message_id": message.message_id, "type": "bill",
                              "error": "The app failed while sending (RuntimeError). See the server log."}
    assert message.failure_reason == "The app failed while sending (RuntimeError). See the server log."
    error = audit_rows(db_session, "system.error")[0]
    assert (error.level, error.entity_type, error.entity_id, error.customer_id) == (
        "error", "message", message.message_id, mary.customer_id)
    assert error.details == {"error_type": "RuntimeError", "action": "send the SMS", "message_id": message.message_id}
    # the exception's own text can hold anything, so it is not copied into the audit log
    assert "a bug in the provider code" not in str(error.details) + error.summary


def test_a_failed_sms_left_by_a_crash_can_be_sent_again(db_session, owner, bill, sms, monkeypatch):
    def broken(settings, phone, text):
        raise RuntimeError("a bug")

    monkeypatch.setattr("app.features.messaging.providers.send_sms", broken)
    messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,")
    monkeypatch.setattr("app.features.messaging.providers.send_sms", sms.send)

    assert messaging.resend_message(db_session, owner, bill).status == "sent"


# ---- send again (SMS-4) ----

def test_a_failed_sms_is_sent_again_as_a_new_attempt_with_the_same_text(db_session, owner, mary, bill, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    failed = messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,\nAmount Due: Ksh 2,100")
    sms.failure = None

    resent = messaging.resend_message(db_session, owner, bill)

    assert resent.message_id != failed.message_id
    assert [row.status for row in messages(db_session, bill)] == ["failed", "sent"]
    assert (resent.message_type, resent.content) == ("bill", "Hi Mary,\nAmount Due: Ksh 2,100")
    assert sms.sent == [("254722123456", "Hi Mary,\nAmount Due: Ksh 2,100")]
    row = audit_rows(db_session, "sms.resend_requested")[0]
    assert (row.level, row.entity_type, row.entity_id) == ("info", "message", failed.message_id)
    assert row.customer_id == mary.customer_id
    assert row.details == {"message_id": failed.message_id}


def test_a_failed_correction_is_sent_again_as_a_correction(db_session, owner, bill, sms):
    add_attempt(db_session, bill, status="sent", message_type="bill")
    add_attempt(db_session, bill, status="failed", message_type="correction")

    resent = messaging.resend_message(db_session, owner, bill)

    assert resent.message_type == "correction"


@pytest.mark.parametrize("latest_status", ["sent", "pending"])
def test_only_a_failed_sms_can_be_sent_again(db_session, owner, bill, sms, latest_status):
    add_attempt(db_session, bill, status="failed")
    add_attempt(db_session, bill, status=latest_status)

    with pytest.raises(ConflictError) as raised:
        messaging.resend_message(db_session, owner, bill)

    assert raised.value.status_code == 409
    assert "Only an SMS that failed can be sent again" in raised.value.message
    newest = messages(db_session, bill)[-1]
    assert len(messages(db_session, bill)) == 2
    assert sms.sent == []
    assert audit_rows(db_session, "sms.resend_requested") == []
    row = audit_rows(db_session, "sms.resend_refused")[0]
    assert (row.level, row.entity_type, row.entity_id) == ("warning", "bill", bill.bill_id)
    assert row.details == {"bill_id": bill.bill_id, "message_id": newest.message_id,
                           "reason": "last message did not fail"}


def test_a_bill_with_no_sms_has_nothing_to_send_again(db_session, owner, bill, sms):
    with pytest.raises(ConflictError):
        messaging.resend_message(db_session, owner, bill)

    assert audit_rows(db_session, "sms.resend_refused")[0].details == {
        "bill_id": bill.bill_id, "message_id": None, "reason": "last message did not fail"}


def test_an_inactive_customer_gets_no_sms_sent_again(db_session, owner, mary, bill, sms):
    # SMS-4, CUS-4
    failed = add_attempt(db_session, bill, status="failed")
    mary.status = "inactive"
    db_session.flush()

    with pytest.raises(BusinessRuleError) as raised:
        messaging.resend_message(db_session, owner, bill)

    assert raised.value.status_code == 422
    assert raised.value.message == "This customer is inactive, so no SMS is sent. Reactivate the customer first."
    assert sms.sent == []
    assert len(messages(db_session, bill)) == 1
    row = audit_rows(db_session, "sms.resend_refused")[0]
    assert (row.level, row.customer_id, row.actor_user_id) == ("warning", mary.customer_id, owner.user_id)
    assert row.details == {"bill_id": bill.bill_id, "message_id": failed.message_id, "reason": "customer inactive"}


# ---- the daily limit per bill (SMS-S3) ----

def test_the_sixth_attempt_on_a_bill_in_one_day_is_blocked_and_recorded(db_session, owner, mary, bill, sms):
    # every type and failed attempts count too
    for status, message_type in [("failed", "bill"), ("failed", "bill"), ("sent", "bill"),
                                 ("failed", "correction"), ("sent", "correction")]:
        add_attempt(db_session, bill, status=status, message_type=message_type)

    with pytest.raises(TooManyAttemptsError) as raised:
        messaging.send_message(db_session, owner, bill, "correction", "We corrected your meter reading.")

    assert raised.value.status_code == 429
    assert raised.value.message == "This bill has already had 5 SMS today. Please send it again tomorrow."
    assert len(messages(db_session, bill)) == 5
    assert sms.sent == []
    row = audit_rows(db_session, "sms.send_blocked")[0]
    assert (row.level, row.entity_type, row.entity_id) == ("warning", "bill", bill.bill_id)
    assert row.customer_id == mary.customer_id
    assert row.details == {"bill_id": bill.bill_id, "cap": 5}


def test_send_again_counts_towards_the_limit(db_session, owner, bill, sms):
    for _ in range(5):
        add_attempt(db_session, bill, status="failed")

    with pytest.raises(TooManyAttemptsError):
        messaging.resend_message(db_session, owner, bill)

    assert sms.sent == []


def test_the_fifth_attempt_is_still_allowed(db_session, owner, bill, sms):
    for _ in range(4):
        add_attempt(db_session, bill, status="failed")

    assert messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,").status == "sent"


def test_attempts_from_yesterday_do_not_count(db_session, owner, bill, sms):
    yesterday = (datetime.now(EAT) - timedelta(days=1)).replace(tzinfo=None)
    for _ in range(5):
        add_attempt(db_session, bill, status="failed", created_at=yesterday)

    assert messaging.send_message(db_session, owner, bill, "bill", "Hi Mary,").status == "sent"


def test_the_limit_is_per_bill(db_session, owner, mary, bill, sms):
    for _ in range(5):
        add_attempt(db_session, bill, status="failed")
    next_month = add_bill(db_session, mary, owner, period="2026-09")

    assert messaging.send_message(db_session, owner, next_month, "bill", "Hi Mary,").status == "sent"
