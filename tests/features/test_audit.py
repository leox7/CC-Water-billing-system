"""The audit writer (AUDIT_LOG.md section 8, the parts about the writer)."""

import logging
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.logging import client_ip_var, request_id_var
from app.features.audit import service as audit
from app.models import AuditLog, Customer, User


@pytest.fixture
def owner(db_session):
    user = User(first_name="Daniel", last_name="Kamau", phone="254722000001",
                password_hash="x", role="owner")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def customer(db_session):
    customer = Customer(first_name="Mary", last_name="Wanjiru", phone="254722000002",
                        location="Kiawara", meter_number="MTR-0032")
    db_session.add(customer)
    db_session.flush()
    return customer


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


def test_every_catalogue_event_has_a_valid_level():
    assert set(audit.EVENT_LEVELS.values()) <= {"info", "warning", "error"}
    assert len(audit.EVENT_LEVELS) == 39
    assert audit.EVENT_LEVELS["sms.resend_refused"] == "warning"
    # the phone method (AUDIT_LOG.md section 4)
    assert audit.EVENT_LEVELS["sms.queued"] == "info"
    assert audit.EVENT_LEVELS["sms.marked_sent"] == "info"
    assert audit.EVENT_LEVELS["sms.cancelled"] == "info"


def test_a_success_row_has_the_right_fields(db_session, owner, customer):
    audit.record_event(
        db_session, "customer.created", "Customer MTR-0032 added",
        actor=owner, entity=customer, customer_id=customer.customer_id,
        details={"meter_number": "MTR-0032", "rate": Decimal("130.00")},
    )

    row = audit_rows(db_session, "customer.created")[0]
    assert row.level == "info"
    assert row.summary == "Customer MTR-0032 added"
    assert row.actor_user_id == owner.user_id
    assert row.actor_label == "Daniel (owner)"
    assert row.entity_type == "customer"
    assert row.entity_id == customer.customer_id
    assert row.customer_id == customer.customer_id
    assert row.details == {"meter_number": "MTR-0032", "rate": "130.00"}
    assert row.created_at is not None


def test_a_member_label_reads_naturally(db_session):
    member = User(first_name="Peter", last_name="Otieno", phone="254722000009",
                  password_hash="x", role="member")
    db_session.add(member)
    db_session.flush()

    audit.record_event(db_session, "auth.logout", "Peter signed out", actor=member)

    assert audit_rows(db_session, "auth.logout")[0].actor_label == "Peter (member)"


def test_a_system_event_needs_no_actor_or_entity(db_session):
    audit.record_event(db_session, "system.started", "App started", details={"sms": "console"})

    row = audit_rows(db_session, "system.started")[0]
    assert row.actor_user_id is None
    assert row.actor_label is None
    assert row.entity_type is None
    assert row.level == "info"


def test_levels_come_from_the_catalogue(db_session):
    audit.record_event(db_session, "sms.failed", "SMS failed")
    audit.record_event(db_session, "system.error", "Unhandled error")

    assert audit_rows(db_session, "sms.failed")[0].level == "warning"
    assert audit_rows(db_session, "system.error")[0].level == "error"


def test_request_id_and_ip_are_filled_in_from_the_current_request(db_session):
    request_token = request_id_var.set("7f3a9c1e")
    ip_token = client_ip_var.set("203.0.113.9")
    try:
        audit.record_event(db_session, "auth.logout", "Signed out")
    finally:
        request_id_var.reset(request_token)
        client_ip_var.reset(ip_token)

    row = audit_rows(db_session, "auth.logout")[0]
    assert row.request_id == "7f3a9c1e"
    assert row.ip_address == "203.0.113.9"


def test_a_rolled_back_action_leaves_no_success_row(db_session, owner):
    db_session.commit()
    db_session.add(Customer(first_name="A", last_name="B", phone="254722000003",
                            location="X", meter_number="MTR-0099"))
    audit.record_event(db_session, "customer.created", "Customer MTR-0099 added", actor=owner)

    db_session.rollback()

    assert audit_rows(db_session, "customer.created") == []


@pytest.mark.real_audit_session
def test_a_rejection_is_saved_even_though_the_action_is_rolled_back(test_engine):
    # Rule 3. No db_session here: real, separate connections, so the problem row is
    # committed on its own and the caller's rollback cannot take it with it.
    unique_id = uuid.uuid4().hex[:8]
    token = request_id_var.set(unique_id)
    try:
        with Session(test_engine) as caller_session:
            caller_session.add(Customer(first_name="A", last_name="B", phone="254722000003",
                                        location="X", meter_number="MTR-ROLLBACK"))
            caller_session.flush()
            audit.record_problem(
                "reading.rejected", "Reading refused for MTR-ROLLBACK",
                details={"reason": "lower than previous"},
            )
            caller_session.rollback()
    finally:
        request_id_var.reset(token)

    try:
        with Session(test_engine) as other_session:
            saved = other_session.scalars(
                select(AuditLog).where(AuditLog.request_id == unique_id)
            ).all()
            customer_left_behind = other_session.scalar(
                select(Customer).where(Customer.meter_number == "MTR-ROLLBACK")
            )
        assert len(saved) == 1
        assert saved[0].level == "warning"
        assert saved[0].details == {"reason": "lower than previous"}
        assert customer_left_behind is None
    finally:
        with Session(test_engine) as cleanup_session:
            cleanup_session.execute(delete(AuditLog).where(AuditLog.request_id == unique_id))
            cleanup_session.commit()


def test_a_problem_row_can_be_read_in_a_normal_test(db_session, owner, customer):
    audit.record_problem(
        "payment.rejected", "Payment refused", actor=owner,
        customer_id=customer.customer_id, details={"reason": "duplicate M-Pesa code"},
    )

    row = audit_rows(db_session, "payment.rejected")[0]
    assert row.actor_label == "Daniel (owner)"
    assert row.customer_id == customer.customer_id


def test_a_failed_audit_write_does_not_stop_the_users_action(db_session, caplog, monkeypatch):
    def row_that_cannot_be_saved(*args):
        # longer than the 50 characters the column holds, so MySQL refuses it
        return AuditLog(event_type="x" * 60, summary="too long")

    monkeypatch.setattr(audit, "_build_audit_row", row_that_cannot_be_saved)
    db_session.add(Customer(first_name="Mary", last_name="W", phone="254722000002",
                            location="Kiawara", meter_number="MTR-0050"))

    with caplog.at_level(logging.ERROR, logger="app.audit"):
        audit.record_event(db_session, "customer.created", "Customer MTR-0050 added")
    db_session.commit()

    assert "could not be saved" in caplog.text
    assert db_session.scalar(select(Customer).where(Customer.meter_number == "MTR-0050"))


def test_a_failed_problem_write_does_not_raise(caplog, monkeypatch):
    def broken_session():
        raise RuntimeError("database is down")

    monkeypatch.setattr(audit, "_open_problem_session", broken_session)

    with caplog.at_level(logging.ERROR, logger="app.audit"):
        audit.record_problem("system.error", "Something broke")

    assert "could not be saved" in caplog.text


def test_an_event_outside_the_catalogue_is_not_recorded(db_session, caplog):
    with caplog.at_level(logging.ERROR, logger="app.audit"):
        audit.record_event(db_session, "reading.edited", "Near-duplicate of reading.updated")

    assert audit_rows(db_session, "reading.edited") == []
    assert "not an event in the audit catalogue" in caplog.text


def test_secrets_and_phone_numbers_never_reach_a_row(db_session):
    audit.record_event(
        db_session, "user.password_reset", "Reset for 0722123456",
        details={
            "user_id": 5,
            "password": "hunter2",
            "new_password_hash": "$2b$12$abc",
            "csrf_token": "t0ken",
            "Session_Cookie": "c00kie",
            "api_key": "k",
            "customer_phone": "254722123456",
            "note": "called +254722123456 about it",
            "nested": {"token": "abc", "kept": "yes"},
            "how": "phone",
        },
    )

    row = audit_rows(db_session, "user.password_reset")[0]
    assert row.summary == "Reset for [phone removed]"
    assert row.details == {
        "user_id": 5,
        "note": "called [phone removed] about it",
        "nested": {"kept": "yes"},
        "how": "phone",
    }


def test_a_long_summary_is_cut_to_fit_the_column(db_session):
    audit.record_event(db_session, "auth.logout", "x" * 400)

    assert len(audit_rows(db_session, "auth.logout")[0].summary) == 255


def test_the_writer_has_no_way_to_change_or_remove_a_row():
    public_names = [name for name in dir(audit) if not name.startswith("_")]

    assert not [n for n in public_names if "update" in n or "delete" in n or "remove" in n]
