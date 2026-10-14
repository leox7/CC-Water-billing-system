from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Enum, Index, String, text
from sqlalchemy.dialects.mysql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditLog(Base):
    """Developer-only record of every event. See docs/AUDIT_LOG.md.

    No foreign keys on purpose, so a record of "reading 41 was deleted" survives
    after the reading itself is gone. The app only inserts and reads (rule 5).
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_created", "created_at"),
        Index("idx_audit_event", "event_type", "created_at"),
        Index("idx_audit_entity", "entity_type", "entity_id"),
        Index("idx_audit_customer", "customer_id", "created_at"),
        Index("idx_audit_request", "request_id"),
    )

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(fsp=3), server_default=text("CURRENT_TIMESTAMP(3)")
    )
    level: Mapped[str] = mapped_column(
        Enum("info", "warning", "error", name="audit_level"), server_default="info"
    )
    event_type: Mapped[str] = mapped_column(String(50))
    summary: Mapped[str] = mapped_column(String(255))
    actor_user_id: Mapped[int | None]
    actor_label: Mapped[str | None] = mapped_column(String(100))
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[int | None]
    customer_id: Mapped[int | None]
    request_id: Mapped[str | None] = mapped_column(String(36))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
