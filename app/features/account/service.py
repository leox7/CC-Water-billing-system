"""The team and passwords (PRD sections 4.1 and 4.9): member accounts, changing your own
password, and the owner setting a new password for a member.

Owner and member do the same billing work. Managing accounts is the one thing only the
owner does, so every action on another person's account checks that the actor is the
owner, here as well as in the route, so the rule holds wherever it is called from
(AUTHZ-1, AUTHZ-4).
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import security
from app.core.errors import (
    AppError,
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    unexpected_error,
)
from app.core.security import check_password_rules, hash_password, verify_password
from app.core.validation import clean_name, parse_phone_or_email
from app.features.audit import service as audit
from app.models import MeterReading, Payment, User


@dataclass
class TeamMember:
    user: User
    # AUTH-4: how many readings and payments this person recorded. A member who recorded
    # anything can be deactivated but never deleted.
    record_count: int


def list_team(db: Session) -> list[TeamMember]:
    """Everyone with an account: the owner first, then the members by name."""
    try:
        readings_per_user = dict(
            db.execute(select(MeterReading.recorded_by, func.count()).group_by(MeterReading.recorded_by)).all()
        )
        payments_per_user = dict(
            db.execute(select(Payment.recorded_by, func.count()).group_by(Payment.recorded_by)).all()
        )
        # MySQL sorts an ENUM by its place in the list, and "owner" comes first
        users = db.scalars(select(User).order_by(User.role, User.first_name, User.last_name)).all()
        team = []
        for user in users:
            record_count = readings_per_user.get(user.user_id, 0) + payments_per_user.get(user.user_id, 0)
            team.append(TeamMember(user, record_count))
        return team
    except Exception as exc:
        raise unexpected_error(exc, "list the team") from exc


def team_member(db: Session, user: User) -> TeamMember:
    """One person with their record count, for the row shown after a change."""
    try:
        return TeamMember(user, _record_count(db, user.user_id))
    except Exception as exc:
        raise unexpected_error(exc, "load the team member") from exc


def create_member(
    db: Session, actor: User, first_name: str, last_name: str, identifier: str, password: str
) -> User:
    """AUTH-3: the owner adds a member. The role is always member (AUTHZ-3)."""
    try:
        _require_owner(actor, "add a member")
        clean_first_name = clean_name(first_name, "first_name", "first name")
        clean_last_name = clean_name(last_name, "last_name", "last name")
        phone, email = parse_phone_or_email(identifier)
        _check_not_taken(db, phone, email)
        check_password_rules(password, own_phone=phone)

        member = User(
            first_name=clean_first_name,
            last_name=clean_last_name,
            phone=phone,
            email=email,
            password_hash=hash_password(password),
            role="member",
        )
        db.add(member)
        db.flush()
        audit.record_event(
            db, "user.created", f"Member {clean_first_name} {clean_last_name} added",
            actor=actor, entity=member, details={"user_id": member.user_id},
        )
        db.commit()
        return member
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "add the member") from exc


def deactivate_member(db: Session, actor: User, member_id: int) -> User:
    """AUTH-4, AUTH-7: the member can no longer sign in, from their very next click."""
    try:
        _require_owner(actor, "deactivate a member")
        member = _get_member(db, member_id)
        if member.status == "inactive":
            return member
        member.status = "inactive"
        audit.record_event(
            db, "user.deactivated", f"Member {member.first_name} {member.last_name} deactivated",
            actor=actor, entity=member, details={"user_id": member.user_id},
        )
        db.commit()
        return member
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "deactivate the member") from exc


def reactivate_member(db: Session, actor: User, member_id: int) -> User:
    try:
        _require_owner(actor, "reactivate a member")
        member = _get_member(db, member_id)
        if member.status == "active":
            return member
        member.status = "active"
        audit.record_event(
            db, "user.reactivated", f"Member {member.first_name} {member.last_name} reactivated",
            actor=actor, entity=member, details={"user_id": member.user_id},
        )
        db.commit()
        return member
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "reactivate the member") from exc


def delete_member(db: Session, actor: User, member_id: int) -> str:
    """AUTH-4: only a member who has recorded no reading and no payment may be deleted.
    Returns their name."""
    try:
        _require_owner(actor, "delete a member")
        member = _get_member(db, member_id)
        name = f"{member.first_name} {member.last_name}"
        if _record_count(db, member.user_id):
            raise BusinessRuleError(
                f"{name} has recorded readings or payments, so the account cannot be deleted. Deactivate it instead."
            )
        db.delete(member)
        db.flush()
        audit.record_event(
            db, "user.deleted", f"Member {name} deleted",
            actor=actor, entity=member, details={"user_id": member_id},
        )
        db.commit()
        return name
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "delete the member") from exc


def change_own_password(db: Session, user: User, current_password: str, new_password: str) -> None:
    """AUTH-5, AUTHZ-4: anyone signed in changes their own password. The user comes from the
    session, never from the form."""
    try:
        if not verify_password(current_password, user.password_hash):
            raise BusinessRuleError("That is not your current password. Please try again.", field="current_password")
        check_password_rules(new_password, own_phone=user.phone, field="new_password")
        user.password_hash = hash_password(new_password)
        audit.record_event(
            db, "auth.password_changed", f"{user.first_name} changed their password",
            actor=user, entity=user, details={"user_id": user.user_id, "how": "account screen"},
        )
        db.commit()
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "change the password") from exc


def set_member_password(db: Session, actor: User, member_id: int, new_password: str) -> User:
    """ACC-3: the owner sets a new password for a member, never for an owner account.
    It also lifts any sign in lock on that member."""
    try:
        _require_owner(actor, "set a password for someone else")
        member = _get_member(db, member_id)
        check_password_rules(new_password, own_phone=member.phone, field="new_password")
        member.password_hash = hash_password(new_password)
        for sign_in_name in (member.phone, member.email):
            if sign_in_name:
                security.login_limiter.clear(security.login_key(sign_in_name))
        # never the password itself
        audit.record_event(
            db, "user.password_reset", f"New password set for member {member.first_name} {member.last_name}",
            actor=actor, entity=member, details={"user_id": member.user_id},
        )
        db.commit()
        return member
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "set the new password") from exc


def reset_owner_password(db: Session, identifier: str, new_password: str) -> User:
    """AUTH-8: the developer's reset script, run on the server. It works on the owner only."""
    try:
        phone, email = parse_phone_or_email(identifier)
        if phone:
            user = db.scalar(select(User).where(User.phone == phone))
        else:
            user = db.scalar(select(User).where(User.email == email))
        if user is None:
            raise NotFoundError("No account has that phone number or email.")
        if user.role != "owner":
            raise PermissionDeniedError(
                "That account is not the owner. A member's password is set from the Account screen."
            )
        check_password_rules(new_password, own_phone=user.phone, field="new_password")
        user.password_hash = hash_password(new_password)
        audit.record_event(
            db, "auth.password_changed", f"Owner {user.first_name}'s password reset with the reset script",
            entity=user, details={"user_id": user.user_id, "how": "reset script"},
        )
        db.commit()
        return user
    except AppError:
        raise
    except Exception as exc:
        db.rollback()
        raise unexpected_error(exc, "reset the owner's password") from exc


def _require_owner(actor: User, action: str) -> None:
    if actor.role == "owner":
        return
    audit.record_problem(
        "auth.access_denied",
        f"{actor.first_name} ({actor.role}) was refused: {action}",
        actor=actor,
        details={"action": action, "role": actor.role},
    )
    raise PermissionDeniedError("Only the owner can do that.")


def _get_member(db: Session, member_id: int) -> User:
    member = db.get(User, member_id)
    if member is None:
        raise NotFoundError("We could not find that team member.")
    if member.role != "member":
        # the owner account is never deactivated, deleted or given a password from here
        raise PermissionDeniedError("That can only be done for a member.")
    return member


def _record_count(db: Session, user_id: int) -> int:
    # AUTH-4: readings and payments both count, since members can record either
    readings = db.scalar(select(func.count()).select_from(MeterReading).where(MeterReading.recorded_by == user_id))
    payments = db.scalar(select(func.count()).select_from(Payment).where(Payment.recorded_by == user_id))
    return (readings or 0) + (payments or 0)


def _check_not_taken(db: Session, phone: str | None, email: str | None) -> None:
    # AUTH-1: a phone number or email belongs to one account only
    if phone and db.scalar(select(User.user_id).where(User.phone == phone)):
        raise ConflictError("That phone number is already used by another account.", field="identifier")
    if email and db.scalar(select(User.user_id).where(User.email == email)):
        raise ConflictError("That email is already used by another account.", field="identifier")
