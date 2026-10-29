"""The phone method, SMS_BACKEND=phone (TASK-076, PRD SMS-14 to SMS-21, SECURITY.md SMS-S3
and SMS-S4).

A message is queued as ready, a user sends it from their own phone and marks it sent, and a
ready message whose bill is deleted becomes cancelled. No provider is ever called: the
`phone_method` fixture fails the test if Africa's Talking is reached.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, TooManyAttemptsError
from app.features.bills import service as bills
from app.features.messaging import service as messaging
from app.features.readings import service as readings
from app.models import AuditLog, Bill, MessageLog, MeterReading, Payment


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)
                              .order_by(AuditLog.audit_id)).all()


def messages_of(db_session, bill_id):
    return db_session.scalars(select(MessageLog).where(MessageLog.bill_id == bill_id)
                              .order_by(MessageLog.message_id)).all()


def typed(current, reading_date="2025-08-30"):
    return {"current_reading": current, "reading_date": reading_date}


def pay(db_session, owner, bill, amount):
    """A payment as payments/service.py records it."""
    db_session.add(Payment(bill_id=bill.bill_id, amount=Decimal(amount), payment_date=date(2025, 9, 2),
                           recorded_by=owner.user_id))
    bill.amount_paid += Decimal(amount)
    bill.status = bills.bill_status(bill.total_due, bill.amount_paid)
    db_session.commit()


def card_for(db_session, bill):
    """The Messages to send card for the bill, built as the screen builds it."""
    return next(card for card in bills.messages_to_send(db_session) if card.bill.bill_id == bill.bill_id)


def mark_card(db_session, user, card):
    """Mark as sent, with what the screen showed."""
    return bills.mark_sent(db_session, user, card.message.message_id, str(card.bill.total_due), card.fingerprint)


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
def july(db_session, owner, mary, phone_method):
    """Mary's July reading of 120. Its bill waits, ready to send."""
    return readings.create_reading(db_session, owner, mary.customer_id, typed("120", "2025-07-30"))


@pytest.fixture
def august(db_session, owner, mary, july):
    """Mary's August reading of 135. Its bill waits, ready to send."""
    return readings.create_reading(db_session, owner, mary.customer_id, typed("135"))


@pytest.fixture
def august_sent(db_session, owner, august):
    """The same, with its bill SMS sent from the phone and marked sent."""
    mark_card(db_session, owner, card_for(db_session, august.bill))
    return august


# ---- a new bill is queued, never sent (SMS-14) ----

def test_a_reading_saves_the_bill_and_one_ready_message_and_calls_no_provider(db_session, owner, mary,
                                                                              phone_method):
    result = readings.create_reading(db_session, owner, mary.customer_id, typed("120", "2025-07-30"))

    assert result.sms_status == "ready"
    assert db_session.get(MeterReading, result.reading.reading_id) is not None
    assert db_session.get(Bill, result.bill.bill_id) is not None
    [message] = messages_of(db_session, result.bill.bill_id)
    assert (message.message_type, message.status, message.sent_by_user_id) == ("bill", "ready", None)
    # SMS-19: the text of the moment is stored, so content is never empty
    assert message.content == bills.sms_text(db_session, result.bill, "bill")
    assert phone_method == []
    assert audit_rows(db_session, "sms.sent") == []


def test_queueing_is_recorded_as_sms_queued_without_the_text(db_session, owner, july):
    [message] = messages_of(db_session, july.bill.bill_id)

    [row] = audit_rows(db_session, "sms.queued")

    assert row.entity_type == "message" and row.entity_id == message.message_id
    assert row.customer_id == july.customer.customer_id
    assert row.details == {"message_id": message.message_id, "type": "bill", "bill_id": july.bill.bill_id}
    assert "Amount Due" not in row.summary


def test_when_queueing_fails_the_reading_and_bill_stay_saved_and_the_error_is_recorded(
        db_session, owner, mary, phone_method, monkeypatch, caplog):
    def database_down(*args):
        raise OperationalError("INSERT INTO messages_log", {}, Exception("gone away"))

    monkeypatch.setattr(messaging, "_write_ready_row", database_down)

    result = readings.create_reading(db_session, owner, mary.customer_id, typed("120", "2025-07-30"))

    assert result.sms_status == "not_queued"
    assert db_session.get(MeterReading, result.reading.reading_id) is not None
    assert db_session.get(Bill, result.bill.bill_id) is not None
    assert messages_of(db_session, result.bill.bill_id) == []
    [error] = audit_rows(db_session, "system.error")
    assert error.details == {"error_type": "OperationalError", "action": "queue the SMS",
                             "bill_id": result.bill.bill_id}
    assert "could not be queued" in caplog.text


def test_a_failure_to_queue_carries_a_plain_message():
    error = messaging.MessageNotQueuedError()

    assert error.status_code == 500
    assert error.message == ("The reading and bill are saved, but the SMS could not be prepared. "
                             "Open the bill and press Send again.")


def test_a_message_already_waiting_is_returned_and_nothing_new_is_written(db_session, owner, july):
    [waiting] = messages_of(db_session, july.bill.bill_id)

    again = messaging.queue_message(db_session, owner, july.bill, "bill", "any text")

    assert again.message_id == waiting.message_id
    assert len(messages_of(db_session, july.bill.bill_id)) == 1
    assert len(audit_rows(db_session, "sms.queued")) == 1


# ---- the daily limit counts ready rows (SMS-21, SMS-S3) ----

def test_ready_rows_count_towards_the_daily_limit(db_session, owner, mary, july):
    # one ready bill message already, and four more ready corrections today: five in all
    for _ in range(4):
        db_session.add(MessageLog(bill_id=july.bill.bill_id, customer_id=mary.customer_id,
                                  message_type="correction", content="Hi Mary,", status="ready"))
    db_session.commit()
    ready_bill = messages_of(db_session, july.bill.bill_id)[0]
    ready_bill.status = "sent"
    db_session.commit()

    with pytest.raises(TooManyAttemptsError):
        messaging.queue_message(db_session, owner, july.bill, "bill", "Hi Mary,")

    assert len(messages_of(db_session, july.bill.bill_id)) == 5
    [blocked] = audit_rows(db_session, "sms.send_blocked")
    assert blocked.details == {"bill_id": july.bill.bill_id, "cap": 5}


def test_send_again_is_blocked_by_the_limit_counting_ready_rows(db_session, owner, mary, august_sent):
    bill = august_sent.bill
    db_session.add(MessageLog(bill_id=bill.bill_id, customer_id=mary.customer_id, message_type="correction",
                              content="Hi Mary,", status="ready"))
    for _ in range(3):
        db_session.add(MessageLog(bill_id=bill.bill_id, customer_id=mary.customer_id, message_type="bill",
                                  content="Hi Mary,", status="sent"))
    db_session.commit()

    with pytest.raises(TooManyAttemptsError):
        bills.resend_sms(db_session, owner, bill.bill_id)

    assert len(audit_rows(db_session, "sms.send_blocked")) == 1


# ---- Mark as sent (SMS-17, SMS-S4) ----

def test_mark_as_sent_stores_who_when_and_the_exact_text_shown(db_session, member, august):
    card = card_for(db_session, august.bill)

    message = mark_card(db_session, member, card)

    assert message.status == "sent"
    assert message.sent_by_user_id == member.user_id
    assert message.content == card.text
    # the sent time is the moment the row changed to sent
    assert message.updated_at is not None
    [row] = audit_rows(db_session, "sms.marked_sent")
    assert row.actor_user_id == member.user_id
    assert row.details == {"message_id": message.message_id, "type": "bill", "bill_id": august.bill.bill_id,
                           "marked_by": member.user_id}
    assert "Amount Due" not in row.summary


def test_a_marked_message_leaves_the_list(db_session, owner, august):
    mark_card(db_session, owner, card_for(db_session, august.bill))

    waiting_bills = [card.bill.bill_id for card in bills.messages_to_send(db_session)]
    assert august.bill.bill_id not in waiting_bills


def test_a_second_press_is_refused_and_records_nothing_more(db_session, owner, august):
    card = card_for(db_session, august.bill)
    mark_card(db_session, owner, card)

    with pytest.raises(ConflictError) as raised:
        mark_card(db_session, owner, card)

    assert raised.value.message == "This message is no longer waiting to be sent. Refresh the page to see the list."
    assert len(audit_rows(db_session, "sms.marked_sent")) == 1
    [refused] = audit_rows(db_session, "sms.resend_refused")
    assert refused.details == {"bill_id": august.bill.bill_id, "message_id": card.message.message_id,
                               "reason": "message not ready"}


def test_marking_is_refused_for_an_inactive_customer(db_session, owner, mary, august):
    card = card_for(db_session, august.bill)
    mary.status = "inactive"
    db_session.commit()

    with pytest.raises(BusinessRuleError) as raised:
        mark_card(db_session, owner, card)

    assert raised.value.message == "This customer is inactive, so no SMS is sent. Reactivate the customer first."
    assert db_session.get(MessageLog, card.message.message_id).status == "ready"
    assert audit_rows(db_session, "sms.resend_refused")[0].details["reason"] == "customer inactive"


def test_marking_is_refused_when_the_total_changed(db_session, owner, august):
    # paid, so a correction edits the bill in place and its ready message stays
    pay(db_session, owner, august.bill, "500")
    card = card_for(db_session, august.bill)
    readings.correct_reading(db_session, owner, august.reading.reading_id, typed("140"))

    with pytest.raises(ConflictError) as raised:
        mark_card(db_session, owner, card)

    assert raised.value.message == "This bill changed. Open the message again."
    assert raised.value.status_code == 409
    assert audit_rows(db_session, "sms.resend_refused")[-1].details["reason"] == "bill changed"
    assert audit_rows(db_session, "sms.marked_sent") == []


def test_marking_is_refused_when_the_text_changed_but_the_total_did_not(db_session, owner, august_sent):
    # a correction waits; a payment then adds "Paid so far" and "Balance" to its text, but the
    # total due stays the same
    readings.correct_reading(db_session, owner, august_sent.reading.reading_id, typed("140"))
    bill = db_session.get(Bill, august_sent.bill.bill_id)
    shown = card_for(db_session, bill)
    assert shown.message.message_type == "correction"
    pay(db_session, owner, bill, "500")

    with pytest.raises(ConflictError) as raised:
        bills.mark_sent(db_session, owner, shown.message.message_id, str(bill.total_due), shown.fingerprint)

    assert raised.value.message == "This bill changed. Open the message again."
    assert db_session.get(MessageLog, shown.message.message_id).status == "ready"
    # the card shown again has the new text, and marking that one stores it
    fresh = bills.message_to_send(db_session, shown.message.message_id)
    assert "Paid so far: Ksh 500\n" in fresh.text
    message = mark_card(db_session, owner, fresh)
    assert message.content == fresh.text


def test_a_fingerprint_that_was_tampered_with_is_refused(db_session, owner, august):
    card = card_for(db_session, august.bill)

    with pytest.raises(ConflictError):
        bills.mark_sent(db_session, owner, card.message.message_id, str(card.bill.total_due), "0" * 64)

    assert db_session.get(MessageLog, card.message.message_id).status == "ready"


def test_marking_an_unknown_message_is_not_found(db_session, owner, phone_method):
    with pytest.raises(NotFoundError):
        bills.mark_sent(db_session, owner, 999999, "0", "x")


# ---- Send again with the phone method (SMS-18) ----

def test_send_again_on_a_sent_message_writes_a_new_ready_row(db_session, owner, august_sent):
    bill = august_sent.bill

    message = bills.resend_sms(db_session, owner, bill.bill_id)

    assert (message.status, message.message_type) == ("ready", "bill")
    assert [row.status for row in messages_of(db_session, bill.bill_id)] == ["sent", "ready"]
    assert message.content == bills.sms_text(db_session, bill, "bill")
    assert len(audit_rows(db_session, "sms.resend_requested")) == 1
    assert len(audit_rows(db_session, "sms.queued")) == 3


def test_send_again_is_refused_while_a_message_waits(db_session, owner, august):
    with pytest.raises(ConflictError) as raised:
        bills.resend_sms(db_session, owner, august.bill.bill_id)

    assert raised.value.message == "A message is already waiting to be sent."
    assert len(messages_of(db_session, august.bill.bill_id)) == 1
    [refused] = audit_rows(db_session, "sms.resend_refused")
    assert refused.details["reason"] == "message already waiting"


def test_send_again_is_refused_for_an_inactive_customer(db_session, owner, mary, august_sent):
    mary.status = "inactive"
    db_session.commit()

    with pytest.raises(BusinessRuleError):
        bills.resend_sms(db_session, owner, august_sent.bill.bill_id)

    assert len(messages_of(db_session, august_sent.bill.bill_id)) == 1
    assert audit_rows(db_session, "sms.resend_refused")[0].details["reason"] == "customer inactive"


def test_send_again_prepares_a_bill_that_has_no_message_yet(db_session, owner, august):
    # the daily limit or a failure to queue can leave a bill with no message at all
    db_session.delete(messages_of(db_session, august.bill.bill_id)[0])
    db_session.commit()

    message = bills.resend_sms(db_session, owner, august.bill.bill_id)

    assert (message.status, message.message_type) == ("ready", "bill")


# ---- corrections (SMS-14, SMS-19) ----

def test_correcting_a_sent_bill_queues_a_ready_correction(db_session, owner, august_sent):
    result = readings.correct_reading(db_session, owner, august_sent.reading.reading_id, typed("140"))

    assert result.correction_case == readings.BILL_SENT_OR_PAID
    assert result.sms_status == "ready"
    newest = messages_of(db_session, result.bill.bill_id)[-1]
    assert (newest.message_type, newest.status) == ("correction", "ready")
    assert "We corrected your meter reading.\n" in newest.content


def test_a_second_correction_does_not_add_another_ready_correction(db_session, owner, august_sent):
    readings.correct_reading(db_session, owner, august_sent.reading.reading_id, typed("140"))

    result = readings.correct_reading(db_session, owner, august_sent.reading.reading_id, typed("142"))

    assert result.sms_status == "ready"
    corrections = [row for row in messages_of(db_session, result.bill.bill_id) if row.message_type == "correction"]
    assert len(corrections) == 1
    # SMS-19: the text shown is built from the bill as it is now
    assert "Current Reading:142\n" in card_for(db_session, result.bill).text


def test_a_paid_bill_with_only_a_ready_message_gets_no_new_row_and_shows_the_new_figures(db_session, owner,
                                                                                         august):
    pay(db_session, owner, august.bill, "500")

    result = readings.correct_reading(db_session, owner, august.reading.reading_id, typed("140"))

    assert result.correction_case == readings.BILL_SENT_OR_PAID
    assert result.sms_status == "ready"
    [message] = messages_of(db_session, august.bill.bill_id)
    assert (message.message_type, message.status) == ("bill", "ready")
    card = card_for(db_session, result.bill)
    assert "Current Reading:140\n" in card.text
    assert card.text.endswith("Payments via Mpesa: 0722995388")


def test_a_paid_bill_whose_sms_never_went_queues_a_ready_bill_message(db_session, owner, august):
    # its ready message was cancelled by hand on the server, so none waits and none was sent
    messages_of(db_session, august.bill.bill_id)[0].status = "cancelled"
    pay(db_session, owner, august.bill, "500")

    result = readings.correct_reading(db_session, owner, august.reading.reading_id, typed("140"))

    newest = messages_of(db_session, august.bill.bill_id)[-1]
    assert result.sms_status == "ready"
    assert (newest.message_type, newest.status) == ("bill", "ready")
    assert "We corrected" not in newest.content


# ---- the three-case rule with a ready-only bill (SMS-20, COR-4) ----

def test_a_ready_only_bill_counts_as_not_sent(db_session, owner, august):
    assert bills.sms_was_sent(db_session, august.bill.bill_id) is False
    assert readings.correction_case(db_session, august.reading) == readings.BILL_NOT_SENT


def test_a_ready_only_bill_is_deleted_with_its_reading_and_its_message_is_kept_as_cancelled(db_session, owner, mary,
                                                                                           august):
    [message] = messages_of(db_session, august.bill.bill_id)
    message_id, bill_id = message.message_id, august.bill.bill_id

    readings.delete_reading(db_session, owner, august.reading.reading_id)

    assert db_session.get(Bill, bill_id) is None
    kept = db_session.get(MessageLog, message_id)
    assert (kept.status, kept.bill_id, kept.customer_id) == ("cancelled", None, mary.customer_id)
    [cancelled] = audit_rows(db_session, "sms.cancelled")
    assert cancelled.entity_id == message_id
    assert cancelled.details == {"message_id": message_id, "bill_id": bill_id}
    assert all(card.message.message_id != message_id for card in bills.messages_to_send(db_session))


def test_correcting_a_ready_only_bill_cancels_its_message_and_queues_the_new_bill(db_session, owner, august):
    [old] = messages_of(db_session, august.bill.bill_id)

    result = readings.correct_reading(db_session, owner, august.reading.reading_id, typed("140"))

    assert result.correction_case == readings.BILL_NOT_SENT
    assert db_session.get(MessageLog, old.message_id).status == "cancelled"
    [new] = messages_of(db_session, result.bill.bill_id)
    assert (new.message_type, new.status) == ("bill", "ready")


def test_a_bill_with_a_sent_message_cannot_be_deleted(db_session, owner, august_sent):
    assert readings.correction_case(db_session, august_sent.reading) == readings.BILL_SENT_OR_PAID

    with pytest.raises(BusinessRuleError):
        readings.delete_reading(db_session, owner, august_sent.reading.reading_id)

    assert db_session.get(Bill, august_sent.bill.bill_id) is not None


def test_marking_a_message_whose_bill_was_deleted_is_refused(db_session, owner, august):
    card = card_for(db_session, august.bill)
    readings.delete_reading(db_session, owner, august.reading.reading_id)

    with pytest.raises(ConflictError):
        bills.mark_sent(db_session, owner, card.message.message_id, str(card.bill.total_due), card.fingerprint)

    assert db_session.get(MessageLog, card.message.message_id).status == "cancelled"


# ---- the Messages to send list (SMS-15) ----

def test_the_list_holds_ready_messages_of_active_customers_oldest_first(db_session, owner, make_customer, mary,
                                                                        phone_method):
    first = readings.create_reading(db_session, owner, mary.customer_id, typed("120", "2025-07-30"))
    john = make_customer(first_name="John", last_name="Kamau")
    second = readings.create_reading(db_session, owner, john.customer_id, typed("50", "2025-07-30"))
    grace = make_customer(first_name="Grace", last_name="Njeri")
    readings.create_reading(db_session, owner, grace.customer_id, typed("70", "2025-07-30"))
    grace.status = "inactive"
    db_session.commit()

    cards = bills.messages_to_send(db_session)

    assert [card.bill.bill_id for card in cards] == [first.bill.bill_id, second.bill.bill_id]
    assert messaging.waiting_count(db_session) == 2
    assert cards[0].link.startswith("sms:+254722123456?&body=C%26C%20H2O%20WATER%20SUPPLIES%0AHi%20Mary%2C")
