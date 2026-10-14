import pytest
from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.core.deps import protect_by_default
from app.core.security import (
    CONTENT_SECURITY_POLICY,
    build_security_headers,
)
from app.main import create_app

EXPECTED_HEADERS = [
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Permissions-Policy",
]


def settings_for(**overrides: str):
    environ = {"APP_ENV": "test", "TEST_DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1:3306/x_test"}
    environ.update(overrides)
    return load_settings(environ)


def test_the_policy_is_exactly_the_one_in_security_md():
    assert CONTENT_SECURITY_POLICY == (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
    )


def test_the_policy_allows_no_inline_scripts_or_styles_and_nothing_from_other_sites():
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY


@pytest.mark.parametrize(
    "path,status",
    [
        ("/health", 200),
        ("/no-such-page", 404),
        ("/static/shared/css/tokens.css", 200),
        ("/static/shared/js/app.js", 200),
        ("/static/shared/img/logo-full.png", 200),
        ("/static/shared/nothing.css", 404),
    ],
)
def test_every_response_carries_the_headers(client, path, status):
    response = client.get(path)

    assert response.status_code == status
    for name in EXPECTED_HEADERS:
        assert name in response.headers, f"{name} missing on {path}"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"


def test_a_wrong_method_response_carries_the_headers(client):
    response = client.post("/health")

    assert response.status_code == 405
    assert "Content-Security-Policy" in response.headers


def test_debug_pages_carry_the_headers_too():
    debug_client = TestClient(create_app(settings_for(DEBUG="true")))

    assert "Content-Security-Policy" in debug_client.get("/docs").headers


def test_an_unhandled_error_response_carries_the_headers_too():
    app = create_app(settings_for())
    # this test route is about the headers, not sign in, so the sign in check is off
    app.dependency_overrides[protect_by_default] = lambda: None

    @app.get("/boom")
    def boom():
        raise RuntimeError("something broke")

    # raise_server_exceptions=False lets the test read the 500 the user would get
    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    for name in EXPECTED_HEADERS:
        assert name in response.headers


def test_hsts_is_off_until_it_is_turned_on_in_the_settings(client):
    assert "Strict-Transport-Security" not in client.get("/health").headers


def test_hsts_uses_the_configured_lifetime():
    hsts_client = TestClient(create_app(settings_for(HSTS_SECONDS="300")))

    assert hsts_client.get("/health").headers["Strict-Transport-Security"] == "max-age=300"


def test_the_header_list_is_built_in_one_place():
    assert set(build_security_headers()) == set(EXPECTED_HEADERS)
    assert "Strict-Transport-Security" in build_security_headers(hsts_seconds=60)
