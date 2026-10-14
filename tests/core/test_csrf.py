"""CSRF protection (SECURITY.md section 4, TASK-020)."""

import re
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import load_settings
from app.core.deps import protect_by_default
from app.core.errors import AppError
from app.core.security import CSRF_HEADER
from app.core.templating import csrf_field, get_csrf_token, templates
from app.main import create_app
from app.models import AuditLog

PAGE = """{% extends "base.html" %}{% block title %}Page{% endblock %}
{% block content %}<form method="post" action="/change">{{ csrf_field(request) }}</form>{% endblock %}"""


def make_app():
    settings = load_settings(
        {"APP_ENV": "test", "TEST_DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1:3306/x_test"}
    )
    app = create_app(settings)
    # These test routes are about something else, not sign in, so the sign in check is off
    app.dependency_overrides[protect_by_default] = lambda: None
    changes = []

    # Stand-in for the real error handlers, which a later task adds
    @app.exception_handler(AppError)
    def show_error(request: Request, exc: AppError):
        return PlainTextResponse(exc.message, status_code=exc.status_code)

    @app.get("/form")
    def form(request: Request):
        return HTMLResponse(templates.env.from_string(PAGE).render(request=request, user=None))

    @app.get("/page-with-shell")
    def page_with_shell(request: Request):
        user = SimpleNamespace(first_name="Daniel", role="owner")
        return HTMLResponse(templates.env.from_string(PAGE).render(request=request, user=user))

    @app.post("/change")
    def change():
        changes.append("post")
        return PlainTextResponse("changed")

    @app.put("/change")
    def change_put():
        changes.append("put")
        return PlainTextResponse("changed")

    @app.patch("/change")
    def change_patch():
        changes.append("patch")
        return PlainTextResponse("changed")

    @app.delete("/change")
    def change_delete():
        changes.append("delete")
        return PlainTextResponse("changed")

    return app, changes


@pytest.fixture
def csrf(db_session):
    """A client for a test app, and the list of changes its routes made.

    db_session is here so the audit row for a failed check is written to the test
    transaction and rolled back afterwards."""
    app, changes = make_app()
    return TestClient(app), changes


def token_from(client):
    html = client.get("/form").text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def audit_rows(db_session):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == "auth.csrf_failed")).all()


def test_a_post_without_a_token_is_refused_and_changes_nothing(csrf):
    client, changes = csrf

    response = client.post("/change")

    assert response.status_code == 403
    assert changes == []
    assert "reload it and try again" in response.text


def test_a_failed_check_is_recorded_in_the_audit_log(csrf, db_session):
    client, _ = csrf

    client.post("/change")

    rows = audit_rows(db_session)
    assert len(rows) == 1
    assert rows[0].level == "warning"
    assert rows[0].details == {"route": "/change", "method": "POST"}


def test_a_post_with_a_wrong_form_token_is_refused(csrf):
    client, changes = csrf
    token_from(client)

    response = client.post("/change", data={"csrf_token": "not-the-token"})

    assert response.status_code == 403
    assert changes == []


def test_a_post_with_the_right_form_token_works(csrf):
    client, changes = csrf
    token = token_from(client)

    response = client.post("/change", data={"csrf_token": token})

    assert response.status_code == 200
    assert changes == ["post"]


def test_a_post_with_the_right_header_token_works_for_htmx(csrf):
    client, changes = csrf
    token = token_from(client)

    response = client.post("/change", headers={CSRF_HEADER: token})

    assert response.status_code == 200
    assert changes == ["post"]


def test_a_post_with_a_wrong_header_token_is_refused(csrf):
    client, changes = csrf
    token_from(client)

    response = client.post("/change", headers={CSRF_HEADER: "wrong"})

    assert response.status_code == 403
    assert changes == []


def test_a_token_from_another_session_is_refused(csrf):
    client, changes = csrf
    other_persons_token = token_from(TestClient(client.app))
    token_from(client)

    response = client.post("/change", data={"csrf_token": other_persons_token})

    assert response.status_code == 403
    assert changes == []


def test_a_token_is_useless_without_a_session_cookie(csrf):
    client, changes = csrf
    token = token_from(client)
    client.cookies.clear()

    response = client.post("/change", data={"csrf_token": token})

    assert response.status_code == 403
    assert changes == []


def test_a_json_post_without_a_token_is_refused(csrf):
    client, changes = csrf
    token_from(client)

    response = client.post("/change", json={"csrf_token": "anything"})

    assert response.status_code == 403
    assert changes == []


def test_a_token_with_odd_characters_is_a_403_not_a_crash(csrf):
    client, changes = csrf
    token_from(client)

    # raw bytes in a header, as anyone can send them
    response = client.post("/change", headers={CSRF_HEADER.encode(): "caf\u00e9".encode("utf-8")})

    assert response.status_code == 403
    assert changes == []


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_changing_methods_need_the_token_too(csrf, method):
    client, changes = csrf
    token = token_from(client)

    refused = getattr(client, method)("/change")
    allowed = getattr(client, method)("/change", headers={CSRF_HEADER: token})

    assert refused.status_code == 403
    assert allowed.status_code == 200
    assert changes == [method]


def test_reading_a_page_needs_no_token(csrf):
    client, _ = csrf

    assert client.get("/form").status_code == 200
    assert client.get("/health").status_code == 200


def test_the_token_stays_the_same_for_one_person(csrf):
    client, _ = csrf

    assert token_from(client) == token_from(client)


def test_two_people_get_different_tokens(csrf):
    client, _ = csrf

    assert token_from(client) != token_from(TestClient(client.app))


def test_the_page_layout_sets_the_same_token_for_htmx(csrf):
    client, _ = csrf

    html = client.get("/page-with-shell").text
    header_token = re.search(r"hx-headers='\{\"X-CSRF-Token\": \"([^\"]+)\"\}'", html).group(1)

    assert header_token == token_from(client)
    # the sign out form and the page's own form both carry it
    assert html.count(f'name="csrf_token" value="{header_token}"') == 2


def test_the_token_is_escaped_in_the_field():
    request = SimpleNamespace(session={"csrf_token": '"><script>'})

    assert "<script>" not in csrf_field(request)


def test_get_csrf_token_makes_one_the_first_time_and_keeps_it():
    request = SimpleNamespace(session={})

    token = get_csrf_token(request)

    assert len(token) >= 32
    assert get_csrf_token(request) == token
    assert request.session["csrf_token"] == token
