"""account/service.py: members and passwords (TASK-030, AUTH-3 to AUTH-5, ACC-3,
AUTHZ-3, AUTHZ-4)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core import security
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.security import verify_password
from app.features.account import service as account
from app.models import AuditLog, Bill, Customer, MessageLog, MeterReading, Payment, User
from tests.conftest import TEST_PASSWORD

NEW_PASSWORD = "lake-forest-77"


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


def add_reading_by(db_session, user):
    """A customer and a reading recorded by `user`. Returns the reading."""
    customer_number = db_session.scalar(select(func.count()).select_from(Customer)) + 1
    customer = Customer(first_name="Mary", last_name="Wanjiru", phone="254722999999",
                        location="Kiawara", meter_number=f"MTR-{customer_number}")
    db_session.add(customer)
    db_session.flush()
    reading = MeterReading(customer_id=customer.customer_id, billing_period="2026-08",
                           previous_reading=Decimal("0"), current_reading=Decimal("10"),
                           units_consumed=Decimal("10"), reading_date=date(2026, 8, 20),
                           recorded_by=user.user_id)
    db_session.add(reading)
    db_session.flush()
    return reading


def add_payment_by(db_session, member, reading_by):
    """A payment recorded by `member`, on a bill whose reading `reading_by` entered."""
    reading = add_reading_by(db_session, reading_by)
    bill = Bill(reading_id=reading.reading_id, customer_id=reading.customer_id,
                rate_per_unit=Decimal("130"), service_charge=Decimal("150"), units_consumed=Decimal("10"),
                consumption_charge=Decimal("1300"), total_due=Decimal("1450"))
    db_session.add(bill)
    db_session.flush()
    db_session.add(Payment(bill_id=bill.bill_id, amount=Decimal("500"), payment_date=date(2026, 8, 25),
                           recorded_by=member.user_id))
    db_session.flush()


def add_marked_message_by(db_session, member, reading_by):
    """An SMS the member marked as sent with the phone method (SMS-17)."""
    reading = add_reading_by(db_session, reading_by)
    db_session.add(MessageLog(customer_id=reading.customer_id, message_type="bill", content="text",
                              status="sent", sent_by_user_id=member.user_id))
    db_session.flush()


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel", phone="254722000001")


@pytest.fixture
def member(make_user):
    return make_user(role="member", first_name="Peter", phone="254733000002")


# ---- the team list ----

def test_the_team_lists_the_owner_first_then_members_by_name(db_session, make_user, owner):
    make_user(role="member", first_name="Zawadi")
    make_user(role="member", first_name="Amina")

    team = account.list_team(db_session)

    assert [person.user.first_name for person in team] == ["Daniel", "Amina", "Zawadi"]


def test_the_team_counts_the_readings_and_payments_each_person_recorded(db_session, owner, member, make_user):
    add_reading_by(db_session, member)
    add_payment_by(db_session, member, reading_by=owner)
    amina = make_user(role="member", first_name="Amina")
    add_payment_by(db_session, amina, reading_by=owner)

    counts = {person.user.first_name: person.record_count for person in account.list_team(db_session)}

    assert counts == {"Daniel": 2, "Amina": 1, "Peter": 2}
    assert account.team_member(db_session, member).record_count == 2
    assert account.team_member(db_session, amina).record_count == 1


# ---- adding a member (AUTH-3, AUTHZ-3) ----

def test_the_owner_adds_a_member(db_session, owner):
    member = account.create_member(db_session, owner, " Peter ", "Otieno", "0733 123 456", NEW_PASSWORD)

    assert member.role == "member"
    assert member.status == "active"
    assert member.first_name == "Peter"
    assert member.phone == "254733123456"
    assert verify_password(NEW_PASSWORD, member.password_hash)


def test_a_new_member_is_recorded(db_session, owner):
    member = account.create_member(db_session, owner, "Peter", "Otieno", "peter@example.com", NEW_PASSWORD)

    row = audit_rows(db_session, "user.created")[0]
    assert row.actor_user_id == owner.user_id
    assert row.entity_type == "user" and row.entity_id == member.user_id
    assert row.details == {"user_id": member.user_id}
    assert row.summary == "Member Peter Otieno added"


def test_a_member_cannot_add_anyone(db_session, member):
    with pytest.raises(PermissionDeniedError):
        account.create_member(db_session, member, "Someone", "Else", "0744123456", NEW_PASSWORD)

    row = audit_rows(db_session, "auth.access_denied")[0]
    assert row.details == {"action": "add a member", "role": "member"}
    assert row.summary == "Peter (member) was refused: add a member"
    assert db_session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize("identifier,message", [
    ("0722000001", "phone number is already used"),          # the owner's number
    ("+254722000001", "phone number is already used"),
])
def test_a_phone_number_already_in_use_is_refused(db_session, owner, identifier, message):
    with pytest.raises(ConflictError, match=message) as raised:
        account.create_member(db_session, owner, "Peter", "Otieno", identifier, NEW_PASSWORD)

    assert raised.value.field == "identifier"
    assert raised.value.status_code == 409


def test_an_email_already_in_use_is_refused_in_any_case(db_session, owner, make_user):
    make_user(role="member", phone=None, email="amina@example.com")

    with pytest.raises(ConflictError, match="email is already used"):
        account.create_member(db_session, owner, "Peter", "Otieno", "Amina@Example.com", NEW_PASSWORD)


@pytest.mark.parametrize("first_name,identifier,password,field", [
    ("", "0733123456", NEW_PASSWORD, "first_name"),
    ("Peter", "hello", NEW_PASSWORD, "identifier"),
    ("Peter", "0733123456", "short", "password"),
    ("Peter", "0733123456", "0733123456", "password"),          # the member's own number
])
def test_bad_member_details_are_refused_beside_the_field(db_session, owner, first_name, identifier, password, field):
    with pytest.raises(BusinessRuleError) as raised:
        account.create_member(db_session, owner, first_name, "Otieno", identifier, password)

    assert raised.value.field == field
    assert db_session.scalar(select(func.count()).select_from(User)) == 1


# ---- deactivate and reactivate (AUTH-4, AUTH-7) ----

def test_the_owner_deactivates_and_reactivates_a_member(db_session, owner, member):
    assert account.deactivate_member(db_session, owner, member.user_id).status == "inactive"
    assert account.reactivate_member(db_session, owner, member.user_id).status == "active"

    deactivated = audit_rows(db_session, "user.deactivated")[0]
    reactivated = audit_rows(db_session, "user.reactivated")[0]
    assert deactivated.details == {"user_id": member.user_id} and deactivated.entity_id == member.user_id
    assert reactivated.details == {"user_id": member.user_id}
    assert deactivated.summary == "Member Peter Test deactivated"


def test_deactivating_twice_changes_nothing_the_second_time(db_session, owner, member):
    account.deactivate_member(db_session, owner, member.user_id)
    account.deactivate_member(db_session, owner, member.user_id)

    assert len(audit_rows(db_session, "user.deactivated")) == 1


def test_a_member_who_recorded_something_can_still_be_deactivated(db_session, owner, member):
    add_reading_by(db_session, member)

    assert account.deactivate_member(db_session, owner, member.user_id).status == "inactive"


def test_the_owner_account_cannot_be_deactivated(db_session, owner):
    with pytest.raises(PermissionDeniedError, match="only be done for a member"):
        account.deactivate_member(db_session, owner, owner.user_id)


@pytest.mark.parametrize("action", ["deactivate_member", "reactivate_member"])
def test_a_member_cannot_deactivate_or_reactivate_anyone(db_session, member, make_user, action):
    other = make_user(role="member", first_name="Amina", status="inactive" if action == "reactivate_member" else "active")
    status_before = other.status

    with pytest.raises(PermissionDeniedError):
        getattr(account, action)(db_session, member, other.user_id)

    assert other.status == status_before
    assert audit_rows(db_session, "auth.access_denied")[0].actor_user_id == member.user_id


def test_an_unknown_team_member_is_a_404(db_session, owner):
    with pytest.raises(NotFoundError):
        account.deactivate_member(db_session, owner, 999999)


# ---- delete only a member who recorded nothing (AUTH-4) ----

def test_a_member_who_recorded_nothing_can_be_deleted(db_session, owner, member):
    member_id = member.user_id

    name = account.delete_member(db_session, owner, member_id)

    assert name == "Peter Test"
    assert db_session.get(User, member_id) is None
    row = audit_rows(db_session, "user.deleted")[0]
    assert row.entity_type == "user" and row.entity_id == member_id
    assert row.details == {"user_id": member_id}


@pytest.mark.parametrize("recorded", ["reading", "payment", "marked message"])
def test_a_member_who_recorded_a_reading_or_a_payment_is_never_deleted(db_session, owner, member, recorded):
    # AUTH-4: marking an SMS as sent counts too, so who marked it is kept
    if recorded == "reading":
        add_reading_by(db_session, member)
    elif recorded == "payment":
        add_payment_by(db_session, member, reading_by=owner)
    else:
        add_marked_message_by(db_session, member, reading_by=owner)

    with pytest.raises(BusinessRuleError) as raised:
        account.delete_member(db_session, owner, member.user_id)

    assert raised.value.message == (
        "Peter Test has recorded readings or payments, or marked an SMS as sent, so the account cannot be deleted. "
        "Deactivate it instead."
    )
    assert db_session.get(User, member.user_id) is not None
    assert audit_rows(db_session, "user.deleted") == []


def test_the_owner_account_cannot_be_deleted(db_session, owner):
    with pytest.raises(PermissionDeniedError):
        account.delete_member(db_session, owner, owner.user_id)


def test_a_member_cannot_delete_anyone(db_session, member, make_user):
    other = make_user(role="member", first_name="Amina")

    with pytest.raises(PermissionDeniedError):
        account.delete_member(db_session, member, other.user_id)

    assert db_session.get(User, other.user_id) is not None


# ---- changing your own password (AUTH-5) ----

@pytest.mark.parametrize("role", ["owner", "member"])
def test_anyone_can_change_their_own_password(db_session, make_user, role):
    user = make_user(role=role)

    account.change_own_password(db_session, user, TEST_PASSWORD, NEW_PASSWORD)

    assert verify_password(NEW_PASSWORD, user.password_hash)
    row = audit_rows(db_session, "auth.password_changed")[0]
    assert row.actor_user_id == user.user_id
    assert row.details == {"user_id": user.user_id, "how": "account screen"}


def test_the_current_password_must_be_right(db_session, member):
    with pytest.raises(BusinessRuleError, match="not your current password") as raised:
        account.change_own_password(db_session, member, "wrong-password-1", NEW_PASSWORD)

    assert raised.value.field == "current_password"
    assert verify_password(TEST_PASSWORD, member.password_hash)


@pytest.mark.parametrize("weak", ["short", "aaaaaaaa", "password", "0733000002"])
def test_the_new_password_follows_the_rules(db_session, member, weak):
    with pytest.raises(BusinessRuleError) as raised:
        account.change_own_password(db_session, member, TEST_PASSWORD, weak)

    assert raised.value.field == "new_password"
    assert verify_password(TEST_PASSWORD, member.password_hash)


# ---- the owner sets a new password for a member (ACC-3, AUTHZ-4) ----

def test_the_owner_sets_a_new_password_for_a_member(db_session, owner, member):
    account.set_member_password(db_session, owner, member.user_id, NEW_PASSWORD)

    assert verify_password(NEW_PASSWORD, member.password_hash)
    row = audit_rows(db_session, "user.password_reset")[0]
    assert row.actor_user_id == owner.user_id
    assert row.entity_id == member.user_id
    assert row.details == {"user_id": member.user_id}
    assert NEW_PASSWORD not in row.summary + str(row.details)


def test_setting_a_new_password_lifts_the_members_sign_in_lock(db_session, owner, member):
    for _ in range(5):
        security.login_limiter.record_failure("254733000002")
    assert security.login_limiter.seconds_locked("254733000002") > 0

    account.set_member_password(db_session, owner, member.user_id, NEW_PASSWORD)

    assert security.login_limiter.seconds_locked("254733000002") == 0


def test_the_lock_is_lifted_for_a_member_who_signs_in_with_email(db_session, owner, make_user):
    member = make_user(role="member", phone=None, email="amina@example.com")
    for _ in range(5):
        security.login_limiter.record_failure("amina@example.com")

    account.set_member_password(db_session, owner, member.user_id, NEW_PASSWORD)

    assert security.login_limiter.seconds_locked("amina@example.com") == 0


@pytest.mark.parametrize("target", ["another member", "themselves", "the owner"])
def test_a_member_cannot_reset_anyones_password(db_session, owner, member, make_user, target):
    others = {"another member": lambda: make_user(role="member", first_name="Amina"),
              "themselves": lambda: member, "the owner": lambda: owner}
    other = others[target]()

    with pytest.raises(PermissionDeniedError):
        account.set_member_password(db_session, member, other.user_id, NEW_PASSWORD)

    assert verify_password(TEST_PASSWORD, other.password_hash)
    assert audit_rows(db_session, "user.password_reset") == []
    assert audit_rows(db_session, "auth.access_denied")[0].actor_user_id == member.user_id


def test_a_new_password_is_never_set_for_an_owner_account(db_session, owner):
    with pytest.raises(PermissionDeniedError):
        account.set_member_password(db_session, owner, owner.user_id, NEW_PASSWORD)

    assert verify_password(TEST_PASSWORD, owner.password_hash)


def test_the_new_member_password_follows_the_rules(db_session, owner, member):
    with pytest.raises(BusinessRuleError) as raised:
        account.set_member_password(db_session, owner, member.user_id, "0733000002")    # the member's own number

    assert raised.value.field == "new_password"
    assert verify_password(TEST_PASSWORD, member.password_hash)


# ---- the reset script's rule (AUTH-8) ----

def test_the_reset_script_changes_the_owners_password(db_session, owner):
    account.reset_owner_password(db_session, "0722 000 001", NEW_PASSWORD)

    assert verify_password(NEW_PASSWORD, owner.password_hash)
    row = audit_rows(db_session, "auth.password_changed")[0]
    assert row.details == {"user_id": owner.user_id, "how": "reset script"}
    assert row.actor_user_id is None


def test_the_reset_script_refuses_a_member(db_session, owner, member):
    with pytest.raises(PermissionDeniedError, match="not the owner"):
        account.reset_owner_password(db_session, "0733000002", NEW_PASSWORD)

    assert verify_password(TEST_PASSWORD, member.password_hash)


def test_the_reset_script_refuses_an_unknown_account(db_session, owner):
    with pytest.raises(NotFoundError):
        account.reset_owner_password(db_session, "0799000000", NEW_PASSWORD)


def test_the_reset_script_refuses_a_weak_password(db_session, owner):
    with pytest.raises(BusinessRuleError):
        account.reset_owner_password(db_session, "0722000001", "short")

    assert verify_password(TEST_PASSWORD, owner.password_hash)
