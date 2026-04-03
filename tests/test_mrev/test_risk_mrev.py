"""
Tests for MREV-1H risk engine and position sizer.
"""
from __future__ import annotations

from datetime import datetime, timezone

from apps.svc_risk_mrev.engine import (
    MrevEvaluationResult,
    MrevPortfolioState,
    evaluate_mrev_signal,
)
from apps.svc_risk_mrev.position_sizer import (
    MrevSizingResult,
    calculate_mrev_position_size,
)
from apps.svc_strategy_mrev.scanner import MrevSignalDecision
from packages.shared.enums import RiskDecision, SignalType


NOW = datetime(2026, 4, 1, 14, 0, 0, tzinfo=timezone.utc)


class TestMrevPositionSizer:
    """Test MREV position sizing with aggressive parameters."""

    def test_basic_sizing_etf(self):
        result = calculate_mrev_position_size(
            portfolio_value=1000.0,
            close_price=50.0,
            atr_14=1.0,
            symbol="SPY",
        )
        assert result.qty > 0
        assert result.rejection_reason is None
        assert result.is_crypto is False

    def test_crypto_fractional_sizing(self):
        result = calculate_mrev_position_size(
            portfolio_value=1000.0,
            close_price=60000.0,
            atr_14=500.0,
            symbol="BTC/USD",
        )
        assert result.is_crypto is True
        # With $1000 portfolio, 2% risk = $20, stop_dist = 1.5*500 = $750
        # shares_risk = 20/750 ≈ 0.0266, max = 250/60000 ≈ 0.0041
        # Should be capped by max position size
        assert result.qty > 0
        assert result.qty < 1  # fractional BTC

    def test_rejects_zero_portfolio(self):
        result = calculate_mrev_position_size(0.0, 100.0, 2.0, "SPY")
        assert result.qty == 0
        assert result.rejection_reason == "portfolio_value_non_positive"

    def test_rejects_zero_atr(self):
        result = calculate_mrev_position_size(1000.0, 100.0, 0.0, "SPY")
        assert result.qty == 0
        assert result.rejection_reason == "atr_non_positive"

    def test_min_order_usd_check(self):
        """Very small portfolio should reject if order < $10."""
        result = calculate_mrev_position_size(
            portfolio_value=10.0,  # tiny
            close_price=60000.0,
            atr_14=500.0,
            symbol="BTC/USD",
        )
        assert result.qty == 0
        assert "order_below_minimum" in (result.rejection_reason or "")

    def test_stop_price_correct(self):
        result = calculate_mrev_position_size(1000.0, 100.0, 2.0, "SPY")
        expected_stop = 100.0 - 1.5 * 2.0  # 97.0
        assert abs(result.stop_price - 97.0) < 0.01

    def test_risk_per_trade_respected(self):
        """Risk amount should not exceed 2% of portfolio."""
        result = calculate_mrev_position_size(1000.0, 50.0, 1.0, "SPY")
        assert result.risk_amount <= 1000.0 * 0.02 + 0.01  # small tolerance

    def test_max_position_pct_respected(self):
        """Notional should not exceed 25% of portfolio."""
        result = calculate_mrev_position_size(1000.0, 10.0, 0.1, "QQQ")
        assert result.pct_of_portfolio <= 0.25 + 0.001


class TestMrevRiskEngine:
    """Test the MREV risk evaluation engine."""

    def _make_enter_signal(self, symbol="BTC/USD", close=95.0, atr=2.0):
        return MrevSignalDecision(
            symbol=symbol,
            signal_datetime=NOW,
            signal_type=SignalType.ENTER.value,
            close_price=close,
            atr_14=atr,
            rsi_14=25.0,
            sma_20=100.0,
        )

    def _make_exit_signal(self, symbol="BTC/USD"):
        return MrevSignalDecision(
            symbol=symbol,
            signal_datetime=NOW,
            signal_type=SignalType.EXIT.value,
            close_price=100.0,
        )

    def _make_portfolio(self, equity=1000.0, peak=1000.0, positions=0, cash=1000.0):
        return MrevPortfolioState(
            total_equity=equity,
            peak_equity=peak,
            open_position_count=positions,
            cash=cash,
        )

    def test_exit_always_approved(self):
        result = evaluate_mrev_signal(
            self._make_exit_signal(), self._make_portfolio()
        )
        assert result.decision == RiskDecision.APPROVED.value

    def test_hold_is_deferred(self):
        signal = MrevSignalDecision(
            symbol="BTC/USD", signal_datetime=NOW,
            signal_type=SignalType.HOLD.value, close_price=100.0,
        )
        result = evaluate_mrev_signal(signal, self._make_portfolio())
        assert result.decision == RiskDecision.DEFERRED.value

    def test_enter_approved_healthy_portfolio(self):
        result = evaluate_mrev_signal(
            self._make_enter_signal(symbol="SPY", close=50.0, atr=1.0),
            self._make_portfolio(),
        )
        assert result.decision == RiskDecision.APPROVED.value
        assert result.sizing is not None
        assert result.sizing.qty > 0

    def test_p1_drawdown_rejects(self):
        """20% drawdown should trigger kill switch."""
        result = evaluate_mrev_signal(
            self._make_enter_signal(symbol="SPY", close=50.0, atr=1.0),
            self._make_portfolio(equity=800.0, peak=1000.0),  # 20% drawdown
        )
        assert result.decision == RiskDecision.REJECTED.value
        assert result.rule_code == "P1_MAX_DRAWDOWN"

    def test_p2_max_positions_rejects(self):
        """4 positions should block new entries."""
        result = evaluate_mrev_signal(
            self._make_enter_signal(symbol="SPY", close=50.0, atr=1.0),
            self._make_portfolio(positions=4),
        )
        assert result.decision == RiskDecision.REJECTED.value
        assert result.rule_code == "P2_MAX_POSITIONS"

    def test_missing_atr_rejects(self):
        signal = self._make_enter_signal()
        signal.atr_14 = None
        result = evaluate_mrev_signal(signal, self._make_portfolio())
        assert result.decision == RiskDecision.REJECTED.value
