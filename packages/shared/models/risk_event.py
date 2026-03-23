"""
packages/shared/models/risk_event.py
RiskEvent — one decision record from the Risk Engine.
Every time the Risk Engine evaluates a signal (approve / reject / defer),
a RiskEvent is written. Used for monitoring and post-trade analysis.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.shared.enums import RiskDecision
from packages.shared.models.base import Base, UUIDPrimaryKeyMixin


class RiskEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "risk_events"
    __table_args__ = {"comment": "Risk Engine decision log"}

    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("trading_runs.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    correlation_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
    )

    # Rule that was triggered
    rule_code: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
        comment="e.g. P0_KILL_SWITCH | P1_MAX_DRAWDOWN | P2_POSITION_LIMIT",
    )
    rule_priority: Mapped[str] = mapped_column(
        String(5), nullable=False,
        comment="P0 | P1 | P2 | P3",
    )

    decision: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True,
        comment="RiskDecision: APPROVED | REJECTED | DEFERRED",
    )

    # Context
    symbol: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Portfolio metrics at decision time (JSON)
    metrics_snapshot: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON: equity, drawdown_pct, open_positions, daily_loss_pct",
    )

    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    @classmethod
    def rejected(
        cls,
        *,
        rule_code: str,
        rule_priority: str,
        correlation_id: str,
        rejection_reason: str,
        run_id: str | None = None,
        symbol: str | None = None,
        metrics_snapshot: str | None = None,
    ) -> "RiskEvent":
        return cls(
            rule_code=rule_code,
            rule_priority=rule_priority,
            decision=RiskDecision.REJECTED.value,
            correlation_id=correlation_id,
            rejection_reason=rejection_reason,
            run_id=run_id,
            symbol=symbol,
            metrics_snapshot=metrics_snapshot,
            triggered_at=datetime.now(timezone.utc),
        )

    def __repr__(self) -> str:
        return (
            f"<RiskEvent {self.rule_code} {self.decision} "
            f"symbol={self.symbol} at={self.triggered_at}>"
        )
