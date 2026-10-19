"""The login and register pages, end to end through the real app (TASK-028, TASK-029)."""

import base64
import json
import re

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, User
from tests.conftest import TEST_PASSWORD

GENERIC_FAILURE = "We could not sign you in. Check your details and try again."


def csrf_token_in(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def open_and_post(client, path, data):
    """What a browser does: open the page (which sets the session and its token), then send the form."""
    token = csrf_token_in(client.get(path).text)
    return client.post(path, data={**data, "csrf_token": token}, follow_redirects=False)


def session_contents(response):
    cookie = re.search(r"session=([^;]+);", response.headers["set-cookie"]).group(1)
    payload = cookie.split(".")[0]
    return json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))


def user_count(db_session):
    return db_session.scalar(select(func.count()).select_from(User))


# ---- the login page ----

def test_the_login_page_shows_the_form(web):
    response = web.get("/login")

    assert response.status_code == 200
    html = response.text
    assert "Welcome back" in html
    assert 'for="field-identifier"' in html and "Phone number or email" in html
    assert 'type="password"' in html and 'autocomplete="current-password"' in html
    assert 'name="csrf_token"' in html
    assert "Sign in" in html
    assert "/static/auth/auth.css" in html


def test_the_login_page_has_no_app_shell(web):
    html = web.get("/login").text

    assert "<aside" not in html and "bottom-nav" not in html


def test_the_register_link_shows_only_while_registration_is_open(web, make_user):
    assert 'href="/register"' in web.get("/login").text

    make_user()

    assert 'href="/register"' not in web.get("/login").text


def test_signing_in_with_the_right_details_goes_to_the_home_page(web, make_user):
    make_user(phone="254722123456")

    response = open_and_post(web, "/login", {"identifier": "0722 123 456", "password": TEST_PASSWORD})

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_wrong_details_show_the_generic_message_and_keep_the_phone_or_email(web, make_user):
    make_user(phone="254722123456")

    response = open_and_post(web, "/login", {"identifier": "0722123456", "password": "river-stone-43"})

    assert response.status_code == 422
    assert GENERIC_FAILURE in response.text
    assert 'value="0722123456"' in response.text
    assert "river-stone-43" not in response.text


def test_an_unknown_account_gets_exactly_the_same_page(web, make_user):
    # AUTH-S2: nothing on the page says whether the account exists
    make_user(phone="254722123456")

    known = open_and_post(web, "/login", {"identifier": "0722123456", "password": "river-stone-43"})
    unknown = open_and_post(web, "/login", {"identifier": "0799000000", "password": "river-stone-43"})

    def without_token_and_typed_number(html):
        html = re.sub(r'name="csrf_token" value="[^"]+"', "", html)
        return html.replace("0722123456", "TYPED").replace("0799000000", "TYPED")

    assert known.status_code == unknown.status_code == 422
    assert without_token_and_typed_number(known.text) == without_token_and_typed_number(unknown.text)


def test_the_sixth_attempt_shows_the_same_message(web, make_user):
    make_user(phone="254722123456")
    for _ in range(5):
        open_and_post(web, "/login", {"identifier": "0722123456", "password": "river-stone-43"})

    response = open_and_post(web, "/login", {"identifier": "0722123456", "password": TEST_PASSWORD})

    assert response.status_code == 429
    assert GENERIC_FAILURE in response.text


def test_signing_in_without_the_form_token_is_refused(web, make_user):
    make_user(phone="254722123456")
    web.get("/login")

    response = web.post("/login", data={"identifier": "0722123456", "password": TEST_PASSWORD}, follow_redirects=False)

    assert response.status_code == 403


def test_a_signed_in_person_who_opens_login_goes_to_the_home_page(web, make_user, sign_in_as):
    sign_in_as(web, make_user())

    response = web.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize("notice,text", [("registered", "Your account is ready"), ("signed-out", "You have signed out")])
def test_the_login_page_shows_a_known_notice(web, notice, text):
    assert text in web.get(f"/login?notice={notice}").text


def test_an_unknown_notice_shows_nothing_from_the_address(web):
    html = web.get("/login?notice=<script>alert(1)</script>").text

    assert "alert(1)" not in html
    assert "message-success" not in html


# ---- the session after signing in (TASK-029: AUTH-S6, SES-1 to SES-3) ----

def test_signing_in_starts_a_new_session_holding_only_the_user_id_and_the_form_token(web, make_user):
    user = make_user(phone="254722123456")
    old_token = csrf_token_in(web.get("/login").text)

    response = web.post("/login", data={"identifier": "0722123456", "password": TEST_PASSWORD, "csrf_token": old_token},
                        follow_redirects=False)

    contents = session_contents(response)
    # a new session: the token from before signing in is gone
    assert contents == {"user_id": user.user_id}


def test_the_session_cookie_flags(web, make_user):
    make_user(phone="254722123456")

    response = open_and_post(web, "/login", {"identifier": "0722123456", "password": TEST_PASSWORD})

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "max-age=604800" in cookie        # 7 days, from the settings
    assert "secure" not in cookie            # Secure is for production only (next test)


def test_the_session_cookie_is_secure_in_production(db_session, make_user):
    from fastapi.testclient import TestClient

    from app.core.config import load_settings
    from app.core.database import get_db
    from app.main import create_app

    settings = load_settings({"APP_ENV": "production", "DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1:3306/x",
                              "SESSION_SECRET": "x" * 48, "SESSION_LIFETIME_DAYS": "3"})
    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: db_session
    https_client = TestClient(app, base_url="https://testserver")
    make_user(phone="254722123456")

    response = open_and_post(https_client, "/login", {"identifier": "0722123456", "password": TEST_PASSWORD})

    cookie = response.headers["set-cookie"].lower()
    assert "; secure" in cookie
    assert "max-age=259200" in cookie        # 3 days, from the settings


def test_the_session_is_only_rewritten_when_it_changes_so_its_lifetime_is_fixed(web, make_user, sign_in_as):
    # SES-3: a fixed lifetime from sign in, not one that restarts on every click
    sign_in_as(web, make_user())

    response = web.get("/account", follow_redirects=False)

    assert "set-cookie" not in response.headers


# ---- the register page (AUTH-2) ----

def test_the_register_page_shows_while_nobody_has_an_account(web):
    response = web.get("/register")

    assert response.status_code == 200
    assert "Create the owner account" in response.text
    for field in ("first_name", "last_name", "identifier", "password"):
        assert f'name="{field}"' in response.text


def test_registering_creates_the_owner_and_goes_to_sign_in(web, db_session):
    response = open_and_post(web, "/register", {"first_name": "Daniel", "last_name": "Kamau",
                                                "identifier": "0722123456", "password": TEST_PASSWORD})

    assert response.status_code == 303
    assert response.headers["location"] == "/login?notice=registered"
    owner = db_session.scalar(select(User))
    assert owner.role == "owner" and owner.phone == "254722123456"


def test_the_whole_first_visit_register_then_sign_in(web):
    open_and_post(web, "/register", {"first_name": "Daniel", "last_name": "Kamau",
                                     "identifier": "daniel@example.com", "password": TEST_PASSWORD})
    assert "Your account is ready" in web.get("/login?notice=registered").text

    response = open_and_post(web, "/login", {"identifier": "Daniel@Example.com", "password": TEST_PASSWORD})

    assert response.headers["location"] == "/"


def test_register_redirects_to_login_once_an_owner_exists(web, make_user):
    make_user()

    response = web.get("/register", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_second_registration_is_sent_to_login_and_creates_nobody(web, db_session):
    open_and_post(web, "/register", {"first_name": "Daniel", "last_name": "Kamau",
                                     "identifier": "0722123456", "password": TEST_PASSWORD})
    token = csrf_token_in(web.get("/login").text)

    response = web.post("/register", data={"first_name": "Someone", "last_name": "Else", "identifier": "0733123456",
                                           "password": TEST_PASSWORD, "csrf_token": token}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert user_count(db_session) == 1


def test_a_bad_field_shows_its_message_beside_it_and_keeps_what_was_typed(web, db_session):
    response = open_and_post(web, "/register", {"first_name": "Daniel", "last_name": "Kamau",
                                                "identifier": "07221", "password": TEST_PASSWORD})

    assert response.status_code == 422
    html = response.text
    assert 'id="field-identifier-error"' in html
    assert 'aria-invalid="true"' in html
    assert 'aria-describedby="identifier-help field-identifier-error"' in html
    assert 'value="Daniel"' in html and 'value="Kamau"' in html and 'value="07221"' in html
    assert TEST_PASSWORD not in html
    assert user_count(db_session) == 0


def test_a_weak_password_is_shown_beside_the_password(web):
    response = open_and_post(web, "/register", {"first_name": "Daniel", "last_name": "Kamau",
                                                "identifier": "0722123456", "password": "0722123456"})

    assert response.status_code == 422
    assert 'id="field-password-error"' in response.text
    assert "phone number as your password" in response.text


def test_the_pages_hold_no_inline_styles_or_scripts(web):
    for path in ("/login", "/register"):
        html = web.get(path).text
        assert " style=" not in html and "<style" not in html
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert not re.search(r"\son[a-z]+\s*=", html)


# ---- signing out (TASK-029, AUTH-S6) ----

def test_signing_out_clears_the_session_and_goes_to_login(web, make_user, sign_in_as):
    from tests.conftest import TEST_CSRF_TOKEN

    sign_in_as(web, make_user())

    response = web.post("/logout", data={"csrf_token": TEST_CSRF_TOKEN}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?notice=signed-out"
    assert response.headers["set-cookie"].startswith("session=null;")
    assert "expires=Thu, 01 Jan 1970" in response.headers["set-cookie"]


def test_after_signing_out_a_signed_in_page_goes_to_login(web, make_user, sign_in_as):
    from tests.conftest import TEST_CSRF_TOKEN

    sign_in_as(web, make_user())
    web.post("/logout", data={"csrf_token": TEST_CSRF_TOKEN})

    response = web.get("/login", follow_redirects=False)

    assert response.status_code == 200      # the login page itself, not a redirect home


def test_signing_out_is_recorded(web, make_user, sign_in_as, db_session):
    from tests.conftest import TEST_CSRF_TOKEN

    user = make_user(first_name="Daniel")
    sign_in_as(web, user)

    web.post("/logout", data={"csrf_token": TEST_CSRF_TOKEN})

    row = db_session.scalars(select(AuditLog).where(AuditLog.event_type == "auth.logout")).one()
    assert row.level == "info"
    assert row.actor_user_id == user.user_id
    assert row.summary == "Daniel (owner) signed out"


def test_signing_out_needs_the_form_token(web, make_user, sign_in_as):
    sign_in_as(web, make_user())

    response = web.post("/logout", follow_redirects=False)

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_signing_out_with_no_session_goes_to_login(web):
    response = web.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_signing_out_is_a_post_never_a_get(web, make_user, sign_in_as):
    sign_in_as(web, make_user())

    assert web.get("/logout").status_code == 405
