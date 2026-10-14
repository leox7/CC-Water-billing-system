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


def test_real_sms_backend_is_refused_outside_production():
    # SMS-6: a real SMS is never sent from a dev or test run
    environ = development_environ(SMS_BACKEND="africastalking", AT_USERNAME="u", AT_API_KEY="k")

    with pytest.raises(ConfigError, match="production only"):
        load_settings(environ)


def test_the_refusal_points_to_the_sandbox():
    environ = development_environ(SMS_BACKEND="africastalking", AT_USERNAME="ccwater", AT_API_KEY="k")

    with pytest.raises(ConfigError, match="AT_USERNAME=sandbox"):
        load_settings(environ)


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_the_sandbox_may_be_used_outside_production(app_env):
    # it sends nothing to a real phone, so SMS-6 and SEC-7 still hold
    environ = development_environ(SMS_BACKEND="africastalking", AT_USERNAME="sandbox", AT_API_KEY="sandbox-key",
                                  APP_ENV=app_env, TEST_DATABASE_URL="mysql+pymysql://u:p@127.0.0.1/x_test")

    settings = load_settings(environ)

    assert settings.uses_sandbox is True


def test_the_sandbox_still_needs_its_api_key():
    environ = development_environ(SMS_BACKEND="africastalking", AT_USERNAME="sandbox")

    with pytest.raises(ConfigError, match="AT_API_KEY"):
        load_settings(environ)


def test_the_sandbox_is_refused_in_production():
    environ = production_environ(SMS_BACKEND="africastalking", AT_USERNAME="sandbox", AT_API_KEY="k")

    with pytest.raises(ConfigError, match="sandbox is for development only"):
        load_settings(environ)


def test_the_live_account_does_not_use_the_sandbox():
    settings = load_settings(production_environ(SMS_BACKEND="africastalking", AT_USERNAME="ccwater", AT_API_KEY="k"))

    assert settings.uses_sandbox is False


def test_the_console_backend_never_uses_the_sandbox():
    assert load_settings(development_environ(AT_USERNAME="sandbox")).uses_sandbox is False


def test_real_sms_backend_needs_credentials_in_production():
    with pytest.raises(ConfigError, match="AT_USERNAME"):
        load_settings(production_environ(SMS_BACKEND="africastalking"))


def test_real_sms_backend_is_allowed_in_production_with_credentials():
    settings = load_settings(
        production_environ(SMS_BACKEND="africastalking", AT_USERNAME="u", AT_API_KEY="k")
    )

    assert settings.sms_backend == "africastalking"


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
    settings = load_settings(production_environ(SMS_BACKEND="africastalking",
                                                 AT_USERNAME="u", AT_API_KEY="key-value-123"))

    text = repr(settings)

    assert REAL_SECRET not in text
    assert "key-value-123" not in text
    assert "pw@" not in text


@pytest.mark.parametrize("typo", ["whatsapp", "consol", "console2", "africa", "sms"])
def test_an_unknown_sms_backend_stops_the_app_with_a_clear_message(typo):
    # SMS-6: a typo must never fall back to sending real SMS, or to silence
    with pytest.raises(ConfigError) as raised:
        load_settings(development_environ(SMS_BACKEND=typo))

    assert str(raised.value) == "SMS_BACKEND must be one of: console, africastalking."


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
    assert "SMS_BACKEND must be one of: console, africastalking." in started.stderr
