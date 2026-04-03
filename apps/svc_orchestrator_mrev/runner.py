"""
apps/svc_orchestrator_mrev/runner.py
MREV-1H Hourly runner — standalone entry point.

Runs the full MREV pipeline using in-memory state (no DB required for V1).
Designed to be called every hour by a scheduler or manually for testing.

Architecture:
  - Stateful in-memory tracker (positions, equity, peak)
  - Delegates computation to pipeline.py (pure, no DB)
  - Uses DryRunBroker for paper trading simulation

Usage:
    python -m apps.svc_orchestrator_mrev.runner --bars 500 --capital 1000
    python -m apps.svc_orchestrator_mrev.runner --live-scan  # real Alpaca data (future)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from apps.svc_data_1h.indicators import compute_mrev_indicators
from apps.svc_data_1h.synthetic import generate_multi_symbol_1h
from apps.svc_execution.broker import DryRunBroker
from apps.svc_orchestrator_mrev.pipeline import (
    MrevExecutionIntent,
    MrevPipelineRun,
    MrevPortfolioState,
    run_mrev_pipeline,
)
from apps.svc_strategy_mrev.constants import MREV_SYMBOLS
from packages.shared.enums import OrderSide, OrderStatus
from packages.shared.logging_config import configure_logging, get_logger

log = get_logger(__name__)


# ── In-memory state tracker ──────────────────────────────────────────────────

@dataclass
class OpenPosition:
    """Lightweight in-memory position for the MREV runner."""
    position_id: str
    symbol: str
    qty: float
    entry_price: float
    entry_datetime: datetime
    stop_loss: float


@dataclass
class MrevRunState:
    """
    Complete in-memory state for a MREV-1H run.
    No database needed — everything lives here.
    """
    initial_capital: float
    cash: float
    peak_equity: float
    positions: dict[str, OpenPosition] = field(default_factory=dict)  # symbol → OpenPosition
    closed_trades: list[dict] = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    bars_processed: int = 0

    @property
    def positions_value(self) -> float:
        """Current mark-to-market value of all open positions."""
        return sum(p.qty * p.entry_price for p in self.positions.values())

    @property
    def total_equity(self) -> float:
        return self.cash + self.positions_value

    def portfolio_state(self) -> MrevPortfolioState:
        return MrevPortfolioState(
            total_equity=self.total_equity,
            peak_equity=self.peak_equity,
            open_position_count=len(self.positions),
            cash=self.cash,
        )

    def open_positions_map(self) -> dict[str, dict]:
        """Format open positions for the pipeline."""
        return {
            sym: {
                "position_id": pos.position_id,
                "entry_price": pos.entry_price,
                "entry_datetime": pos.entry_datetime,
            }
            for sym, pos in self.positions.items()
        }


# ── Execute intents against broker ───────────────────────────────────────────

def execute_mrev_intents(
    intents: list[MrevExecutionIntent],
    state: MrevRunState,
    broker: DryRunBroker,
) -> int:
    """
    Execute a list of MREV intents against the broker.
    Updates state in-place. Returns number of filled orders.
    """
    filled = 0

    for intent in intents:
        if intent.is_exit:
            pos = state.positions.get(intent.symbol)
            if pos is None:
                log.warning("mrev_no_position_for_exit", symbol=intent.symbol)
                continue

            try:
                broker_order = broker.submit_order(
                    symbol=intent.symbol,
                    side=OrderSide.SELL.value,
                    qty=int(pos.qty) if not intent.signal.symbol.count("/") else 1,
                    submitted_price=intent.signal.close_price,
                )
            except Exception as exc:
                log.error("mrev_exit_failed", symbol=intent.symbol, error=str(exc))
                continue

            if broker_order.status == OrderStatus.FILLED.value:
                exit_price = broker_order.filled_avg_price or intent.signal.close_price
                pnl = (exit_price - pos.entry_price) * pos.qty

                state.closed_trades.append({
                    "symbol": intent.symbol,
                    "entry_price": pos.entry_price,
                    "exit_price": exit_price,
                    "qty": pos.qty,
                    "pnl": round(pnl, 4),
                    "reason": intent.signal.reason,
                    "entry_dt": str(pos.entry_datetime),
                    "exit_dt": str(intent.signal.signal_datetime),
                })

                state.cash += pos.qty * exit_price
                state.total_trades += 1
                if pnl > 0:
                    state.winning_trades += 1
                else:
                    state.losing_trades += 1

                del state.positions[intent.symbol]
                filled += 1

                log.info(
                    "mrev_position_closed",
                    symbol=intent.symbol,
                    pnl=round(pnl, 2),
                    reason=intent.signal.reason,
                )

        elif intent.is_entry:
            if intent.evaluation is None or intent.evaluation.sizing is None:
                continue

            sizing = intent.evaluation.sizing
            notional = sizing.qty * intent.signal.close_price

            if notional > state.cash:
                log.warning("mrev_insufficient_cash", symbol=intent.symbol, needed=notional, cash=state.cash)
                continue

            try:
                broker_order = broker.submit_order(
                    symbol=intent.symbol,
                    side=OrderSide.BUY.value,
                    qty=max(1, int(sizing.qty)),
                    submitted_price=intent.signal.close_price,
                )
            except Exception as exc:
                log.error("mrev_entry_failed", symbol=intent.symbol, error=str(exc))
                continue

            if broker_order.status == OrderStatus.FILLED.value:
                import uuid
                state.positions[intent.symbol] = OpenPosition(
                    position_id=str(uuid.uuid4()),
                    symbol=intent.symbol,
                    qty=sizing.qty,
                    entry_price=intent.signal.close_price,
                    entry_datetime=intent.signal.signal_datetime,
                    stop_loss=sizing.stop_price,
                )
                state.cash -= notional
                filled += 1

                log.info(
                    "mrev_position_opened",
                    symbol=intent.symbol,
                    qty=sizing.qty,
                    price=intent.signal.close_price,
                    stop=sizing.stop_price,
                )

    # Update peak equity
    state.peak_equity = max(state.peak_equity, state.total_equity)

    return filled


# ── Backtest simulation ──────────────────────────────────────────────────────

def run_mrev_backtest(
    capital: float = 1000.0,
    bars: int = 500,
    seed: int = 42,
    symbols: list[str] | None = None,
) -> MrevRunState:
    """
    Run a full MREV-1H backtest on synthetic data.

    Args:
        capital: starting capital in USD
        bars:    number of hourly bars to simulate
        seed:    random seed for reproducibility
        symbols: list of symbols to trade (defaults to MREV universe)

    Returns:
        MrevRunState with complete trade history and P&L.
    """
    if symbols is None:
        symbols = MREV_SYMBOLS

    log.info("mrev_backtest_start", capital=capital, bars=bars, symbols=len(symbols))

    # Generate synthetic data
    raw_data = generate_multi_symbol_1h(symbols=symbols, bars=bars, seed=seed)

    # Compute indicators
    indicator_data: dict[str, pd.DataFrame] = {}
    for sym, df in raw_data.items():
        indicator_data[sym] = compute_mrev_indicators(df)

    # Initialize state
    state = MrevRunState(
        initial_capital=capital,
        cash=capital,
        peak_equity=capital,
    )
    broker = DryRunBroker(initial_cash=capital)

    # Warm-up period (need at least 20 bars for Bollinger Bands)
    start_bar = 25

    # Simulate bar-by-bar
    for bar_idx in range(start_bar, bars):
        # Build rows for this bar
        rows: dict[str, pd.Series] = {}
        current_datetime = None

        for sym in symbols:
            df = indicator_data[sym]
            if bar_idx >= len(df):
                continue
            row = df.iloc[bar_idx]
            rows[sym] = row
            if current_datetime is None:
                current_datetime = row["datetime"]

        if not rows or current_datetime is None:
            continue

        # Run pipeline
        pipeline_result = run_mrev_pipeline(
            symbols=symbols,
            rows=rows,
            open_positions=state.open_positions_map(),
            portfolio=state.portfolio_state(),
            as_of_datetime=current_datetime,
        )

        # Execute
        if pipeline_result.exec_plan:
            execute_mrev_intents(pipeline_result.exec_plan, state, broker)

        state.bars_processed += 1

    # Close any remaining open positions at last price
    log.info(
        "mrev_backtest_complete",
        bars_processed=state.bars_processed,
        total_trades=state.total_trades,
        winning=state.winning_trades,
        losing=state.losing_trades,
        final_equity=round(state.total_equity, 2),
        return_pct=round((state.total_equity - capital) / capital * 100, 2),
        open_positions=len(state.positions),
    )

    return state


# ── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(
        description="MREV-1H Mean Reversion Strategy Runner"
    )
    parser.add_argument(
        "--capital", type=float, default=1000.0,
        help="Starting capital in USD (default: 1000)",
    )
    parser.add_argument(
        "--bars", type=int, default=500,
        help="Number of hourly bars to simulate (default: 500)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for synthetic data (default: 42)",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Symbols to trade (default: full MREV universe)",
    )

    args = parser.parse_args()

    state = run_mrev_backtest(
        capital=args.capital,
        bars=args.bars,
        seed=args.seed,
        symbols=args.symbols,
    )

    # Print summary
    print("\n" + "=" * 60)
    print("  MREV-1H BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Initial Capital:  ${state.initial_capital:,.2f}")
    print(f"  Final Equity:     ${state.total_equity:,.2f}")
    return_pct = (state.total_equity - state.initial_capital) / state.initial_capital * 100
    print(f"  Return:           {return_pct:+.2f}%")
    print(f"  Peak Equity:      ${state.peak_equity:,.2f}")
    drawdown = (state.peak_equity - state.total_equity) / state.peak_equity * 100 if state.peak_equity > 0 else 0
    print(f"  Current Drawdown: {drawdown:.2f}%")
    print(f"  Total Trades:     {state.total_trades}")
    print(f"  Winning Trades:   {state.winning_trades}")
    print(f"  Losing Trades:    {state.losing_trades}")
    win_rate = state.winning_trades / state.total_trades * 100 if state.total_trades > 0 else 0
    print(f"  Win Rate:         {win_rate:.1f}%")
    print(f"  Bars Processed:   {state.bars_processed}")
    print(f"  Open Positions:   {len(state.positions)}")
    print("=" * 60)

    if state.closed_trades:
        print("\n  TRADE LOG:")
        print("-" * 60)
        for t in state.closed_trades:
            pnl_str = f"${t['pnl']:+.2f}"
            print(f"  {t['symbol']:10s} | {pnl_str:>10s} | {t['reason']}")

    if state.positions:
        print(f"\n  OPEN POSITIONS ({len(state.positions)}):")
        print("-" * 60)
        for sym, pos in state.positions.items():
            print(f"  {sym:10s} | qty={pos.qty:.4f} | entry=${pos.entry_price:.2f} | stop=${pos.stop_loss:.2f}")

    print()


if __name__ == "__main__":
    main()
