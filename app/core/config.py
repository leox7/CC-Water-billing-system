"""Settings, read from environment variables (and from .env for local work).

No secret is ever written in code. The only secret-like value here is the
example session secret, which production refuses to run with (SEC-3).
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# The placeholder shipped in .env.example. Production must replace it.
EXAMPLE_SESSION_SECRET = "change-me-to-a-random-value-of-at-least-32-bytes"
# Used only outside production when SESSION_SECRET is not set.
DEVELOPMENT_SESSION_SECRET = "development-only-secret-not-for-production-use"

APP_ENVIRONMENTS = ("development", "test", "production")
# SMS-1: console (development and tests), phone (the user sends each SMS from their own
# phone) or africastalking (sent automatically)
SMS_BACKENDS = ("console", "phone", "africastalking")
MINIMUM_SECRET_BYTES = 32
# Africa's Talking's test account. The bulk SMS endpoint the app uses has no sandbox, so this
# username is refused rather than sending to an address that does not exist.
SANDBOX_USERNAME = "sandbox"
# Africa's Talking's rule for an alphanumeric sender ID: 1 to 11 letters, digits, - or _
SENDER_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,11}")


class ConfigError(Exception):
    """The settings are missing or wrong. The app must not start."""


@dataclass(frozen=True)
class Settings:
    app_env: str
    debug: bool
    # repr=False keeps secrets out of logs and tracebacks (SEC-2, LOG-2)
    database_url: str = field(repr=False)
    session_secret: str = field(repr=False)
    session_lifetime_days: int
    sms_backend: str
    at_username: str
    at_api_key: str = field(repr=False)
    at_sender_id: str
    hsts_seconds: int
    default_rate_per_unit: Decimal
    default_service_charge: Decimal

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def _read_int(environ: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw_value = environ.get(name, "").strip()
    if raw_value == "":
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number.") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be {minimum} or more.")
    return value


def _read_money(environ: Mapping[str, str], name: str, default: str) -> Decimal:
    raw_value = environ.get(name, "").strip() or default
    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number.") from exc
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{name} must be zero or more.")
    return value


def _read_choice(
    environ: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]
) -> str:
    value = environ.get(name, "").strip().lower() or default
    if value not in allowed:
        raise ConfigError(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def _read_database_url(environ: Mapping[str, str], app_env: str) -> str:
    # DB-5: tests read their own variable, so a dev or real database in
    # DATABASE_URL can never be reached from a test run.
    name = "TEST_DATABASE_URL" if app_env == "test" else "DATABASE_URL"
    database_url = environ.get(name, "").strip()
    if database_url == "":
        raise ConfigError(f"{name} is not set. Add it to .env (see .env.example).")
    return database_url


def _read_session_secret(environ: Mapping[str, str], app_env: str) -> str:
    secret = environ.get("SESSION_SECRET", "").strip()
    if app_env != "production":
        return secret or DEVELOPMENT_SESSION_SECRET
    # SEC-3: production refuses a missing, example or short secret
    if secret == "" or secret == EXAMPLE_SESSION_SECRET:
        raise ConfigError("SESSION_SECRET must be set to a real random value in production.")
    if len(secret.encode("utf-8")) < MINIMUM_SECRET_BYTES:
        raise ConfigError(f"SESSION_SECRET must be at least {MINIMUM_SECRET_BYTES} bytes.")
    return secret


def _read_debug(environ: Mapping[str, str], app_env: str) -> bool:
    debug = environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")
    # OUT-4: debug is never on in production
    if debug and app_env == "production":
        raise ConfigError("DEBUG must be off in production.")
    return debug


def _read_sender_id(environ: Mapping[str, str]) -> str:
    sender_id = environ.get("AT_SENDER_ID", "").strip()
    # empty is allowed here: only the africastalking backend needs one (_check_sms_settings)
    if sender_id != "" and not SENDER_ID_PATTERN.fullmatch(sender_id):
        raise ConfigError("AT_SENDER_ID must be 1 to 11 characters: letters, digits, - or _, with no spaces.")
    return sender_id


def _check_sms_settings(sms_backend: str, app_env: str, username: str, api_key: str, sender_id: str) -> None:
    # console and phone never call a provider (SMS-6), so they need no Africa's Talking settings
    if sms_backend != "africastalking":
        return
    if username == SANDBOX_USERNAME:
        raise ConfigError("Africa's Talking's bulk SMS endpoint has no sandbox, so AT_USERNAME=sandbox cannot be "
                          "used. Use SMS_BACKEND=console outside production.")
    # SMS-6, SEC-7: a real SMS is never sent from a development or test run
    if app_env != "production":
        raise ConfigError("SMS_BACKEND=africastalking is allowed in production only. "
                          "Use SMS_BACKEND=console for development.")
    if username == "" or api_key == "":
        raise ConfigError("AT_USERNAME and AT_API_KEY are needed for africastalking.")
    # Africa's Talking requires senderId on every bulk request
    if sender_id == "":
        raise ConfigError("AT_SENDER_ID is needed for africastalking. Set it to the sender ID "
                          "Africa's Talking approved.")


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build the settings from a mapping of variables, or raise ConfigError."""
    app_env = _read_choice(environ, "APP_ENV", "development", APP_ENVIRONMENTS)
    sms_backend = _read_choice(environ, "SMS_BACKEND", "console", SMS_BACKENDS)
    at_username = environ.get("AT_USERNAME", "").strip()
    at_api_key = environ.get("AT_API_KEY", "").strip()
    at_sender_id = _read_sender_id(environ)
    _check_sms_settings(sms_backend, app_env, at_username, at_api_key, at_sender_id)

    return Settings(
        app_env=app_env,
        debug=_read_debug(environ, app_env),
        database_url=_read_database_url(environ, app_env),
        session_secret=_read_session_secret(environ, app_env),
        session_lifetime_days=_read_int(environ, "SESSION_LIFETIME_DAYS", default=7, minimum=1),
        sms_backend=sms_backend,
        at_username=at_username,
        at_api_key=at_api_key,
        at_sender_id=at_sender_id,
        hsts_seconds=_read_int(environ, "HSTS_SECONDS", default=0, minimum=0),
        # PRD section 13: rate and service charge default from configuration
        default_rate_per_unit=_read_money(environ, "DEFAULT_RATE_PER_UNIT", "130.00"),
        default_service_charge=_read_money(environ, "DEFAULT_SERVICE_CHARGE", "150.00"),
    )


@lru_cache
def get_settings() -> Settings:
    """The app's settings, read once. Variables already set in the environment win over .env."""
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env")
    return load_settings(os.environ)
