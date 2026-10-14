from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    """The owner and the members he adds. Customers never log in."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("phone", name="uq_users_phone"),
        UniqueConstraint("email", name="uq_users_email"),
        # AUTH-1: at least one of phone or email on every account
        CheckConstraint("phone IS NOT NULL OR email IS NOT NULL", name="ck_users_phone_or_email"),
    )

    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    first_name: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(50))
    phone: Mapped[str | None] = mapped_column(String(12))
    email: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    # PRD section 3: two roles. Only the owner manages accounts.
    role: Mapped[str] = mapped_column(Enum("owner", "member", name="user_role"))
    status: Mapped[str] = mapped_column(
        Enum("active", "inactive", name="user_status"), server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    )
