from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.main import create_app


def settings_for(**overrides: str):
    environ = {"APP_ENV": "test", "TEST_DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1:3306/x_test"}
    environ.update(overrides)
    return load_settings(environ)


def test_health_answers_ok_without_login(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.text == "ok"


def test_interactive_docs_are_off_when_not_in_debug(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_interactive_docs_are_on_in_debug():
    debug_client = TestClient(create_app(settings_for(DEBUG="true")))

    assert debug_client.get("/openapi.json").status_code == 200
    assert debug_client.get("/docs").status_code == 200
