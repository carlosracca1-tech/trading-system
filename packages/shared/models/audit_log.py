"""
packages/shared/models/audit_log.py
AuditLog — immutable, append-only event trail.
Every significant action (order submitted, kill switch triggered, config changed)
must produce an audit log entry.

RULES:
  - Never UPDATE or DELETE rows in this table.
  - Write via AuditLog.write() only — never construct directly.
  - correlation_id ties together all events from one request/job.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import AlertSeverity
from packages.shared.models.base import Base, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = {"comment": "Immutable append-only event trail — never update/delete"}

    correlation_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
        comment="UUID linking related events across services",
    )
    event_type: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
        comment="Dotted namespace e.g. order.submitted, kill_switch.triggered",
    )
    actor: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="Service or user that triggered the event",
    )
    severity: Mapped[str] = mapped_column(
        String(10), nullable=False, default=AlertSeverity.INFO.value, index=True,
    )

    # Optional entity reference
    entity_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment="e.g. order | position | run | config",
    )
    entity_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )

    # Full event data
    payload: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON — full event context for reconstruction",
    )

    # Application time (not DB insert time — important for ordering)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    @classmethod
    def write(
        cls,
        *,
        event_type: str,
        actor: str,
        correlation_id: str,
        payload: str | None = None,
        severity: AlertSeverity = AlertSeverity.INFO,
        entity_type: str | None = None,
        entity_id: str | None = None,
        run_id: str | None = None,
    ) -> "AuditLog":
        """Factory method — use this instead of constructing directly."""
        return cls(
            event_type=event_type,
            actor=actor,
            correlation_id=correlation_id,
            payload=payload,
            severity=severity.value,
            entity_type=entity_type,
            entity_id=entity_id,
            run_id=run_id,
            occurred_at=datetime.now(timezone.utc),
        )

    def __repr__(self) -> str:
        return (
            f"<AuditLog {self.event_type} actor={self.actor} "
            f"occurred_at={self.occurred_at}>"
        )
