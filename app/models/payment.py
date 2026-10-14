from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Payment(Base):
    """Append only: the app never updates or deletes a payment (PAY-9)."""

    __tablename__ = "payments"
    __table_args__ = (
        # PAY-8: a given M-Pesa reference can be recorded once (several NULLs are allowed)
        UniqueConstraint("mpesa_reference", name="uq_payments_mpesa_reference"),
        Index("idx_payments_bill", "bill_id"),
        Index("idx_payments_date", "payment_date"),
        # PAY-4: amount must be greater than 0
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
    )

    payment_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.bill_id", name="fk_payments_bill"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    payment_date: Mapped[date]
    # PAY-1: M-Pesa only
    method: Mapped[str] = mapped_column(String(20), server_default="mpesa")
    mpesa_reference: Mapped[str | None] = mapped_column(String(20))
    recorded_by: Mapped[int] = mapped_column(ForeignKey("users.user_id", name="fk_payments_user"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("CURRENT_TIMESTAMP"))
