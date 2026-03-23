"""
packages/shared/models/__init__.py
Central import point for all SQLAlchemy models.

Import this module (or individual models) wherever you need to reference
model classes. Alembic's env.py imports from here for autogenerate.

Usage:
    from packages.shared.models import Symbol, MarketDataDaily, Order
"""
from packages.shared.models.audit_log import AuditLog
from packages.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from packages.shared.models.indicator import IndicatorCache
from packages.shared.models.market_data import MarketDataDaily
from packages.shared.models.order import Order
from packages.shared.models.portfolio_snapshot import PortfolioSnapshot
from packages.shared.models.position import Position
from packages.shared.models.risk_event import RiskEvent
from packages.shared.models.signal import Signal
from packages.shared.models.symbol import Symbol
from packages.shared.models.trading_run import TradingRun

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    # Domain models
    "Symbol",
    "MarketDataDaily",
    "IndicatorCache",
    "TradingRun",
    "Signal",
    "Order",
    "Position",
    "PortfolioSnapshot",
    "AuditLog",
    "RiskEvent",
]
