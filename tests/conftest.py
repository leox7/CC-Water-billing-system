import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# This must run before any app module reads the settings. Tests always run in the
# "test" environment, which reads TEST_DATABASE_URL only, so a dev or real
# database can never be reached from a test run (DB-5). A variable already set
# in the environment wins over .env.
os.environ["APP_ENV"] = "test"
# Tests must not depend on the developer's .env, which may turn debug on.
os.environ["DEBUG"] = "false"
load_dotenv(PROJECT_ROOT / ".env")
# Lets tests that need no database import the app when .env does not exist yet.
# The database fixtures below fail with a clear message if this is still in use.
os.environ.setdefault(
    "TEST_DATABASE_URL",
    "mysql+pymysql://tests-not-configured:unset@127.0.0.1:3306/water_billing_test",
)


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real bcrypt, with 4 rounds instead of 12, so each hash takes milliseconds, not half
    a second. test_passwords.py checks that the app itself still uses 12."""
    from passlib.context import CryptContext

    monkeypatch.setattr(
        "app.core.security.password_context", CryptContext(schemes=["bcrypt"], bcrypt__rounds=4)
    )


@pytest.fixture(autouse=True)
def fresh_login_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The login attempt counter lives in memory; each test starts with none counted."""
    from app.core.security import LoginAttemptLimiter

    monkeypatch.setattr("app.core.security.login_limiter", LoginAttemptLimiter())


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """SMS-6, SEC-7: no test can reach Africa's Talking, or anything else on the internet.
    Every real network request fails as if the network were down. The test client and the
    stand-in transports of the provider tests never come here."""
    import httpx

    def refuse(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Tests may not reach the network ({request.url.host})", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


class FakeSmsProvider:
    """Stands in for providers.send_sms: keeps what was sent, or fails with `failure`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.failure: str | None = None

    def send(self, settings: Any, phone: str, text: str) -> str:
        from app.features.messaging.providers import SmsSendError

        if self.failure:
            raise SmsSendError(self.failure)
        self.sent.append((phone, text))
        return f"ATPid_test_{len(self.sent)}"


@pytest.fixture
def sms(monkeypatch: pytest.MonkeyPatch) -> FakeSmsProvider:
    """The SMS provider for a test. Set sms.failure = "reason" to make the next sends fail."""
    fake = FakeSmsProvider()
    monkeypatch.setattr("app.features.messaging.providers.send_sms", fake.send)
    return fake


@pytest.fixture(autouse=True)
def audit_writes_are_rolled_back(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Audit problems (rejections and errors) are normally committed in their own session.
    In a test that would leave rows behind, so every test writes them into a transaction
    that is rolled back at the end. A test that needs the real separate session says so
    with @pytest.mark.real_audit_session and cleans up after itself."""
    if request.node.get_closest_marker("real_audit_session"):
        yield
        return

    opened: list[tuple[Any, Any]] = []

    def open_rolled_back_session() -> Session:
        from app.core.database import get_engine

        connection = get_engine().connect()
        transaction = connection.begin()
        opened.append((connection, transaction))
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    monkeypatch.setattr("app.features.audit.service._open_problem_session", open_rolled_back_session)
    yield
    for connection, transaction in opened:
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    from app.core.database import get_engine

    engine = get_engine()
    # DB-5: never run against a database that could hold real data
    if not (engine.url.database or "").endswith("_test"):
        pytest.fail("The test database name must end in _test.", pytrace=False)
    try:
        with engine.connect():
            pass
    except OperationalError:
        pytest.fail(
            "Cannot connect to the test database. Run scripts/dev_db_setup.sql as the "
            "MySQL admin, then set TEST_DATABASE_URL in .env (see .env.example).",
            pytrace=False,
        )
    # Bring the schema up to date. It runs the real migrations, so they are tested too.
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(alembic_config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """A session whose work is rolled back at the end, so tests never leave data."""
    connection = test_engine.connect()
    outer_transaction = connection.begin()
    # a commit inside the test only releases a savepoint, not the outer transaction
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    # Audit problems are normally saved in their own session. Here that session joins
    # the test transaction, so the test can read the row and nothing is left behind.
    monkeypatch.setattr(
        "app.features.audit.service._open_problem_session",
        lambda: Session(bind=connection, join_transaction_mode="create_savepoint"),
    )
    yield session
    session.close()
    outer_transaction.rollback()
    connection.close()


TEST_SETTINGS = {
    "APP_ENV": "test",
    "TEST_DATABASE_URL": "mysql+pymysql://unused:unused@127.0.0.1:3306/unused_test",
}
TEST_PASSWORD = "river-stone-42"
TEST_CSRF_TOKEN = "test-csrf-token"


@pytest.fixture
def web(db_session: Session) -> TestClient:
    """The real app, with every request using the test transaction, for tests that sign
    in and use pages. raise_server_exceptions=False shows the 500 page a person would see."""
    from app.core.config import load_settings
    from app.core.database import get_db
    from app.main import create_app

    app = create_app(load_settings(TEST_SETTINGS))
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def make_user(db_session: Session):
    """Adds a user. Defaults to an active owner with TEST_PASSWORD."""
    from app.core.security import hash_password
    from app.models import User

    phone_numbers = iter(range(254700000001, 254700000999))

    def make(role: str = "owner", status: str = "active", first_name: str = "Daniel",
             phone: str | None = "next", email: str | None = None, password: str = TEST_PASSWORD) -> User:
        user = User(
            first_name=first_name,
            last_name="Test",
            phone=str(next(phone_numbers)) if phone == "next" else phone,
            email=email,
            password_hash=hash_password(password),
            role=role,
            status=status,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return make


@pytest.fixture
def make_customer(db_session: Session):
    """Adds a customer. Defaults to an active Mary Wanjiru at Ksh 130 a unit and Ksh 150 a
    month, each with the next meter number (MTR-0001, MTR-0002...)."""
    from decimal import Decimal

    from app.models import Customer

    meter_numbers = iter(range(1, 999))

    def make(first_name: str = "Mary", last_name: str = "Wanjiru", status: str = "active",
             rate_per_unit: str = "130.00", service_charge: str = "150.00",
             phone: str = "254722123456") -> Customer:
        customer = Customer(
            first_name=first_name, last_name=last_name, phone=phone, location="Kiawara Estate",
            meter_number=f"MTR-{next(meter_numbers):04d}", rate_per_unit=Decimal(rate_per_unit),
            service_charge=Decimal(service_charge), status=status,
        )
        db_session.add(customer)
        db_session.flush()
        return customer

    return make


def session_cookie(data: dict[str, Any]) -> str:
    """A session cookie signed exactly as Starlette's SessionMiddleware signs one."""
    import base64
    import json

    import itsdangerous

    from app.core.config import load_settings

    signer = itsdangerous.TimestampSigner(load_settings(TEST_SETTINGS).session_secret)
    return signer.sign(base64.b64encode(json.dumps(data).encode("utf-8"))).decode("utf-8")


def set_session(client: TestClient, data: dict[str, Any]) -> None:
    """Puts a signed session cookie in the client, for the test server's own domain, so a
    cookie the server later clears is really gone (as it would be in a browser)."""
    client.cookies.set("session", session_cookie(data), domain="testserver.local", path="/")


@pytest.fixture
def sign_in_as():
    """Gives a client a valid session for a user, with a known CSRF token.

    Posts then send the token: data={"csrf_token": TEST_CSRF_TOKEN} or the X-CSRF-Token header."""

    def sign_in(client: TestClient, user: Any) -> None:
        set_session(client, {"user_id": user.user_id, "csrf_token": TEST_CSRF_TOKEN})

    return sign_in


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    # Not used as a "with" block, so the app's startup (system.started) does not run
    # for every test. The startup has its own test.
    return TestClient(app)
