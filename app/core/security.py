"""Security plumbing shared by every feature: password hashing, the password rules and the
login attempt limit (SECURITY.md section 1), the security headers on every response
(section 14) and CSRF protection (section 4).
"""

import hmac
import re
import threading
import time
from collections.abc import Callable

from fastapi import Request
from passlib.context import CryptContext
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import AppError, BusinessRuleError, PermissionDeniedError, unexpected_error
from app.core.templating import CSRF_FORM_FIELD, CSRF_SESSION_KEY
from app.core.validation import clean_text, parse_phone_or_email
from app.features.audit import service as audit

# ---- passwords (SECURITY.md AUTH-S3, AUTH-S4) ----

# bcrypt through Passlib. 12 rounds takes about a quarter of a second, which is slow
# enough to make guessing expensive and quick enough for a person signing in.
PASSWORD_HASH_ROUNDS = 12
password_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=PASSWORD_HASH_ROUNDS)

PASSWORD_MIN_LENGTH = 8
# bcrypt ignores everything after 72 bytes, so a longer password would mislead the person
PASSWORD_MAX_BYTES = 72


def hash_password(password: str) -> str:
    """The bcrypt hash to store. The password itself is never stored or logged."""
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True when the password matches the stored hash."""
    return password_context.verify(password, password_hash)


def check_password_rules(password: str, own_phone: str | None = None, field: str = "password") -> None:
    """Raise a BusinessRuleError, beside `field`, for a password that breaks the rules."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise BusinessRuleError(f"Use at least {PASSWORD_MIN_LENGTH} characters for the password.", field=field)
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise BusinessRuleError("That password is too long. Use 72 characters or fewer.", field=field)
    if len(set(password)) == 1:
        raise BusinessRuleError("Use a password that is not one character repeated.", field=field)
    if password.strip().lower() == "password":
        raise BusinessRuleError("That password is too easy to guess. Please choose another.", field=field)
    if own_phone and _is_this_phone_number(password, own_phone):
        raise BusinessRuleError("Do not use your phone number as your password.", field=field)


class LoginAttemptLimiter:
    """AUTH-S5: after 5 failed sign ins for the same phone or email within 15 minutes, that
    phone or email is refused for 15 minutes.

    It lives in the app's memory, so it is right only while the app runs as one worker
    (DEPLOYMENT.md section 10), and restarting the app clears it. If more workers are ever
    used, move it into the database first.
    """

    # Old entries are cleared out once there are this many, so memory cannot grow forever
    CLEAN_UP_AFTER = 10_000

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: int = 15 * 60,
        lock_seconds: int = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self._clock = clock
        self._failure_times: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        # requests run in several threads at once, so changes happen one at a time
        self._thread_lock = threading.Lock()

    def seconds_locked(self, key: str) -> float:
        """How long this phone or email is still locked for. 0 when it is not locked."""
        with self._thread_lock:
            locked_until = self._locked_until.get(key)
            if locked_until is None:
                return 0
            remaining = locked_until - self._clock()
            if remaining <= 0:
                # the lock is over: start counting again from zero
                del self._locked_until[key]
                self._failure_times.pop(key, None)
                return 0
            return remaining

    def record_failure(self, key: str) -> int:
        """Count a failed sign in. Returns the failures in the last 15 minutes; at 5 the lock starts."""
        with self._thread_lock:
            now = self._clock()
            if len(self._failure_times) >= self.CLEAN_UP_AFTER:
                self._forget_old_failures(now)
            recent = [moment for moment in self._failure_times.get(key, []) if now - moment < self.window_seconds]
            recent.append(now)
            self._failure_times[key] = recent
            if len(recent) >= self.max_failures:
                self._locked_until[key] = now + self.lock_seconds
            return len(recent)

    def clear(self, key: str) -> None:
        """Forget the failures and any lock: after a good sign in, or when the owner sets a new password."""
        with self._thread_lock:
            self._failure_times.pop(key, None)
            self._locked_until.pop(key, None)

    def _forget_old_failures(self, now: float) -> None:
        for key in list(self._failure_times):
            if key not in self._locked_until and now - self._failure_times[key][-1] >= self.window_seconds:
                del self._failure_times[key]


# The one limiter for the running app. Services use it as security.login_limiter.
login_limiter = LoginAttemptLimiter()


def login_key(identifier: str) -> str:
    """What the limiter counts by: the phone or email in its stored form, so 0722 123 456 and
    +254722123456 are the same person. Anything else is counted as typed, in lower case."""
    try:
        phone, email = parse_phone_or_email(identifier)
    except BusinessRuleError:
        return clean_text(identifier).lower()
    return phone or email or ""


def _is_this_phone_number(password: str, stored_phone: str) -> bool:
    # stored_phone is 2547XXXXXXXX; people may type it as 07XXXXXXXX or +2547XXXXXXXX
    digits = re.sub(r"\D", "", password)
    if len(digits) != len(password.replace(" ", "").replace("+", "").replace("-", "")):
        return False
    return digits in (stored_phone, "0" + stored_phone[3:])

# htmx, fonts and icons are all served by this app, so nothing needs to be loaded from
# anywhere else. There are no inline scripts or styles, so none are allowed.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
)


def build_security_headers(hsts_seconds: int = 0) -> dict[str, str]:
    """The headers every response carries. Used by the middleware and by the error handlers."""
    headers = {
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        # this app never uses the camera, microphone or location
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
    # HSTS is turned on in the settings only once HTTPS is confirmed working
    if hsts_seconds > 0:
        headers["Strict-Transport-Security"] = f"max-age={hsts_seconds}"
    return headers


class SecurityHeadersMiddleware:
    """Adds the security headers to every response, whatever route or error made it."""

    def __init__(self, app: ASGIApp, hsts_seconds: int = 0) -> None:
        self.app = app
        self.headers = build_security_headers(hsts_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    response_headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


# ---- CSRF (SECURITY.md section 4) ----
# Anything that changes data is a POST (CSRF-1). Every POST must carry the token that was
# put in the person's session: as a hidden form field, or, for HTMX, in a header that is
# set once on <body>. A missing or wrong token is refused with 403 and changes nothing.
# The token itself is made and put into pages by get_csrf_token() and csrf_field() in
# templating.py.

CSRF_HEADER = "X-CSRF-Token"
CSRF_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


async def verify_csrf(request: Request) -> None:
    """App-wide dependency: refuses any POST that does not carry the right token."""
    try:
        if request.method in CSRF_SAFE_METHODS:
            return
        expected_token = request.session.get(CSRF_SESSION_KEY, "")
        submitted_token = request.headers.get(CSRF_HEADER) or await _token_from_form(request)
        # compare_digest takes the same time however many characters match. Bytes, because
        # it refuses text with non-ASCII characters, and anyone can send those.
        if expected_token and hmac.compare_digest(expected_token.encode(), submitted_token.encode()):
            return
        audit.record_problem(
            "auth.csrf_failed",
            f"Form token missing or wrong for {request.method} {request.url.path}",
            details={"route": request.url.path, "method": request.method},
        )
        raise PermissionDeniedError("This page has expired. Please reload it and try again.")
    except (AppError, HTTPException):
        raise
    except Exception as exc:
        raise unexpected_error(exc, "check the form token") from exc


async def _token_from_form(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        return ""
    form = await request.form()
    token = form.get(CSRF_FORM_FIELD, "")
    return token if isinstance(token, str) else ""
