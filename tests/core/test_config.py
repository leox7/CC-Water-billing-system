import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import (
    DEVELOPMENT_SESSION_SECRET,
    EXAMPLE_SESSION_SECRET,
    ConfigError,
    load_settings,
)

REAL_SECRET = "x" * 48
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def development_environ(**overrides: str) -> dict[str, str]:
    environ = {"DATABASE_URL": "mysql+pymysql://user:pw@127.0.0.1:3306/water_billing_dev"}
    environ.update(overrides)
    return environ


def production_environ(**overrides: str) -> dict[str, str]:
    environ = {
        "APP_ENV": "production",
        "DATABASE_URL": "mysql+pymysql://user:pw@127.0.0.1:3306/water_billing",
        "SESSION_SECRET": REAL_SECRET,
    }
    environ.update(overrides)
    return environ


def test_defaults_in_development():
    settings = load_settings(development_environ())

    assert settings.app_env == "development"
    assert settings.debug is False
    assert settings.session_secret == DEVELOPMENT_SESSION_SECRET
    assert settings.session_lifetime_days == 7
    assert settings.sms_backend == "console"
    assert settings.hsts_seconds == 0
    assert settings.default_rate_per_unit == Decimal("130.00")
    assert settings.default_service_charge == Decimal("150.00")
    assert settings.is_production is False


def test_values_are_read_from_the_environment():
    settings = load_settings(
        development_environ(
            DEBUG="true",
            SESSION_LIFETIME_DAYS="3",
            HSTS_SECONDS="300",
            DEFAULT_RATE_PER_UNIT="140.50",
            DEFAULT_SERVICE_CHARGE="200",
        )
    )

    assert settings.debug is True
    assert settings.session_lifetime_days == 3
    assert settings.hsts_seconds == 300
    assert settings.default_rate_per_unit == Decimal("140.50")
    assert settings.default_service_charge == Decimal("200")


def test_production_accepts_a_real_secret():
    settings = load_settings(production_environ())

    assert settings.is_production is True
    assert settings.session_secret == REAL_SECRET


@pytest.mark.parametrize("secret", ["", EXAMPLE_SESSION_SECRET, "too-short"])
def test_production_refuses_a_missing_example_or_short_secret(secret):
    with pytest.raises(ConfigError, match="SESSION_SECRET"):
        load_settings(production_environ(SESSION_SECRET=secret))


def test_production_refuses_debug_on():
    with pytest.raises(ConfigError, match="DEBUG"):
        load_settings(production_environ(DEBUG="true"))


def test_missing_database_url_is_refused():
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings({})


def test_test_environment_uses_only_the_test_database_url():
    # DB-5: the dev or real DATABASE_URL must never be used by a test run
    environ = development_environ(
        APP_ENV="test",
        TEST_DATABASE_URL="mysql+pymysql://user:pw@127.0.0.1:3306/water_billing_test",
    )

    settings = load_settings(environ)

    assert settings.database_url.endswith("/water_billing_test")


def test_test_environment_without_test_database_url_is_refused():
    with pytest.raises(ConfigError, match="TEST_DATABASE_URL"):
        load_settings(development_environ(APP_ENV="test"))


def test_unknown_app_env_is_refused():
    with pytest.raises(ConfigError, match="APP_ENV"):
        load_settings(development_environ(APP_ENV="staging"))


def test_unknown_sms_backend_is_refused():
    with pytest.raises(ConfigError, match="SMS_BACKEND"):
        load_settings(development_environ(SMS_BACKEND="whatsapp"))


@pytest.mark.parametrize("app_env", ["development", "production"])
@pytest.mark.parametrize("backend", ["console", "phone"])
def test_console_and_phone_start_with_no_africastalking_settings(app_env, backend):
    # SMS-1, SMS-6: neither calls a provider, so neither needs AT_USERNAME, AT_API_KEY or AT_SENDER_ID
    environ = production_environ if app_env == "production" else development_environ

    settings = load_settings(environ(SMS_BACKEND=backend))

    assert settings.sms_backend == backend
    assert (settings.at_username, settings.at_api_key, settings.at_sender_id) == ("", "", "")


def test_the_phone_backend_ignores_a_sandbox_username():
    assert load_settings(development_environ(SMS_BACKEND="phone", AT_USERNAME="sandbox")).sms_backend == "phone"


@pytest.mark.parametrize("backend", ["console", "phone", "africastalking"])
def test_each_of_the_three_backends_starts(backend):
    environ = production_environ(**live_sms(SMS_BACKEND=backend))

    assert load_settings(environ).sms_backend == backend


def test_phone_is_read_whatever_its_case():
    assert load_settings(development_environ(SMS_BACKEND=" Phone ")).sms_backend == "phone"


def live_sms(**overrides: str) -> dict[str, str]:
    environ = {"SMS_BACKEND": "africastalking", "AT_USERNAME": "ccwater", "AT_API_KEY": "k", "AT_SENDER_ID": "CCH2O"}
    environ.update(overrides)
    return environ


def test_real_sms_backend_is_refused_outside_production():
    # SMS-6: a real SMS is never sent from a dev or test run
    with pytest.raises(ConfigError, match="production only") as raised:
        load_settings(development_environ(**live_sms()))

    assert "SMS_BACKEND=console" in str(raised.value)


@pytest.mark.parametrize("app_env", ["development", "production"])
def test_the_sandbox_is_refused_because_the_bulk_endpoint_has_none(app_env):
    environ = (production_environ if app_env == "production" else development_environ)(**live_sms(AT_USERNAME="sandbox"))

    with pytest.raises(ConfigError) as raised:
        load_settings(environ)

    assert str(raised.value) == ("Africa's Talking's bulk SMS endpoint has no sandbox, so AT_USERNAME=sandbox "
                                 "cannot be used. Use SMS_BACKEND=console outside production.")


def test_the_console_backend_ignores_a_sandbox_username():
    assert load_settings(development_environ(AT_USERNAME="sandbox")).sms_backend == "console"


def test_real_sms_backend_needs_credentials_in_production():
    with pytest.raises(ConfigError, match="AT_USERNAME"):
        load_settings(production_environ(SMS_BACKEND="africastalking", AT_SENDER_ID="CCH2O"))


def test_real_sms_backend_needs_a_sender_id_in_production():
    # senderId is required on every bulk request, so the app does not start without one
    with pytest.raises(ConfigError) as raised:
        load_settings(production_environ(**live_sms(AT_SENDER_ID="")))

    assert str(raised.value) == ("AT_SENDER_ID is needed for africastalking. Set it to the sender ID "
                                 "Africa's Talking approved.")


def test_real_sms_backend_is_allowed_in_production_with_credentials_and_a_sender_id():
    settings = load_settings(production_environ(**live_sms()))

    assert settings.sms_backend == "africastalking"
    assert settings.at_sender_id == "CCH2O"


@pytest.mark.parametrize("app_env", ["development", "production"])
def test_the_console_backend_works_without_a_sender_id(app_env):
    environ = production_environ() if app_env == "production" else development_environ()

    assert load_settings(environ).at_sender_id == ""


@pytest.mark.parametrize("sender_id", ["C", "CC_H2O-2026", "cch2o", "  CCH2O  "])
def test_a_good_sender_id_is_accepted(sender_id):
    settings = load_settings(production_environ(**live_sms(AT_SENDER_ID=sender_id)))

    assert settings.at_sender_id == sender_id.strip()


@pytest.mark.parametrize("sender_id", ["CC H2O", "CCH2OWATER12", "C&C", "CCH2O!", "C.C", "MAJI\tYA"])
@pytest.mark.parametrize("backend", ["console", "africastalking"])
def test_a_bad_sender_id_stops_the_app(sender_id, backend):
    environ = production_environ(**live_sms(SMS_BACKEND=backend, AT_SENDER_ID=sender_id))

    with pytest.raises(ConfigError) as raised:
        load_settings(environ)

    assert str(raised.value) == "AT_SENDER_ID must be 1 to 11 characters: letters, digits, - or _, with no spaces."


@pytest.mark.parametrize(
    "name,value",
    [
        ("SESSION_LIFETIME_DAYS", "seven"),
        ("SESSION_LIFETIME_DAYS", "0"),
        ("HSTS_SECONDS", "-1"),
        ("DEFAULT_RATE_PER_UNIT", "abc"),
        ("DEFAULT_RATE_PER_UNIT", "-5"),
        ("DEFAULT_SERVICE_CHARGE", "NaN"),
    ],
)
def test_bad_numbers_are_refused(name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings(development_environ(**{name: value}))


def test_secrets_are_not_in_the_settings_repr():
    settings = load_settings(production_environ(**live_sms(AT_API_KEY="key-value-123")))

    text = repr(settings)

    assert REAL_SECRET not in text
    assert "key-value-123" not in text
    assert "pw@" not in text


@pytest.mark.parametrize("typo", ["whatsapp", "consol", "console2", "africa", "sms", "phones", "mobile"])
def test_an_unknown_sms_backend_stops_the_app_with_a_clear_message(typo):
    # SMS-6: a typo must never fall back to sending real SMS, or to silence
    with pytest.raises(ConfigError) as raised:
        load_settings(development_environ(SMS_BACKEND=typo))

    assert str(raised.value) == "SMS_BACKEND must be one of: console, phone, africastalking."


def test_an_unknown_sms_backend_stops_the_app_from_starting_at_all():
    # the app builds its settings when it starts, so this is what uvicorn would hit
    import subprocess
    import sys

    environ = {"APP_ENV": "development", "SMS_BACKEND": "whatsapp", "SESSION_SECRET": "x" * 40,
               "DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1/none", "PATH": os.environ.get("PATH", ""),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    started = subprocess.run([sys.executable, "-c", "import app.main"], env=environ, capture_output=True, text=True,
                             cwd=PROJECT_ROOT)

    assert started.returncode != 0
    assert "SMS_BACKEND must be one of: console, phone, africastalking." in started.stderr
