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
    # SMS-3: pending, sent or failed with Africa's Talking; ready, then sent or cancelled,
    # with the phone method (SMS-14, SMS-20)
    status: Mapped[str] = mapped_column(
        Enum("pending", "sent", "failed", "ready", "cancelled", name="message_status"), server_default="pending"
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(100))
    # SMS-3: a plain sentence the app chose from the provider's status code, empty unless failed
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    # SMS-17: who marked a phone-method message as sent. AUTH-4: that member is never deleted.
    sent_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.user_id", name="fk_messages_sent_by")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    )
