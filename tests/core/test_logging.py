"""Request ID, JSON log lines and system.started (TASK-022, AUD-4, AUDIT_LOG.md section 6)."""

import json
import logging
import re
import sys

import pytest
from fastapi import Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.core.deps import protect_by_default
from app.core.logging import (
    JsonFormatter,
    client_ip_var,
    configure_logging,
    mask_phone_numbers,
    request_id_var,
)
from app.main import create_app
from app.models import AuditLog

REQUEST_ID_FORMAT = re.compile(r"^[0-9a-f]{8}$")


def make_app():
    settings = load_settings(
        {"APP_ENV": "test", "TEST_DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1:3306/x_test"}
    )
    app = create_app(settings)
    # These test routes are about something else, not sign in, so the sign in check is off
    app.dependency_overrides[protect_by_default] = lambda: None

    @app.get("/crash")
    def crash():
        raise RuntimeError("database password is hunter2 for 0722123456")

    @app.get("/context")
    def context(request: Request):
        # a plain (threadpool) route sees the same request id as the middleware made
        return {"request_id": request_id_var.get(), "client_ip": client_ip_var.get()}

    @app.get("/signed-in")
    def signed_in(request: Request):
        request.session["user_id"] = 7
        return PlainTextResponse("ok")

    return app


@pytest.fixture
def client(db_session):
    # db_session: the audit rows written during a failing request join the test transaction
    return TestClient(make_app(), raise_server_exceptions=False)


def log_lines(capsys):
    """The JSON lines written to stdout so far, as dicts."""
    lines = []
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("{"):
            lines.append(json.loads(line))
    return lines


def request_lines(capsys, path):
    return [line for line in log_lines(capsys) if line.get("path") == path]


# ---- the X-Request-ID header ----

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/health"),
        ("get", "/no-such-page"),
        ("post", "/health"),
        ("get", "/crash"),
        ("get", "/static/shared/css/tokens.css"),
        ("get", "/static/shared/nothing.css"),
    ],
)
def test_every_response_has_a_request_id_header(client, method, path):
    response = getattr(client, method)(path)

    assert REQUEST_ID_FORMAT.match(response.headers["X-Request-ID"])


def test_a_redirect_has_a_request_id_header(client):
    # a 303 like the one sent to the login page
    response = client.get("/static", follow_redirects=False)

    assert REQUEST_ID_FORMAT.match(response.headers["X-Request-ID"])


def test_each_request_gets_its_own_id(client):
    ids = {client.get("/health").headers["X-Request-ID"] for _ in range(20)}

    assert len(ids) == 20


def test_an_id_sent_by_the_client_is_ignored(client):
    response = client.get("/health", headers={"X-Request-ID": "forged-by-client"})

    assert response.headers["X-Request-ID"] != "forged-by-client"
    assert REQUEST_ID_FORMAT.match(response.headers["X-Request-ID"])


def test_the_request_id_and_client_ip_are_available_to_the_code_handling_the_request(client):
    response = client.get("/context")

    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert response.json()["client_ip"] == "testclient"


# ---- one id ties the header, the log line, the audit row and the page together ----

def test_the_500_page_shows_the_same_reference_as_the_header(client):
    response = client.get("/crash")
    request_id = response.headers["X-Request-ID"]

    assert response.status_code == 500
    assert "Reference:" in response.text
    assert request_id in response.text


def test_the_audit_row_has_the_request_id_of_its_request(client, db_session):
    response = client.get("/crash")

    row = db_session.scalars(select(AuditLog).where(AuditLog.event_type == "system.error")).one()
    assert row.request_id == response.headers["X-Request-ID"]
    assert row.ip_address == "testclient"


def test_the_log_line_has_the_request_id_of_its_request(client, capsys):
    response = client.get("/health")

    line = request_lines(capsys, "/health")[0]
    assert line["request_id"] == response.headers["X-Request-ID"]


def test_the_error_log_line_with_the_stack_trace_has_the_same_request_id(client, capsys):
    response = client.get("/crash")

    error_lines = [line for line in log_lines(capsys) if line.get("exc_type") == "RuntimeError"]
    assert len(error_lines) >= 1
    assert error_lines[0]["request_id"] == response.headers["X-Request-ID"]
    assert "stack_trace" in error_lines[0]


# ---- the access log line ----

def test_each_request_writes_one_json_line_with_the_documented_fields(client, capsys):
    client.get("/health")

    lines = request_lines(capsys, "/health")
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == {"time", "level", "request_id", "user_id", "method", "path", "status", "ms"}
    assert line["method"] == "GET"
    assert line["status"] == 200
    assert line["level"] == "info"
    assert line["user_id"] is None
    assert isinstance(line["ms"], int) and line["ms"] >= 0


def test_the_time_is_east_africa_time(client, capsys):
    client.get("/health")

    assert request_lines(capsys, "/health")[0]["time"].endswith("+03:00")


def test_the_signed_in_user_id_is_logged(client, capsys):
    client.get("/signed-in")

    assert request_lines(capsys, "/signed-in")[0]["user_id"] == 7


def test_the_level_follows_the_status(client, capsys):
    client.get("/no-such-page")
    client.get("/crash")

    # read the output once: reading it empties the buffer
    access_lines = {line["path"]: line for line in log_lines(capsys) if "path" in line}
    assert access_lines["/no-such-page"]["level"] == "warning"
    assert access_lines["/crash"]["level"] == "error"


def test_static_files_are_not_logged(client, capsys):
    client.get("/static/shared/css/tokens.css")

    assert request_lines(capsys, "/static/shared/css/tokens.css") == []


def test_a_crash_never_writes_a_full_phone_number_to_the_log(client, capsys):
    # the crashing route puts a phone number in its exception message
    client.get("/crash")

    output = capsys.readouterr().out
    assert "exc_type" in output
    assert "0722123456" not in output
    assert "[phone removed]" in output


# ---- the formatter ----

def make_record(message="hello", exc_info=None, fields=None):
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, message, None, exc_info)
    if fields:
        record.fields = fields
    return record


def test_a_normal_log_line_is_one_json_object_on_one_line():
    line = JsonFormatter().format(make_record("Started"))

    assert "\n" not in line
    entry = json.loads(line)
    assert entry["message"] == "Started"
    assert entry["level"] == "info"
    assert entry["logger"] == "app.test"


def test_an_error_line_adds_the_type_message_and_stack_trace():
    try:
        raise ValueError("bad value")
    except ValueError:
        record = make_record("Failed", exc_info=sys.exc_info())

    line = JsonFormatter().format(record)

    assert "\n" not in line
    entry = json.loads(line)
    assert entry["exc_type"] == "ValueError"
    assert entry["exc_message"] == "bad value"
    assert "Traceback" in entry["stack_trace"]
    assert "ValueError" in entry["stack_trace"]


def test_phone_numbers_are_masked_in_log_lines():
    try:
        raise ValueError("could not text 254722123456")
    except ValueError:
        record = make_record("Sending to 0722123456 and +254733987654", exc_info=sys.exc_info())

    line = JsonFormatter().format(record)

    for number in ("0722123456", "254722123456", "254733987654"):
        assert number not in line
    assert "[phone removed]" in line


@pytest.mark.parametrize(
    "text,expected",
    [
        ("call 0722123456 now", "call [phone removed] now"),
        ("254722123456", "[phone removed]"),
        ("+254112345678", "[phone removed]"),
        ("meter 12345678901234", "meter 12345678901234"),
        ("Ksh 2,100", "Ksh 2,100"),
    ],
)
def test_mask_phone_numbers(text, expected):
    assert mask_phone_numbers(text) == expected


def test_configuring_logging_twice_adds_only_one_handler():
    configure_logging()
    configure_logging()

    handlers = [h for h in logging.getLogger("app").handlers if type(h).__name__ == "_StdoutHandler"]
    assert len(handlers) == 1


# ---- system.started ----

def test_the_app_records_system_started_when_it_starts(db_session, monkeypatch):
    connection = db_session.get_bind()
    monkeypatch.setattr(
        "app.main.get_session_factory",
        lambda: (lambda: Session(bind=connection, join_transaction_mode="create_savepoint")),
    )

    with TestClient(make_app()):
        pass

    row = db_session.scalars(select(AuditLog).where(AuditLog.event_type == "system.started")).one()
    assert row.level == "info"
    assert row.details == {"sms_backend": "console", "environment": "test"}
    assert row.actor_user_id is None


def test_the_app_still_starts_when_the_database_is_down(monkeypatch, caplog):
    def database_is_down():
        raise OperationalError("connect", {}, Exception("database is down"))

    monkeypatch.setattr("app.main.get_session_factory", lambda: database_is_down)

    with caplog.at_level(logging.ERROR, logger="app.main"):
        with TestClient(make_app()) as started_client:
            assert started_client.get("/health").status_code == 200

    assert "could not be recorded" in caplog.text
