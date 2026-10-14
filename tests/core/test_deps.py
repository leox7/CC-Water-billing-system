"""Who is signed in and what they may do (TASK-024, AUTH-7, SES-2, AUTH-S1, AUTHZ-2)."""

import pytest
from fastapi import Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.core.deps import PUBLIC_PATHS, require_login, require_owner
from app.models import AuditLog, User
from tests.conftest import session_cookie, set_session


@pytest.fixture
def app_client(web):
    """The real app, plus three test pages: one with no protection of its own, one that
    asks for a signed-in user, and one that asks for the owner."""

    @web.app.get("/test/forgotten")
    def forgotten_route():
        return PlainTextResponse("reached")

    @web.app.get("/test/me")
    def me(user: User = Depends(require_login)):
        return PlainTextResponse(f"hello {user.first_name}")

    @web.app.get("/test/owner")
    def owner_only(user: User = Depends(require_owner)):
        return PlainTextResponse("owner page")

    return web


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


# ---- nobody signed in ----

@pytest.mark.parametrize("path", ["/test/forgotten", "/test/me", "/test/owner"])
def test_anonymous_visitors_are_sent_to_login(app_client, path):
    response = app_client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_route_nobody_protected_still_needs_sign_in(app_client):
    # AUTH-S1: a forgotten route fails closed
    response = app_client.get("/test/forgotten", follow_redirects=False)

    assert response.status_code == 303
    assert "reached" not in response.text


def test_an_anonymous_htmx_request_gets_hx_redirect(app_client):
    response = app_client.get("/test/me", headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


def test_only_login_register_and_health_are_public():
    assert PUBLIC_PATHS == {"/login", "/register", "/health"}


def test_the_health_check_needs_no_sign_in(app_client):
    assert app_client.get("/health").status_code == 200


def test_a_tampered_cookie_counts_as_nobody(app_client, make_user):
    owner = make_user()
    good_cookie = session_cookie({"user_id": owner.user_id})
    app_client.cookies.set("session", good_cookie[:-3] + "xyz", domain="testserver.local", path="/")

    response = app_client.get("/test/me", follow_redirects=False)

    assert response.status_code == 303


# ---- signed in ----

def test_a_signed_in_owner_reaches_every_page(app_client, make_user, sign_in_as):
    sign_in_as(app_client, make_user(first_name="Daniel"))

    assert app_client.get("/test/forgotten").text == "reached"
    assert app_client.get("/test/me").text == "hello Daniel"
    assert app_client.get("/test/owner").text == "owner page"


def test_a_member_reaches_signed_in_pages(app_client, make_user, sign_in_as):
    sign_in_as(app_client, make_user(role="member", first_name="Peter"))

    assert app_client.get("/test/me").text == "hello Peter"


# ---- require_owner ----

def test_a_member_gets_403_on_an_owner_page(app_client, make_user, sign_in_as):
    sign_in_as(app_client, make_user(role="member", first_name="Peter"))

    response = app_client.get("/test/owner")

    assert response.status_code == 403
    assert "You do not have access" in response.text
    assert "owner page" not in response.text


def test_the_403_page_keeps_the_signed_in_layout(app_client, make_user, sign_in_as):
    sign_in_as(app_client, make_user(role="member", first_name="Peter"))

    response = app_client.get("/test/owner")

    assert "Peter (Member)" in response.text


def test_a_refused_page_is_recorded_as_access_denied(app_client, make_user, sign_in_as, db_session):
    member = make_user(role="member", first_name="Peter")
    sign_in_as(app_client, member)

    app_client.get("/test/owner")

    rows = audit_rows(db_session, "auth.access_denied")
    assert len(rows) == 1
    assert rows[0].level == "warning"
    assert rows[0].actor_user_id == member.user_id
    assert rows[0].actor_label == "Peter (member)"
    assert rows[0].details == {"route": "/test/owner", "method": "GET", "role": "member"}


def test_the_role_comes_from_the_database_not_the_cookie(app_client, make_user):
    member = make_user(role="member")
    # a cookie that claims to be the owner changes nothing (AUTHZ-3, SES-2)
    set_session(app_client, {"user_id": member.user_id, "role": "owner"})

    assert app_client.get("/test/owner").status_code == 403


def test_a_role_change_in_the_database_applies_on_the_next_request(app_client, make_user, sign_in_as, db_session):
    user = make_user(role="owner")
    sign_in_as(app_client, user)
    assert app_client.get("/test/owner").status_code == 200

    user.role = "member"
    db_session.flush()

    assert app_client.get("/test/owner").status_code == 403


# ---- a deactivated or missing user with a valid cookie ----

def test_a_deactivated_user_with_a_valid_cookie_is_locked_out(app_client, make_user, sign_in_as):
    sign_in_as(app_client, make_user(role="member", status="inactive"))

    response = app_client.get("/test/me", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_deactivation_locks_the_user_out_on_their_very_next_click(app_client, make_user, sign_in_as, db_session):
    # AUTH-7
    member = make_user(role="member")
    sign_in_as(app_client, member)
    assert app_client.get("/test/me").status_code == 200

    member.status = "inactive"
    db_session.flush()

    response = app_client.get("/test/me", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_session_of_a_deactivated_user_is_cleared(app_client, make_user, sign_in_as):
    sign_in_as(app_client, make_user(role="member", status="inactive"))

    response = app_client.get("/test/me", follow_redirects=False)

    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith("session=null;")
    assert "expires=Thu, 01 Jan 1970" in set_cookie


def test_a_deactivated_user_is_recorded_as_session_rejected(app_client, make_user, sign_in_as, db_session):
    member = make_user(role="member", status="inactive")
    sign_in_as(app_client, member)

    app_client.get("/test/me")

    rows = audit_rows(db_session, "auth.session_rejected")
    assert len(rows) == 1
    assert rows[0].level == "warning"
    assert rows[0].details == {"user_id": member.user_id, "reason": "inactive"}


def test_a_cookie_for_a_user_who_no_longer_exists_is_rejected(app_client, db_session):
    set_session(app_client, {"user_id": 999999})

    response = app_client.get("/test/me", follow_redirects=False)

    assert response.status_code == 303
    assert audit_rows(db_session, "auth.session_rejected")[0].details == {"user_id": 999999, "reason": "missing"}
