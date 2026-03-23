"""
apps/svc_risk/rules.py
RFTM Risk Rules — P1 through P4.

Each rule exposes a single check(...) method that returns (passed: bool, reason: str).
Rules are evaluated in priority order by the engine (P1 first — kill switch).

Rule hierarchy
--------------
P1  MAX_DRAWDOWN     Portfolio drawdown ≥ 15 %  → kill switch (reject everything)
P2  MAX_POSITIONS    Open positions ≥ 10         → no new entries
P3  MAX_POSITION_PCT Single position > 10 %      → position too large
P4  MIN_SHARES       Sized position = 0 shares   → too small to execute
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ── Thresholds (match RISK_PARAMS in position_sizer.py) ──────────────────────
DEFAULT_MAX_DRAWDOWN_PCT = 0.15    # 15 % peak-to-trough → kill switch
DEFAULT_MAX_POSITIONS = 10         # concurrent open positions
DEFAULT_MAX_POSITION_PCT = 0.10   # max 10 % of portfolio per position


# ── Rule base & result type ───────────────────────────────────────────────────

@dataclass
class RuleResult:
    passed: bool
    rule_code: str
    reason: str = ""


# ── P1: Portfolio max drawdown ────────────────────────────────────────────────

class P1MaxDrawdown:
    """
    Kill switch: reject ALL new entries if current portfolio drawdown
    from its peak exceeds `max_drawdown_pct`.

    Does NOT block EXIT signals — exiting a losing position is always allowed.
    """
    rule_code = "P1_MAX_DRAWDOWN"

    def __init__(self, max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT):
        self.max_drawdown_pct = max_drawdown_pct

    def check(self, peak_equity: float, current_equity: float) -> RuleResult:
        """
        Args:
            peak_equity:    highest portfolio equity reached in this run
            current_equity: current total equity

        Returns:
            passed=True if drawdown is within limit, False = kill switch triggered.
        """
        if peak_equity <= 0:
            return RuleResult(passed=True, rule_code=self.rule_code)

        drawdown = (peak_equity - current_equity) / peak_equity
        if drawdown >= self.max_drawdown_pct:
            return RuleResult(
                passed=False,
                rule_code=self.rule_code,
                reason=(
                    f"drawdown_{drawdown:.2%}_exceeds_limit_{self.max_drawdown_pct:.2%}"
                ),
            )
        return RuleResult(passed=True, rule_code=self.rule_code)


# ── P2: Max concurrent positions ─────────────────────────────────────────────

class P2MaxPositions:
    """
    Reject new ENTER signals when open position count is at the maximum.
    Always passes for EXIT signals (we want to close positions).
    """
    rule_code = "P2_MAX_POSITIONS"

    def __init__(self, max_positions: int = DEFAULT_MAX_POSITIONS):
        self.max_positions = max_positions

    def check(self, open_position_count: int) -> RuleResult:
        """
        Args:
            open_position_count: number of currently open positions

        Returns:
            passed=True if there is room for one more position.
        """
        if open_position_count >= self.max_positions:
            return RuleResult(
                passed=False,
                rule_code=self.rule_code,
                reason=(
                    f"open_positions_{open_position_count}"
                    f"_at_max_{self.max_positions}"
                ),
            )
        return RuleResult(passed=True, rule_code=self.rule_code)


# ── P3: Max single-position size ─────────────────────────────────────────────

class P3MaxPositionSize:
    """
    Reject if the proposed notional value of the new position would exceed
    `max_position_pct` of the current portfolio value.
    """
    rule_code = "P3_MAX_POSITION_SIZE"

    def __init__(self, max_position_pct: float = DEFAULT_MAX_POSITION_PCT):
        self.max_position_pct = max_position_pct

    def check(self, notional_value: float, portfolio_value: float) -> RuleResult:
        """
        Args:
            notional_value:  proposed position value (shares × close_price)
            portfolio_value: current total equity

        Returns:
            passed=True if the position is within the size limit.
        """
        if portfolio_value <= 0:
            return RuleResult(
                passed=False,
                rule_code=self.rule_code,
                reason="portfolio_value_non_positive",
            )

        position_pct = notional_value / portfolio_value
        if position_pct > self.max_position_pct:
            return RuleResult(
                passed=False,
                rule_code=self.rule_code,
                reason=(
                    f"position_pct_{position_pct:.2%}"
                    f"_exceeds_max_{self.max_position_pct:.2%}"
                ),
            )
        return RuleResult(passed=True, rule_code=self.rule_code)


# ── P4: Minimum viable shares ─────────────────────────────────────────────────

class P4MinShares:
    """
    Reject if the position sizer returned 0 shares (trade not executable).
    """
    rule_code = "P4_MIN_SHARES"

    def check(self, shares: int, rejection_reason: Optional[str] = None) -> RuleResult:
        """
        Args:
            shares:           proposed share quantity from position_sizer
            rejection_reason: reason the sizer already returned (if any)

        Returns:
            passed=True if shares >= 1.
        """
        if shares < 1:
            reason = rejection_reason or "position_size_rounds_to_zero"
            return RuleResult(
                passed=False,
                rule_code=self.rule_code,
                reason=reason,
            )
        return RuleResult(passed=True, rule_code=self.rule_code)
