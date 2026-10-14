"""render_error() and the exception handlers (TASK-021, architecture.md section 5)."""

import logging
import re
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core import errors
from app.core.config import load_settings
from app.core.deps import protect_by_default
from app.core.errors import (
    AuthenticationRequiredError,
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    TooManyAttemptsError,
    render_error,
    unexpected_error,
)
from app.core.logging import request_id_var
from app.core.templating import templates
from app.main import create_app
from app.models import AuditLog

SECRET_TEXT = "SELECT password_hash FROM users WHERE phone = '0722123456' at C:\\secret\\file.py"

FORM_TEMPLATE = """{% extends "base.html" %}{% block title %}Form{% endblock %}
{% block content %}{% include "partials/form_errors.html" %}
<form><input id="field-first_name" name="first_name" value="{{ form.first_name }}"></form>{% endblock %}"""

TECHNICAL_WORDS = ["Traceback", "SELECT", "password_hash", "C:\\", "File \"", "Exception", "sqlalchemy", "0722123456"]


def make_app():
    settings = load_settings(
        {"APP_ENV": "test", "TEST_DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1:3306/x_test"}
    )
    app = create_app(settings)
    # These test routes are about something else, not sign in, so the sign in check is off
    app.dependency_overrides[protect_by_default] = lambda: None

    raisers = {
        "business": lambda: BusinessRuleError(
            "This is lower than last month's reading (200). Please check the number.", field="current_reading"
        ),
        "conflict": lambda: ConflictError("A reading for Mary Wanjiru already exists for August."),
        "notfound": lambda: NotFoundError(),
        "denied": lambda: PermissionDeniedError(),
        "toomany": lambda: TooManyAttemptsError("Please wait 15 minutes and try again."),
        "auth": lambda: AuthenticationRequiredError(),
        "internal": lambda: unexpected_error(RuntimeError(SECRET_TEXT), "record payment"),
    }

    @app.get("/raise/{kind}")
    def raise_error(kind: str):
        raise raisers[kind]()

    @app.get("/crash")
    def crash():
        raise RuntimeError(SECRET_TEXT)

    @app.get("/database-crash")
    def database_crash():
        raise OperationalError(SECRET_TEXT, {"phone": "0722123456"}, Exception("connection lost"))

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"item_id": item_id}

    @app.get("/search")
    def search(limit: int = 10):
        return {"limit": limit}

    @app.get("/controller-style")
    def controller_style(request: Request):
        # what a real controller does: catch the failure and call render_error itself
        try:
            raise BusinessRuleError("Please enter a first name.", field="first_name")
        except BusinessRuleError as exc:
            return render_error(
                request, exc, form_template="test_form.html", form_context={"form": {"first_name": "Mary"}}
            )

    @app.get("/form")
    def form(request: Request):
        return HTMLResponse(
            templates.env.from_string(
                '<form>{{ csrf_field(request) }}</form>'
            ).render(request=request)
        )

    @app.post("/change")
    def change():
        return {"changed": True}

    return app


@pytest.fixture
def client(db_session, tmp_path):
    """db_session makes the audit rows of a failure land in the test transaction."""
    (tmp_path / "test_form.html").write_text(FORM_TEMPLATE, encoding="utf-8")
    templates.env.loader.searchpath.append(str(tmp_path))
    # raise_server_exceptions=False lets the test read the 500 page the user would get
    yield TestClient(make_app(), raise_server_exceptions=False)
    templates.env.loader.searchpath.remove(str(tmp_path))


def htmx(path_headers=None):
    return {"HX-Request": "true", **(path_headers or {})}


def system_error_rows(db_session):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == "system.error")).all()


def assert_nothing_technical(text):
    for word in TECHNICAL_WORDS:
        assert word not in text, word


# ---- each exception: the right status code and a plain message, as a page ----

@pytest.mark.parametrize(
    "kind,status,heading,message",
    [
        ("business", 422, "That could not be saved", "This is lower than last month&#39;s reading (200)."),
        ("conflict", 409, "Something needs your attention", "A reading for Mary Wanjiru already exists for August."),
        ("notfound", 404, "We could not find that", "We could not find that."),
        ("denied", 403, "You do not have access", "You do not have access to this page."),
        ("toomany", 429, "Too many attempts", "Please wait 15 minutes and try again."),
        ("internal", 500, "Something went wrong on our side", "Something went wrong on our side. Please try again."),
    ],
)
def test_each_exception_gives_its_status_code_and_a_plain_page(client, kind, status, heading, message):
    response = client.get(f"/raise/{kind}")

    assert response.status_code == status
    assert heading in response.text
    assert message in response.text
    assert "<html" in response.text
    assert_nothing_technical(response.text)


def test_the_error_page_gives_a_way_back(client):
    response = client.get("/raise/denied")

    assert 'href="/"' in response.text
    assert "Back to home" in response.text


def test_the_error_page_uses_the_normal_layout_and_styles(client):
    response = client.get("/raise/notfound")

    assert "/static/shared/css/components.css" in response.text
    assert '<main id="main"' in response.text


def test_the_error_page_shows_the_signed_in_layout_when_a_user_is_known(client):
    app = client.app

    @app.middleware("http")
    async def pretend_signed_in(request, call_next):
        request.state.user = SimpleNamespace(first_name="Daniel", role="owner")
        return await call_next(request)

    response = client.get("/raise/denied")

    assert "Daniel (Owner)" in response.text
    assert 'class="sidebar"' in response.text


# ---- as a fragment for HTMX ----

@pytest.mark.parametrize("kind,status", [("business", 422), ("notfound", 404), ("denied", 403), ("internal", 500)])
def test_an_htmx_request_gets_a_fragment_with_the_same_status(client, kind, status):
    response = client.get(f"/raise/{kind}", headers=htmx())

    assert response.status_code == status
    assert "<html" not in response.text
    assert 'role="alert"' in response.text
    assert "message-error" in response.text
    assert_nothing_technical(response.text)


def test_the_fragment_carries_the_message(client):
    response = client.get("/raise/conflict", headers=htmx())

    assert "A reading for Mary Wanjiru already exists for August." in response.text


# ---- 401: sent to the login page ----

def test_a_page_that_needs_sign_in_redirects_to_login(client):
    response = client.get("/raise/auth", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_an_htmx_request_that_needs_sign_in_gets_an_hx_redirect(client):
    response = client.get("/raise/auth", headers=htmx(), follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


# ---- the safety net: things raised outside a controller's own try ----

def test_an_unknown_url_gives_a_404_page(client):
    response = client.get("/no-such-page")

    assert response.status_code == 404
    assert "We could not find that" in response.text
    assert_nothing_technical(response.text)


def test_a_wrong_method_gives_a_405_with_the_allow_header(client):
    response = client.post("/health")

    assert response.status_code == 405
    assert "That is not allowed here" in response.text
    assert "GET" in response.headers["allow"]


def test_a_non_numeric_id_in_the_url_is_a_404(client):
    # REQ-1: a record that does not exist is a 404
    response = client.get("/items/abc")

    assert response.status_code == 404
    assert "We could not find that" in response.text
    assert "int_parsing" not in response.text and "valid integer" not in response.text


def test_a_bad_query_value_gives_a_422_in_plain_words(client):
    response = client.get("/search?limit=abc")

    assert response.status_code == 422
    assert "Some details are missing or not valid" in response.text
    # the technical wording from the validation library must not reach the person
    assert "valid integer" not in response.text
    assert "Input should be" not in response.text
    assert_nothing_technical(response.text)


def test_a_validation_error_is_a_fragment_for_htmx(client):
    response = client.get("/search?limit=abc", headers=htmx())

    assert response.status_code == 422
    assert "<html" not in response.text
    assert "Some details are missing or not valid" in response.text


def test_a_missing_csrf_token_goes_through_the_real_handler_as_a_403_page(client):
    response = client.post("/change")

    assert response.status_code == 403
    assert "This page has expired" in response.text
    assert "<html" in response.text


def test_an_unexpected_exception_gives_a_safe_500_page(client):
    response = client.get("/crash")

    assert response.status_code == 500
    assert "Something went wrong on our side" in response.text
    assert_nothing_technical(response.text)


def test_a_database_error_gives_a_safe_500_page(client):
    response = client.get("/database-crash")

    assert response.status_code == 500
    assert "Something went wrong on our side" in response.text
    assert_nothing_technical(response.text)


def test_an_unexpected_exception_is_logged_with_its_stack_trace(client, caplog):
    with caplog.at_level(logging.ERROR, logger="app.errors"):
        client.get("/crash")

    assert any(record.exc_info and isinstance(record.exc_info[1], RuntimeError) for record in caplog.records)


# ---- the Reference and the audit row for a 500 ----

def test_a_500_page_shows_the_reference(db_session):
    request = SimpleNamespace(
        method="GET", url=SimpleNamespace(path="/readings"), headers={}, state=SimpleNamespace(),
        session={}, query_params={},
    )
    token = request_id_var.set("7f3a9c1e")
    try:
        response = render_error(request, unexpected_error(RuntimeError("x"), "save reading"))
    finally:
        request_id_var.reset(token)

    body = response.body.decode()
    assert response.status_code == 500
    assert "Reference:" in body and "7f3a9c1e" in body


def test_a_4xx_page_does_not_show_a_reference(client):
    token = request_id_var.set("7f3a9c1e")
    try:
        response = client.get("/raise/denied")
    finally:
        request_id_var.reset(token)

    assert "Reference" not in response.text


def test_a_500_records_system_error_without_the_exception_text(client, db_session):
    client.get("/crash")

    rows = system_error_rows(db_session)
    assert len(rows) == 1
    assert rows[0].level == "error"
    assert rows[0].details["error_type"] == "RuntimeError"
    assert rows[0].details["route"] == "/crash"
    assert rows[0].details["method"] == "GET"
    assert_nothing_technical(rows[0].summary + str(rows[0].details))


def test_a_database_error_records_the_exception_type(client, db_session):
    client.get("/database-crash")

    assert system_error_rows(db_session)[0].details["error_type"] == "OperationalError"


def test_an_error_raised_by_a_service_records_the_action(client, db_session):
    client.get("/raise/internal")

    row = system_error_rows(db_session)[0]
    assert row.details["action"] == "record payment"
    assert "record payment" in row.summary


@pytest.mark.parametrize("kind", ["business", "conflict", "notfound", "denied", "toomany"])
def test_a_refusal_does_not_record_system_error(client, db_session, kind):
    # refusals are recorded by the service that refused them, not by render_error
    client.get(f"/raise/{kind}")

    assert system_error_rows(db_session) == []


# ---- the form again, with the typed values kept ----

def test_a_failed_form_is_shown_again_with_the_message_and_the_typed_values(client):
    response = client.get("/controller-style")

    assert response.status_code == 422
    assert 'value="Mary"' in response.text
    assert "Please enter a first name." in response.text
    assert 'href="#field-first_name"' in response.text


# ---- when the error page itself fails ----

def test_a_broken_error_page_still_gives_a_plain_message_and_the_right_status(client, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("template exploded")

    monkeypatch.setattr(errors.templates, "TemplateResponse", broken)

    with caplog.at_level(logging.ERROR, logger="app.errors"):
        response = client.get("/raise/denied")

    assert response.status_code == 500
    assert response.text == "Something went wrong on our side. Please try again."
    assert "could not be rendered" in caplog.text


# ---- every error response carries the security headers ----

@pytest.mark.parametrize("path", ["/no-such-page", "/raise/denied", "/crash", "/items/abc"])
def test_error_responses_carry_the_security_headers(client, path):
    response = client.get(path)

    assert "Content-Security-Policy" in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_no_error_page_holds_inline_styles_or_scripts(client):
    for path in ("/raise/denied", "/crash", "/no-such-page"):
        html = client.get(path).text
        assert " style=" not in html and "<style" not in html
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
