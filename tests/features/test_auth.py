"""auth/service.py: registration of the first account, signing in and signing out
(TASK-025 to TASK-027, PRD section 4.1, SECURITY.md section 1)."""

import pytest
from sqlalchemy import func, select

from app.core.errors import BusinessRuleError, PermissionDeniedError, TooManyAttemptsError
from app.core.security import verify_password
from app.features.auth import service as auth
from app.models import AuditLog, User
from tests.conftest import TEST_PASSWORD


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


def user_count(db_session):
    return db_session.scalar(select(func.count()).select_from(User))


# ---- TASK-025: registration (AUTH-2) ----

def test_registration_is_open_while_nobody_has_an_account(db_session):
    assert auth.registration_is_open(db_session) is True


def test_registration_is_closed_once_any_account_exists(db_session, make_user):
    make_user(role="member")

    assert auth.registration_is_open(db_session) is False


def test_the_first_registration_creates_the_owner(db_session):
    owner = auth.register_owner(db_session, " Daniel ", "Kamau", "0722 123 456", TEST_PASSWORD)

    assert owner.role == "owner"
    assert owner.status == "active"
    assert owner.first_name == "Daniel"
    assert owner.phone == "254722123456"
    assert owner.email is None
    assert verify_password(TEST_PASSWORD, owner.password_hash)
    assert TEST_PASSWORD not in owner.password_hash


def test_the_owner_can_register_with_an_email_instead(db_session):
    owner = auth.register_owner(db_session, "Daniel", "Kamau", "Daniel@Example.com", TEST_PASSWORD)

    assert owner.email == "daniel@example.com"
    assert owner.phone is None


def test_the_second_registration_is_refused(db_session):
    auth.register_owner(db_session, "Daniel", "Kamau", "0722123456", TEST_PASSWORD)

    with pytest.raises(PermissionDeniedError, match="Registration is closed"):
        auth.register_owner(db_session, "Someone", "Else", "0733123456", TEST_PASSWORD)

    assert user_count(db_session) == 1


def test_registration_records_owner_registered(db_session):
    owner = auth.register_owner(db_session, "Daniel", "Kamau", "0722123456", TEST_PASSWORD)

    row = audit_rows(db_session, "auth.owner_registered")[0]
    assert row.level == "info"
    assert row.actor_user_id == owner.user_id
    assert row.entity_type == "user" and row.entity_id == owner.user_id
    assert row.details == {"user_id": owner.user_id}


@pytest.mark.parametrize(
    "first_name,last_name,identifier,password,field",
    [
        ("", "Kamau", "0722123456", TEST_PASSWORD, "first_name"),
        ("Daniel", "  ", "0722123456", TEST_PASSWORD, "last_name"),
        ("Daniel", "Kamau", "", TEST_PASSWORD, "identifier"),
        ("Daniel", "Kamau", "not a number", TEST_PASSWORD, "identifier"),
        ("Daniel", "Kamau", "0722123456", "short", "password"),
        ("Daniel", "Kamau", "0722123456", "0722123456", "password"),   # own phone number
        ("Daniel", "Kamau", "0722123456", "password", "password"),
    ],
)
def test_bad_registration_details_are_refused_beside_the_right_field(
    db_session, first_name, last_name, identifier, password, field
):
    with pytest.raises(BusinessRuleError) as raised:
        auth.register_owner(db_session, first_name, last_name, identifier, password)

    assert raised.value.field == field
    assert user_count(db_session) == 0
    assert audit_rows(db_session, "auth.owner_registered") == []


# ---- TASK-026: sign in by phone or email (AUTH-1, AUTH-4, AUTH-S2) ----

@pytest.mark.parametrize("typed", ["0722123456", "0722 123 456", "254722123456", "+254722123456"])
def test_sign_in_with_the_phone_number_in_any_common_spelling(db_session, make_user, typed):
    owner = make_user(phone="254722123456")

    assert auth.sign_in(db_session, typed, TEST_PASSWORD).user_id == owner.user_id


def test_sign_in_with_the_email_in_any_case(db_session, make_user):
    member = make_user(role="member", phone=None, email="peter@example.com")

    assert auth.sign_in(db_session, " Peter@Example.COM ", TEST_PASSWORD).user_id == member.user_id


@pytest.mark.parametrize(
    "case,identifier,password",
    [
        ("wrong password", "0722123456", "river-stone-43"),
        ("empty password", "0722123456", ""),
        ("unknown phone", "0733999999", TEST_PASSWORD),
        ("unknown email", "nobody@example.com", TEST_PASSWORD),
        ("not a phone or email", "hello", TEST_PASSWORD),
        ("empty identifier", "", TEST_PASSWORD),
    ],
)
def test_every_failure_gives_the_same_generic_message(db_session, make_user, case, identifier, password):
    make_user(phone="254722123456")

    with pytest.raises(BusinessRuleError) as raised:
        auth.sign_in(db_session, identifier, password)

    assert raised.value.message == "We could not sign you in. Check your details and try again."
    assert raised.value.field is None


def test_an_inactive_account_cannot_sign_in_even_with_the_right_password(db_session, make_user):
    make_user(role="member", status="inactive", phone="254722123456")

    with pytest.raises(BusinessRuleError, match="We could not sign you in"):
        auth.sign_in(db_session, "0722123456", TEST_PASSWORD)


def test_a_successful_sign_in_is_recorded(db_session, make_user):
    member = make_user(role="member", first_name="Peter", phone="254722123456")

    auth.sign_in(db_session, "0722123456", TEST_PASSWORD)

    row = audit_rows(db_session, "auth.login_success")[0]
    assert row.level == "info"
    assert row.actor_user_id == member.user_id
    assert row.details == {"user_id": member.user_id, "how": "phone"}


def test_a_failed_sign_in_is_recorded_without_the_password(db_session, make_user):
    make_user(phone="254722123456")

    with pytest.raises(BusinessRuleError):
        auth.sign_in(db_session, "0722123456", "river-stone-43")

    row = audit_rows(db_session, "auth.login_failed")[0]
    assert row.level == "warning"
    assert row.actor_user_id is None
    assert "river-stone-43" not in row.summary + str(row.details)
    assert row.details["how"] == "phone"


def test_a_failed_sign_in_does_not_say_whether_the_account_exists(db_session, make_user):
    make_user(phone="254722123456")

    for identifier in ("0722123456", "0733999999"):     # a real account, then nobody
        with pytest.raises(BusinessRuleError):
            auth.sign_in(db_session, identifier, "river-stone-43")

    real, nobody = audit_rows(db_session, "auth.login_failed")
    assert real.summary == nobody.summary
    assert real.details == nobody.details
    assert real.entity_id is None and nobody.entity_id is None


# ---- TASK-027: the login attempt limit (AUTH-S5) ----

def fail_to_sign_in(db_session, identifier, times):
    for _ in range(times):
        with pytest.raises(BusinessRuleError):
            auth.sign_in(db_session, identifier, "wrong-password-1")


def test_the_sixth_attempt_is_refused_with_the_same_message_even_with_the_right_password(db_session, make_user):
    make_user(phone="254722123456")
    fail_to_sign_in(db_session, "0722123456", 5)

    with pytest.raises(TooManyAttemptsError) as raised:
        auth.sign_in(db_session, "0722123456", TEST_PASSWORD)

    assert raised.value.message == "We could not sign you in. Check your details and try again."
    assert raised.value.status_code == 429


def test_the_lock_is_recorded_as_login_locked(db_session, make_user):
    make_user(phone="254722123456")

    fail_to_sign_in(db_session, "0722123456", 5)

    rows = audit_rows(db_session, "auth.login_locked")
    assert len(rows) == 1
    assert rows[0].level == "warning"
    assert rows[0].details["attempts"] == 5
    assert rows[0].details["how"] == "phone"
    assert rows[0].details["locked_until"].endswith("+03:00")


def test_each_failure_records_its_attempt_number(db_session, make_user):
    make_user(phone="254722123456")

    fail_to_sign_in(db_session, "0722123456", 3)

    attempts = [row.details["attempt"] for row in audit_rows(db_session, "auth.login_failed")]
    assert attempts == [1, 2, 3]


def test_a_refused_attempt_during_the_lock_is_recorded(db_session, make_user):
    make_user(phone="254722123456")
    fail_to_sign_in(db_session, "0722123456", 5)

    with pytest.raises(TooManyAttemptsError):
        auth.sign_in(db_session, "0722123456", TEST_PASSWORD)

    assert audit_rows(db_session, "auth.login_failed")[-1].details == {"how": "phone", "locked": True}
    assert audit_rows(db_session, "auth.login_success") == []


def test_different_spellings_of_one_phone_number_count_together(db_session, make_user):
    make_user(phone="254722123456")
    for spelling in ("0722123456", "0722 123 456", "+254722123456", "254722123456", "0722-123-456"):
        fail_to_sign_in(db_session, spelling, 1)

    with pytest.raises(TooManyAttemptsError):
        auth.sign_in(db_session, "0722123456", TEST_PASSWORD)


def test_an_unknown_account_is_locked_just_the_same(db_session):
    # so the lock cannot be used to find out which accounts exist
    fail_to_sign_in(db_session, "0799000000", 5)

    with pytest.raises(TooManyAttemptsError):
        auth.sign_in(db_session, "0799000000", TEST_PASSWORD)


def test_other_accounts_can_still_sign_in_during_a_lock(db_session, make_user):
    make_user(phone="254722123456")
    other = make_user(role="member", phone="254733123456")
    fail_to_sign_in(db_session, "0722123456", 5)

    assert auth.sign_in(db_session, "0733123456", TEST_PASSWORD).user_id == other.user_id


def test_a_good_sign_in_starts_the_count_again(db_session, make_user):
    make_user(phone="254722123456")
    fail_to_sign_in(db_session, "0722123456", 4)
    auth.sign_in(db_session, "0722123456", TEST_PASSWORD)

    fail_to_sign_in(db_session, "0722123456", 4)

    assert auth.sign_in(db_session, "0722123456", TEST_PASSWORD)
