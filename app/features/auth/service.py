"""Registration of the first account, signing in and signing out (PRD section 4.1).

The controller deals with the session cookie. This file holds the rules.
"""

from datetime import datetime, timedelta
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import security
from app.core.errors import (
    AppError,
    BusinessRuleError,
    PermissionDeniedError,
    TooManyAttemptsError,
    unexpected_error,
)
from app.core.logging import EAT
from app.core.security import check_password_rules, hash_password, verify_password
from app.core.validation import clean_name, parse_phone_or_email
from app.features.audit import service as audit
from app.models import User

# AUTH-S2: one message for every failure, so nobody can tell whether an account exists
SIGN_IN_FAILED = "We could not sign you in. Check your details and try again."


def registration_is_open(db: Session) -> bool:
    """AUTH-2: anyone may register only while no account exists at all."""
    try:
        return db.scalar(select(User.user_id).limit(1)) is None
    except Exception as exc:
        raise unexpected_error(exc, "check whether registration is open") from exc


def register_owner(db: Session, first_name: str, last_name: str, identifier: str, password: str) -> User:
    """AUTH-2: the first account becomes the owner. After that, registration is closed."""
    try:
        # FOR UPDATE: two people registering at the same moment wait for each other, so
        # the second one sees the first account and is refused, instead of both becoming owner
        someone_exists = db.scalar(select(User.user_id).limit(1).with_for_update())
        if someone_exists is not None:
            raise PermissionDeniedError("Registration is closed. Please sign in.")

        clean_first_name = clean_name(first_name, "first_name", "first name")
        clean_last_name = clean_name(last_name, "last_name", "last name")
        phone, email = parse_phone_or_email(identifier)
        check_password_rules(password, own_phone=phone)

        owner = User(
            first_name=clean_first_name,
            last_name=clean_last_name,
            phone=phone,
            email=email,
            password_hash=hash_password(password),
            # AUTHZ-3: the role is set here, never taken from the form
            role="owner",
        )
        db.add(owner)
        db.flush()
        audit.record_event(
            db,
            "auth.owner_registered",
            f"Owner account created for {clean_first_name}",
            actor=owner,
            entity=owner,
            details={"user_id": owner.user_id},
        )
        db.commit()
        return owner
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "create the owner account") from exc


def sign_in(db: Session, identifier: str, password: str) -> User:
    """AUTH-1: sign in with a phone number or an email, and the password.

    Anything wrong (unknown account, wrong password, inactive account) gives the same
    message (AUTH-S2). An inactive account cannot sign in (AUTH-4).
    """
    try:
        how = "email" if "@" in identifier else "phone"
        key = security.login_key(identifier)
        if security.login_limiter.seconds_locked(key) > 0:
            # AUTH-S5: refused without even checking, with the very same message
            audit.record_problem(
                "auth.login_failed",
                f"Sign in refused using a {how}: locked after too many failed attempts",
                details={"how": how, "locked": True},
            )
            raise TooManyAttemptsError(SIGN_IN_FAILED)

        user = _find_user(db, identifier)
        # The password is checked even when nobody matched, against a stand-in hash, so a
        # wrong account and a wrong password take the same time to answer.
        password_hash = user.password_hash if user else _stand_in_hash()
        password_matches = verify_password(password, password_hash)

        if user is not None and password_matches and user.status == "active":
            security.login_limiter.clear(key)
            audit.record_event(
                db,
                "auth.login_success",
                f"{user.first_name} ({user.role}) signed in with their {how}",
                actor=user,
                entity=user,
                details={"user_id": user.user_id, "how": how},
            )
            db.commit()
            return user

        _record_failed_sign_in(key, how)
        raise BusinessRuleError(SIGN_IN_FAILED)
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "sign in") from exc


def record_sign_out(db: Session, user: User) -> None:
    """Records auth.logout. The controller clears the session (AUTH-S6)."""
    try:
        audit.record_event(
            db,
            "auth.logout",
            f"{user.first_name} ({user.role}) signed out",
            actor=user,
            entity=user,
        )
        db.commit()
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "sign out") from exc


def _record_failed_sign_in(key: str, how: str) -> None:
    attempts = security.login_limiter.record_failure(key)
    # Never the password, and never whether the account exists
    audit.record_problem(
        "auth.login_failed",
        f"Sign in failed using a {how} (attempt {attempts})",
        details={"how": how, "attempt": attempts},
    )
    if attempts >= security.login_limiter.max_failures:
        locked_until = datetime.now(EAT) + timedelta(seconds=security.login_limiter.lock_seconds)
        audit.record_problem(
            "auth.login_locked",
            f"Sign in locked for {security.login_limiter.lock_seconds // 60} minutes "
            f"after {attempts} failed attempts using a {how}",
            details={"how": how, "attempts": attempts, "locked_until": locked_until.isoformat(timespec="minutes")},
        )


def _find_user(db: Session, identifier: str) -> User | None:
    try:
        phone, email = parse_phone_or_email(identifier)
    except BusinessRuleError:
        # Not a phone number or an email, so it cannot match anyone. The person still gets
        # the one generic message, not a hint about what they typed.
        return None
    if phone:
        return db.scalar(select(User).where(User.phone == phone))
    return db.scalar(select(User).where(User.email == email))


@lru_cache
def _stand_in_hash() -> str:
    return hash_password("stand-in for an account that does not exist")
