"""initial_schema

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-03-21 00:00:00.000000

Creates all tables for the Trading System V1 (RFTM Strategy).
TimescaleDB hypertables:
  - market_data_daily   (partition by date, 1-year chunks)
  - indicators_cache    (partition by date, 1-year chunks)
  - portfolio_snapshots (partition by snapshot_at, 1-month chunks)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── symbols ───────────────────────────────────────────────────────────────
    op.create_table(
        "symbols",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sector", sa.String(100), nullable=True),
        sa.Column("asset_type", sa.String(20), nullable=False, server_default="ETF"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("symbol", name="uq_symbols_symbol"),
        comment="Master record for each tradeable ETF in the universe",
    )
    op.create_index("ix_symbols_id", "symbols", ["id"])
    op.create_index("ix_symbols_symbol", "symbols", ["symbol"])

    # ── market_data_daily ────────────────────────────────────────────────────
    # TimescaleDB rule: ALL unique indexes (incl. PK) must contain the partition column.
    # PK = (id, date) — composite so TimescaleDB can enforce uniqueness per chunk.
    op.create_table(
        "market_data_daily",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column(
            "symbol_id",
            sa.String(36),
            sa.ForeignKey("symbols.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(12, 4), nullable=False),
        sa.Column("high", sa.Numeric(12, 4), nullable=False),
        sa.Column("low", sa.Numeric(12, 4), nullable=False),
        sa.Column("close", sa.Numeric(12, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("vwap", sa.Numeric(12, 4), nullable=True),
        sa.Column("num_trades", sa.Integer(), nullable=True),
        sa.Column("data_quality", sa.String(20), nullable=False, server_default="VALID"),
        sa.Column("source", sa.String(50), nullable=False, server_default="polygon"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", "date"),  # composite PK — required by TimescaleDB
        sa.UniqueConstraint("symbol", "date", name="uq_market_data_symbol_date"),
        comment="Daily OHLCV bars — TimescaleDB hypertable",
    )
    op.create_index("ix_market_data_daily_id", "market_data_daily", ["id"])
    op.create_index("ix_market_data_daily_symbol", "market_data_daily", ["symbol"])
    op.create_index("ix_market_data_daily_date", "market_data_daily", ["date"])
    op.create_index("ix_market_data_daily_symbol_id", "market_data_daily", ["symbol_id"])

    # ── indicators_cache ─────────────────────────────────────────────────────
    op.create_table(
        "indicators_cache",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column(
            "symbol_id",
            sa.String(36),
            sa.ForeignKey("symbols.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("ema_50", sa.Numeric(12, 4), nullable=True),
        sa.Column("ema_200", sa.Numeric(12, 4), nullable=True),
        sa.Column("rsi_14", sa.Numeric(8, 4), nullable=True),
        sa.Column("atr_14", sa.Numeric(12, 4), nullable=True),
        sa.Column("atr_14_pct", sa.Numeric(8, 6), nullable=True),
        sa.Column("volume_ma_20", sa.Numeric(16, 2), nullable=True),
        sa.Column("high_20d", sa.Numeric(12, 4), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", "date"),  # composite PK — required by TimescaleDB
        sa.UniqueConstraint("symbol", "date", name="uq_indicators_symbol_date"),
        comment="Computed indicators — TimescaleDB hypertable",
    )
    op.create_index("ix_indicators_cache_id", "indicators_cache", ["id"])
    op.create_index("ix_indicators_cache_symbol", "indicators_cache", ["symbol"])
    op.create_index("ix_indicators_cache_date", "indicators_cache", ["date"])
    op.create_index("ix_indicators_cache_symbol_id", "indicators_cache", ["symbol_id"])

    # ── trading_runs ─────────────────────────────────────────────────────────
    op.create_table(
        "trading_runs",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("run_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="RUNNING"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("initial_capital", sa.Numeric(15, 2), nullable=False),
        sa.Column("final_capital", sa.Numeric(15, 2), nullable=True),
        sa.Column("total_return_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("max_drawdown_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winning_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losing_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config_snapshot", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        comment="One execution context (backtest / paper / live)",
    )
    op.create_index("ix_trading_runs_id", "trading_runs", ["id"])

    # ── signals ───────────────────────────────────────────────────────────────
    op.create_table(
        "signals",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("trading_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("signal_type", sa.String(10), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False, server_default="LONG"),
        sa.Column("close_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("ema_50", sa.Numeric(12, 4), nullable=True),
        sa.Column("ema_200", sa.Numeric(12, 4), nullable=True),
        sa.Column("rsi_14", sa.Numeric(8, 4), nullable=True),
        sa.Column("atr_14", sa.Numeric(12, 4), nullable=True),
        sa.Column("volume_ratio", sa.Numeric(8, 4), nullable=True),
        sa.Column("regime_ok", sa.Boolean(), nullable=True),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("stop_loss", sa.Numeric(12, 4), nullable=True),
        sa.Column("position_size_shares", sa.String(20), nullable=True),
        sa.Column("risk_decision", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("risk_rejection_reason", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        comment="Strategy output — one row per symbol per evaluation",
    )
    op.create_index("ix_signals_id", "signals", ["id"])
    op.create_index("ix_signals_run_id", "signals", ["run_id"])
    op.create_index("ix_signals_symbol", "signals", ["symbol"])
    op.create_index("ix_signals_signal_date", "signals", ["signal_date"])

    # ── orders ────────────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("trading_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "signal_id",
            sa.String(36),
            sa.ForeignKey("signals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("broker_order_id", sa.String(100), nullable=True),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("order_type", sa.String(10), nullable=False, server_default="MARKET"),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("stop_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("submitted_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("filled_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("filled_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column("commission", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("slippage", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        comment="Broker order lifecycle record",
    )
    op.create_index("ix_orders_id", "orders", ["id"])
    op.create_index("ix_orders_run_id", "orders", ["run_id"])
    op.create_index("ix_orders_symbol", "orders", ["symbol"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_broker_order_id", "orders", ["broker_order_id"])
    op.create_index("ix_orders_correlation_id", "orders", ["correlation_id"])
    op.create_index("ix_orders_signal_id", "orders", ["signal_id"])

    # ── positions ─────────────────────────────────────────────────────────────
    op.create_table(
        "positions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("trading_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("direction", sa.String(10), nullable=False, server_default="LONG"),
        sa.Column(
            "entry_order_id",
            sa.String(36),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "exit_order_id",
            sa.String(36),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("qty", sa.String(20), nullable=False),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("exit_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("stop_loss", sa.Numeric(12, 4), nullable=False),
        sa.Column("current_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(12, 4), nullable=True),
        sa.Column("commission_total", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        comment="Open and closed trade records",
    )
    op.create_index("ix_positions_id", "positions", ["id"])
    op.create_index("ix_positions_run_id", "positions", ["run_id"])
    op.create_index("ix_positions_symbol", "positions", ["symbol"])
    op.create_index("ix_positions_status", "positions", ["status"])
    op.create_index("ix_positions_entry_order_id", "positions", ["entry_order_id"])
    op.create_index("ix_positions_exit_order_id", "positions", ["exit_order_id"])

    # ── portfolio_snapshots ───────────────────────────────────────────────────
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("trading_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_type", sa.String(20), nullable=False, server_default="DAILY_CLOSE"),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash", sa.Numeric(15, 2), nullable=False),
        sa.Column("positions_value", sa.Numeric(15, 2), nullable=False),
        sa.Column("total_equity", sa.Numeric(15, 2), nullable=False),
        sa.Column("open_positions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("peak_equity", sa.Numeric(15, 2), nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(12, 4), nullable=True),
        sa.Column("cumulative_return_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("positions_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", "snapshot_at"),  # composite PK — required by TimescaleDB
        comment="Portfolio state snapshots — TimescaleDB hypertable",
    )
    op.create_index("ix_portfolio_snapshots_id", "portfolio_snapshots", ["id"])
    op.create_index("ix_portfolio_snapshots_run_id", "portfolio_snapshots", ["run_id"])
    op.create_index("ix_portfolio_snapshots_snapshot_at", "portfolio_snapshots", ["snapshot_at"])

    # ── audit_log ─────────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False, server_default="INFO"),
        sa.Column("entity_type", sa.String(50), nullable=True),
        sa.Column("entity_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        comment="Immutable append-only event trail — never update/delete",
    )
    op.create_index("ix_audit_log_id", "audit_log", ["id"])
    op.create_index("ix_audit_log_correlation_id", "audit_log", ["correlation_id"])
    op.create_index("ix_audit_log_event_type", "audit_log", ["event_type"])
    op.create_index("ix_audit_log_severity", "audit_log", ["severity"])
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])
    op.create_index("ix_audit_log_run_id", "audit_log", ["run_id"])

    # ── risk_events ───────────────────────────────────────────────────────────
    op.create_table(
        "risk_events",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("trading_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("rule_code", sa.String(50), nullable=False),
        sa.Column("rule_priority", sa.String(5), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(10), nullable=True),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column("metrics_snapshot", sa.Text(), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        comment="Risk Engine decision log",
    )
    op.create_index("ix_risk_events_id", "risk_events", ["id"])
    op.create_index("ix_risk_events_run_id", "risk_events", ["run_id"])
    op.create_index("ix_risk_events_correlation_id", "risk_events", ["correlation_id"])
    op.create_index("ix_risk_events_rule_code", "risk_events", ["rule_code"])
    op.create_index("ix_risk_events_decision", "risk_events", ["decision"])
    op.create_index("ix_risk_events_triggered_at", "risk_events", ["triggered_at"])
    op.create_index("ix_risk_events_symbol", "risk_events", ["symbol"])

    # ── TimescaleDB hypertables ───────────────────────────────────────────────
    # Must be called AFTER the tables are created.
    # chunk_time_interval: 1 year for daily data (small number of rows/year per symbol)
    op.execute(
        "SELECT create_hypertable("
        "  'market_data_daily', 'date',"
        "  chunk_time_interval => INTERVAL '1 year',"
        "  if_not_exists => TRUE"
        ")"
    )
    op.execute(
        "SELECT create_hypertable("
        "  'indicators_cache', 'date',"
        "  chunk_time_interval => INTERVAL '1 year',"
        "  if_not_exists => TRUE"
        ")"
    )
    op.execute(
        "SELECT create_hypertable("
        "  'portfolio_snapshots', 'snapshot_at',"
        "  chunk_time_interval => INTERVAL '1 month',"
        "  if_not_exists => TRUE"
        ")"
    )

    # ── Seed: ETF Universe ────────────────────────────────────────────────────
    op.execute("""
        INSERT INTO symbols (id, symbol, name, sector, asset_type, is_active)
        VALUES
          (gen_random_uuid()::text, 'SPY',  'SPDR S&P 500 ETF Trust',                    'Equity-Broad',     'ETF', true),
          (gen_random_uuid()::text, 'QQQ',  'Invesco QQQ Trust',                          'Equity-Tech',      'ETF', true),
          (gen_random_uuid()::text, 'IWM',  'iShares Russell 2000 ETF',                   'Equity-SmallCap',  'ETF', true),
          (gen_random_uuid()::text, 'DIA',  'SPDR Dow Jones Industrial Average ETF',      'Equity-Broad',     'ETF', true),
          (gen_random_uuid()::text, 'GLD',  'SPDR Gold Shares',                           'Commodities',      'ETF', true),
          (gen_random_uuid()::text, 'TLT',  'iShares 20+ Year Treasury Bond ETF',         'Fixed Income',     'ETF', true),
          (gen_random_uuid()::text, 'HYG',  'iShares iBoxx $ High Yield Corp Bond ETF',  'Fixed Income',     'ETF', true),
          (gen_random_uuid()::text, 'XLE',  'Energy Select Sector SPDR Fund',             'Sector-Energy',    'ETF', true),
          (gen_random_uuid()::text, 'XLF',  'Financial Select Sector SPDR Fund',          'Sector-Financial', 'ETF', true),
          (gen_random_uuid()::text, 'XLK',  'Technology Select Sector SPDR Fund',         'Sector-Tech',      'ETF', true),
          (gen_random_uuid()::text, 'XLV',  'Health Care Select Sector SPDR Fund',        'Sector-Health',    'ETF', true),
          (gen_random_uuid()::text, 'XLI',  'Industrial Select Sector SPDR Fund',         'Sector-Industrial','ETF', true),
          (gen_random_uuid()::text, 'XLC',  'Communication Services Select Sector SPDR',  'Sector-Comms',     'ETF', true),
          (gen_random_uuid()::text, 'XLU',  'Utilities Select Sector SPDR Fund',          'Sector-Utilities', 'ETF', true),
          (gen_random_uuid()::text, 'XLB',  'Materials Select Sector SPDR Fund',          'Sector-Materials', 'ETF', true),
          (gen_random_uuid()::text, 'XLRE', 'Real Estate Select Sector SPDR Fund',        'Sector-RealEstate','ETF', true),
          (gen_random_uuid()::text, 'EEM',  'iShares MSCI Emerging Markets ETF',          'Equity-EM',        'ETF', true),
          (gen_random_uuid()::text, 'EFA',  'iShares MSCI EAFE ETF',                      'Equity-Intl',      'ETF', true)
        ON CONFLICT (symbol) DO NOTHING
    """)


def downgrade() -> None:
    # Drop hypertables first (TimescaleDB — order matters)
    op.drop_table("risk_events")
    op.drop_table("audit_log")
    op.drop_table("portfolio_snapshots")
    op.drop_table("positions")
    op.drop_table("orders")
    op.drop_table("signals")
    op.drop_table("trading_runs")
    op.drop_table("indicators_cache")
    op.drop_table("market_data_daily")
    op.drop_table("symbols")
