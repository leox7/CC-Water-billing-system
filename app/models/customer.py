from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("meter_number", name="uq_customers_meter_number"),)

    customer_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    first_name: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(50))
    phone: Mapped[str] = mapped_column(String(12))
    location: Mapped[str] = mapped_column(String(100))
    meter_number: Mapped[str] = mapped_column(String(20))
    # CUS-1: defaults are Ksh 130.00 per unit and Ksh 150.00 service charge
    rate_per_unit: Mapped[Decimal] = mapped_column(Numeric(10, 2), server_default="130.00")
    service_charge: Mapped[Decimal] = mapped_column(Numeric(10, 2), server_default="150.00")
    status: Mapped[str] = mapped_column(
        Enum("active", "inactive", name="customer_status"), server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    )
