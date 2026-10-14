from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Index, Numeric, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Bill(Base):
    __tablename__ = "bills"
    __table_args__ = (
        # BILL-6: one bill per reading
        UniqueConstraint("reading_id", name="uq_bills_reading"),
        Index("idx_bills_customer", "customer_id", "created_at"),
    )

    bill_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reading_id: Mapped[int] = mapped_column(
        ForeignKey("meter_readings.reading_id", name="fk_bills_reading")
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.customer_id", name="fk_bills_customer")
    )
    # BILL-5: the rate and service charge are copied from the customer when the
    # bill is created, so changing the customer later never changes an old bill
    rate_per_unit: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    service_charge: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    units_consumed: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    consumption_charge: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    previous_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0.00")
    total_due: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0.00")
    # BILL-7, PAY-5: starts unpaid and is always derived from the payments
    status: Mapped[str] = mapped_column(
        Enum("unpaid", "partial", "paid", name="bill_status"), server_default="unpaid"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    )
