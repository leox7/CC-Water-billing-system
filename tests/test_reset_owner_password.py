"""scripts/reset_owner_password.py (TASK-032, AUTH-8)."""

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models import AuditLog
from scripts import reset_owner_password as script
from tests.conftest import TEST_PASSWORD

NEW_PASSWORD = "lake-forest-77"


@pytest.fixture
def run_script(db_session):
    """Runs the script with typed answers, in the test transaction. Returns (exit code, printed lines)."""

    def run(identifier, password, typed_again=None):
        answers = iter([password, password if typed_again is None else typed_again])
        printed = []
        code = script.main(
            ask=lambda question: identifier,
            ask_hidden=lambda question: next(answers),
            say=printed.append,
            open_session=lambda: Session(bind=db_session.get_bind(), join_transaction_mode="create_savepoint"),
        )
        return code, printed

    return run


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel", phone="254722000001")


def test_it_changes_the_owners_password(run_script, owner, db_session):
    code, printed = run_script("0722 000 001", NEW_PASSWORD)

    assert code == 0
    assert printed == ["The password for Daniel Test has been changed. Give it to them in person."]
    db_session.refresh(owner)
    assert verify_password(NEW_PASSWORD, owner.password_hash)


def test_it_writes_the_audit_row(run_script, owner, db_session):
    run_script("0722000001", NEW_PASSWORD)

    row = db_session.scalars(select(AuditLog).where(AuditLog.event_type == "auth.password_changed")).one()
    assert row.details == {"user_id": owner.user_id, "how": "reset script"}
    assert row.entity_id == owner.user_id


def test_it_refuses_a_member(run_script, owner, make_user, db_session):
    member = make_user(role="member", phone="254733000002")

    code, printed = run_script("0733000002", NEW_PASSWORD)

    assert code == 1
    assert "not the owner" in printed[0] and "Nothing was changed" in printed[0]
    db_session.refresh(member)
    assert verify_password(TEST_PASSWORD, member.password_hash)


@pytest.mark.parametrize("weak", ["short", "aaaaaaaa", "password", "0722000001"])
def test_it_rejects_a_weak_password(run_script, owner, db_session, weak):
    code, printed = run_script("0722000001", weak)

    assert code == 1
    assert "Nothing was changed" in printed[0]
    db_session.refresh(owner)
    assert verify_password(TEST_PASSWORD, owner.password_hash)


def test_the_two_passwords_must_match(run_script, owner, db_session):
    code, printed = run_script("0722000001", NEW_PASSWORD, typed_again="lake-forest-78")

    assert code == 1
    assert printed == ["The two passwords are not the same. Nothing was changed."]
    db_session.refresh(owner)
    assert verify_password(TEST_PASSWORD, owner.password_hash)


def test_an_unknown_account_changes_nothing(run_script, owner):
    code, printed = run_script("0799000000", NEW_PASSWORD)

    assert code == 1
    assert "No account has that phone number or email." in printed[0]


def test_the_password_is_asked_for_without_showing_it():
    import getpass

    assert script.main.__defaults__[1] is getpass.getpass


def test_the_password_is_never_printed_or_logged(run_script, owner, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        _, printed = run_script("0722000001", NEW_PASSWORD)
        run_script("0722000001", "short")
        run_script("0722000001", NEW_PASSWORD, typed_again="lake-forest-78")

    everything = " ".join(printed) + capsys.readouterr().out + capsys.readouterr().err + caplog.text
    for secret in (NEW_PASSWORD, "short", "lake-forest-78"):
        assert secret not in everything


def test_a_database_failure_says_so_plainly_and_changes_nothing(owner):
    def broken_session():
        raise RuntimeError("database is down")

    printed = []
    answers = iter([NEW_PASSWORD, NEW_PASSWORD])
    code = script.main(ask=lambda q: "0722000001", ask_hidden=lambda q: next(answers),
                       say=printed.append, open_session=broken_session)

    assert code == 1
    assert printed == ["Something went wrong on our side. Please try again. Nothing was changed."]
