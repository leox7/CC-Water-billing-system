from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, CHAR, ForeignKey, Numeric, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MeterReading(Base):
    __tablename__ = "meter_readings"
    __table_args__ = (
        # RD-4: one reading per customer per billing period
        UniqueConstraint("customer_id", "billing_period", name="uq_readings_customer_period"),
        # RD-3: a reading never goes down (meter rollover is out of scope)
        CheckConstraint("current_reading >= previous_reading", name="ck_readings_not_lower"),
    )

    reading_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.customer_id", name="fk_readings_customer")
    )
    # RD-5: YYYY-MM, taken from the reading date
    billing_period: Mapped[str] = mapped_column(CHAR(7))
    previous_reading: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    current_reading: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    units_consumed: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    reading_date: Mapped[date]
    # RD-7: who took the reading
    recorded_by: Mapped[int] = mapped_column(ForeignKey("users.user_id", name="fk_readings_user"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    )
