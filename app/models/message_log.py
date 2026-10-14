from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MessageLog(Base):
    """One row for every SMS attempt (SMS-3)."""

    __tablename__ = "messages_log"
    __table_args__ = (
        Index("idx_messages_bill", "bill_id", "created_at"),
        Index("idx_messages_customer", "customer_id"),
    )

    message_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # COR-4: emptied, not deleted, when a bill is deleted, so the SMS history survives
    bill_id: Mapped[int | None] = mapped_column(
        ForeignKey("bills.bill_id", name="fk_messages_bill", ondelete="SET NULL")
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.customer_id", name="fk_messages_customer")
    )
    message_type: Mapped[str] = mapped_column(Enum("bill", "correction", name="message_type"))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Enum("pending", "sent", "failed", name="message_status"), server_default="pending"
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    )
