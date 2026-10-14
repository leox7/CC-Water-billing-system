"""The Account screen, end to end through the real app (TASK-031, ACC-1 to ACC-3, AUTH-6)."""

import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.security import verify_password
from app.models import AuditLog, Bill, Customer, MeterReading, Payment, User
from tests.conftest import TEST_CSRF_TOKEN, TEST_PASSWORD

HTMX = {"HX-Request": "true", "X-CSRF-Token": TEST_CSRF_TOKEN}
NEW_PASSWORD = "lake-forest-77"


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel", phone="254722000001")


@pytest.fixture
def member(make_user):
    return make_user(role="member", first_name="Peter", phone="254733000002")


@pytest.fixture
def as_owner(web, owner, sign_in_as):
    sign_in_as(web, owner)
    return web


def add_reading_by(db_session, user):
    customer = Customer(first_name="Mary", last_name="Wanjiru", phone="254722999999",
                        location="Kiawara", meter_number="MTR-0032")
    db_session.add(customer)
    db_session.flush()
    reading = MeterReading(customer_id=customer.customer_id, billing_period="2026-08",
                           previous_reading=Decimal("0"), current_reading=Decimal("10"),
                           units_consumed=Decimal("10"), reading_date=date(2026, 8, 20),
                           recorded_by=user.user_id)
    db_session.add(reading)
    db_session.flush()
    return reading


def add_payment_by(db_session, member, owner):
    """A payment recorded by the member, on a bill whose reading the owner entered."""
    reading = add_reading_by(db_session, owner)
    bill = Bill(reading_id=reading.reading_id, customer_id=reading.customer_id,
                rate_per_unit=Decimal("130"), service_charge=Decimal("150"), units_consumed=Decimal("10"),
                consumption_charge=Decimal("1300"), total_due=Decimal("1450"))
    db_session.add(bill)
    db_session.flush()
    db_session.add(Payment(bill_id=bill.bill_id, amount=Decimal("500"), payment_date=date(2026, 8, 25),
                           recorded_by=member.user_id))
    db_session.flush()


def add_record_by(db_session, member, owner, recorded):
    if recorded == "reading":
        add_reading_by(db_session, member)
    else:
        add_payment_by(db_session, member, owner)


def row_html(html, user_id):
    return re.search(rf'<tr id="team-row-{user_id}">.*?</tr>', html, re.S).group(0)


# ---- what each role sees (ACC-1, ACC-2, AUTH-6) ----

def test_the_owner_sees_the_full_account_screen(as_owner, member):
    html = as_owner.get("/account").text

    assert 'id="team-rows"' in html
    assert "Peter Test" in html and "Daniel Test" in html
    assert 'id="add-member-form"' in html
    assert 'id="change-password-form"' in html
    assert 'action="/logout"' in html
    assert "/static/account/account.css" in html


def test_a_member_sees_only_change_password_and_sign_out(web, member, sign_in_as):
    sign_in_as(web, member)

    html = web.get("/account").text

    assert 'id="change-password-form"' in html
    assert 'action="/logout"' in html
    # PRD 4.1: a member sees no add-member form
    assert 'id="team-rows"' not in html
    assert 'id="add-member-form"' not in html and 'action="/account/members"' not in html
    assert "Add member" not in html and "Set new password" not in html


def test_first_name_and_role_show_next_to_sign_out(web, member, sign_in_as):
    sign_in_as(web, member)

    html = web.get("/account").text

    assert "You are signed in as Peter (Member)." in html
    assert "Peter (Member)" in re.search(r'<div class="sidebar-footer">.*?</div>\s*</aside>', html, re.S).group(0)


def test_the_owner_row_has_no_actions(as_owner, owner, member):
    html = as_owner.get("/account").text

    owner_row = row_html(html, owner.user_id)
    assert "Deactivate" not in owner_row and "Set new password" not in owner_row and "Delete" not in owner_row


def test_a_member_row_has_deactivate_set_password_and_delete(as_owner, member):
    member_row = row_html(as_owner.get("/account").text, member.user_id)

    assert 'data-open-dialog="deactivate-dialog-' in member_row
    assert 'data-open-dialog="password-dialog-' in member_row
    assert 'data-open-dialog="delete-dialog-' in member_row
    assert '<dialog class="modal" id="deactivate-dialog-' in member_row


@pytest.mark.parametrize("recorded", ["reading", "payment"])
def test_delete_is_not_offered_for_a_member_who_recorded_a_reading_or_payment(as_owner, owner, member, db_session,
                                                                             recorded):
    add_record_by(db_session, member, owner, recorded)

    member_row = row_html(as_owner.get("/account").text, member.user_id)

    assert "Delete" not in member_row
    assert "Deactivate" in member_row


def test_the_page_holds_no_inline_styles_or_scripts(as_owner, member):
    html = as_owner.get("/account").text

    assert " style=" not in html and "<style" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


def test_reading_the_page_does_not_rewrite_the_session_so_its_lifetime_stays_fixed(as_owner):
    # SES-3
    response = as_owner.get("/account")

    assert response.status_code == 200
    assert "set-cookie" not in response.headers


# ---- adding a member (AUTH-3) ----

def test_the_owner_adds_a_member_and_the_row_appears_without_a_reload(as_owner, db_session):
    response = as_owner.post("/account/members", headers=HTMX, data={
        "first_name": "Amina", "last_name": "Hassan", "identifier": "0744 123 456", "password": NEW_PASSWORD})

    assert response.status_code == 200
    html = response.text
    # the new row travels inside <template>, or the browser drops a <tbody> that follows the form
    assert re.search(r'<template>\s*<tbody hx-swap-oob="beforeend:#team-rows">', html)
    assert re.search(r'<template>\s*<div id="account-messages"[^>]*hx-swap-oob="innerHTML"', html)
    assert "Amina Hassan" in html
    assert 'id="add-member-form"' in html and 'value="Amina"' not in html     # a fresh, empty form
    assert "Amina Hassan was added." in html
    member = db_session.scalar(select(User).where(User.phone == "254744123456"))
    assert member.role == "member"


def test_a_role_sent_in_the_form_is_ignored(as_owner, db_session):
    # AUTHZ-3
    as_owner.post("/account/members", headers=HTMX, data={
        "first_name": "Amina", "last_name": "Hassan", "identifier": "0744123456", "password": NEW_PASSWORD,
        "role": "owner"})

    assert db_session.scalar(select(User).where(User.phone == "254744123456")).role == "member"


def test_a_number_in_use_comes_back_beside_the_field_with_typed_values_kept(as_owner, member):
    response = as_owner.post("/account/members", headers=HTMX, data={
        "first_name": "Amina", "last_name": "Hassan", "identifier": "0733000002", "password": NEW_PASSWORD})

    assert response.status_code == 409
    html = response.text
    assert 'id="add-member-form"' in html
    assert 'id="field-identifier-error"' in html and "already used" in html
    assert 'value="Amina"' in html and 'value="0733000002"' in html
    assert NEW_PASSWORD not in html


def test_without_htmx_adding_a_member_goes_back_to_the_page(as_owner):
    response = as_owner.post("/account/members", follow_redirects=False, data={
        "first_name": "Amina", "last_name": "Hassan", "identifier": "0744123456", "password": NEW_PASSWORD,
        "csrf_token": TEST_CSRF_TOKEN})

    assert response.status_code == 303
    assert response.headers["location"] == "/account"


# ---- deactivate and reactivate (AUTH-7) ----

def test_deactivating_swaps_the_row_and_announces_the_new_status(as_owner, member):
    response = as_owner.post(f"/account/members/{member.user_id}/deactivate", headers=HTMX)

    assert response.status_code == 200
    html = response.text
    assert html.lstrip().startswith(f'<tr id="team-row-{member.user_id}">')
    assert "Inactive" in html and 'role="status"' in html
    assert "Reactivate" in html
    assert "Peter Test was deactivated and can no longer sign in." in html
    assert member.status == "inactive"


def test_a_deactivated_member_is_locked_out_on_their_next_click(as_owner, member, sign_in_as, db_session):
    from fastapi.testclient import TestClient

    members_phone = TestClient(as_owner.app, raise_server_exceptions=False)
    sign_in_as(members_phone, member)
    assert members_phone.get("/account").status_code == 200

    as_owner.post(f"/account/members/{member.user_id}/deactivate", headers=HTMX)

    response = members_phone.get("/account", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_reactivating_swaps_the_row_back(as_owner, member):
    as_owner.post(f"/account/members/{member.user_id}/deactivate", headers=HTMX)

    html = as_owner.post(f"/account/members/{member.user_id}/reactivate", headers=HTMX).text

    assert "Active" in html and "Deactivate" in html
    assert "was reactivated and can sign in again" in html
    assert member.status == "active"


# ---- delete (AUTH-4) ----

def test_deleting_a_member_who_recorded_nothing_removes_the_row(as_owner, member, db_session):
    member_id = member.user_id

    response = as_owner.post(f"/account/members/{member_id}/delete", headers=HTMX)

    assert response.status_code == 200
    assert "<tr" not in response.text            # nothing replaces the row, so it goes
    assert "Peter Test was deleted." in response.text
    assert db_session.get(User, member_id) is None


@pytest.mark.parametrize("recorded", ["reading", "payment"])
def test_deleting_a_member_who_recorded_something_is_refused_with_a_plain_message(as_owner, owner, member, db_session,
                                                                                 recorded):
    add_record_by(db_session, member, owner, recorded)

    response = as_owner.post(f"/account/members/{member.user_id}/delete", headers=HTMX)

    assert response.status_code == 422
    assert ("Peter Test has recorded readings or payments, so the account cannot be deleted. "
            "Deactivate it instead.") in response.text
    assert 'role="alert"' in response.text
    assert db_session.get(User, member.user_id) is not None


# ---- set a new password for a member (ACC-3) ----

def test_the_owner_sets_a_new_password_and_the_member_can_sign_in_with_it(as_owner, member):
    response = as_owner.post(f"/account/members/{member.user_id}/password", headers=HTMX,
                             data={"new_password": NEW_PASSWORD})

    assert response.status_code == 200
    assert "New password set for Peter Test" in response.text
    assert NEW_PASSWORD not in response.text
    assert verify_password(NEW_PASSWORD, member.password_hash)


def test_a_weak_member_password_comes_back_as_a_message(as_owner, member):
    response = as_owner.post(f"/account/members/{member.user_id}/password", headers=HTMX,
                             data={"new_password": "short"})

    assert response.status_code == 422
    assert "at least 8 characters" in response.text
    assert verify_password(TEST_PASSWORD, member.password_hash)


def test_the_set_password_dialog_sends_its_errors_inside_itself(as_owner, member):
    member_row = row_html(as_owner.get("/account").text, member.user_id)

    assert f'data-error-target="#password-error-{member.user_id}"' in member_row
    assert f'id="password-error-{member.user_id}"' in member_row


def test_the_owners_password_cannot_be_set_from_here(as_owner, owner):
    response = as_owner.post(f"/account/members/{owner.user_id}/password", headers=HTMX,
                             data={"new_password": NEW_PASSWORD})

    assert response.status_code == 403
    assert verify_password(TEST_PASSWORD, owner.password_hash)


def test_an_unknown_member_is_a_404(as_owner):
    assert as_owner.post("/account/members/999999/deactivate", headers=HTMX).status_code == 404


def test_an_id_that_is_not_a_number_is_a_404(as_owner):
    assert as_owner.post("/account/members/abc/deactivate", headers=HTMX).status_code == 404


# ---- change your own password (AUTH-5) ----

@pytest.mark.parametrize("role", ["owner", "member"])
def test_anyone_signed_in_changes_their_own_password(web, make_user, sign_in_as, role):
    user = make_user(role=role)
    sign_in_as(web, user)

    response = web.post("/account/password", headers=HTMX,
                        data={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert response.status_code == 200
    assert "Your password has been changed." in response.text
    assert NEW_PASSWORD not in response.text and TEST_PASSWORD not in response.text
    assert verify_password(NEW_PASSWORD, user.password_hash)


def test_a_wrong_current_password_shows_beside_that_field(as_owner, owner):
    response = as_owner.post("/account/password", headers=HTMX,
                             data={"current_password": "wrong-password-1", "new_password": NEW_PASSWORD})

    assert response.status_code == 422
    assert 'id="field-current_password-error"' in response.text
    assert 'id="field-current_password" name="current_password" autocomplete="current-password" aria-invalid="true"' in response.text
    assert verify_password(TEST_PASSWORD, owner.password_hash)


def test_a_user_id_in_the_form_cannot_change_someone_elses_password(web, member, owner, sign_in_as):
    # AUTHZ-4: whose password it is comes from the session
    sign_in_as(web, member)

    web.post("/account/password", headers=HTMX,
             data={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD, "user_id": str(owner.user_id)})

    assert verify_password(TEST_PASSWORD, owner.password_hash)
    assert verify_password(NEW_PASSWORD, member.password_hash)


# ---- a member on the owner's actions (AUTHZ-2) ----

@pytest.mark.parametrize("path", ["/account/members", "/account/members/{id}/deactivate", "/account/members/{id}/reactivate",
                                  "/account/members/{id}/delete", "/account/members/{id}/password"])
def test_a_member_gets_403_on_every_owner_action(web, make_user, member, sign_in_as, db_session, path):
    other = make_user(role="member", first_name="Amina")
    sign_in_as(web, member)

    response = web.post(path.format(id=other.user_id), headers=HTMX,
                        data={"first_name": "X", "last_name": "Y", "identifier": "0755123456", "password": NEW_PASSWORD,
                              "new_password": NEW_PASSWORD})

    assert response.status_code == 403
    assert other.status == "active"
    assert verify_password(TEST_PASSWORD, other.password_hash)
    denied = db_session.scalars(select(AuditLog).where(AuditLog.event_type == "auth.access_denied")).all()
    assert denied[-1].actor_user_id == member.user_id
    assert denied[-1].details == {"route": path.format(id=other.user_id), "method": "POST", "role": "member"}
    assert db_session.scalars(select(User).where(User.phone == "254755123456")).all() == []
