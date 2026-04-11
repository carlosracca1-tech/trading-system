"""
apps/svc_fundamental/checker.py
Fundamental analysis orchestrator.

Single entry point for the pipeline/engine: call can_trade() before
approving an ENTER signal, and should_reduce_size() to adjust position
sizing based on market sentiment.
"""
from __future__ import annotations

from typing import Optional

from apps.svc_fundamental.alpha_vantage import AlphaVantageClient
from apps.svc_fundamental.earnings import EarningsChecker
from apps.svc_fundamental.sentiment import SentimentAnalyzer
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


class FundamentalChecker:
    """
    Orchestrates all fundamental checks before allowing a trade entry.

    Usage:
        checker = FundamentalChecker()
        can, reason = checker.can_trade("AAPL")
        if not can:
            reject(reason)
        multiplier = checker.should_reduce_size()
        final_qty = int(base_qty * multiplier)
    """

    def __init__(self, client: Optional[AlphaVantageClient] = None):
        self._client = client or AlphaVantageClient()
        self.earnings = EarningsChecker(self._client)
        self.sentiment = SentimentAnalyzer(self._client)

    def can_trade(self, symbol: str, *, deep_check: bool = False) -> tuple[bool, str]:
        """
        Determine if a symbol is safe to enter right now.

        Checks (in order):
          1. Earnings proximity (block if earnings within 2 days)
             — uses 1 cached API call for ALL symbols (12h cache)
          2. Market fear level (block if extreme fear < 15)
             — uses free alternative.me API (no Alpha Vantage cost)
          3. News sentiment (ONLY if deep_check=True — costs 1 API call per symbol)
             — block if very negative < -0.3

        The pipeline Stage 0 calls with deep_check=False (cheap filter).
        The risk engine calls with deep_check=True ONLY for symbols that
        already passed technical entry signals (typically 0-3 per day).

        Returns:
            (can_trade: bool, reason: str)
            reason is "fundamental_ok" if safe, otherwise the blocking reason.
        """
        # 1. Earnings check (1 global call, cached 12h — essentially free)
        if self.earnings.has_earnings_within(symbol, days_before=2, days_after=1):
            reason = f"earnings_proximity:{symbol}"
            log.info("fundamental_blocked", symbol=symbol, reason=reason)
            return False, reason

        # 2. Market fear level (free API — no Alpha Vantage cost)
        fg = self.sentiment.get_fear_greed_index()
        if fg < 15:
            reason = f"extreme_fear:{fg}"
            log.info("fundamental_blocked", symbol=symbol, reason=reason)
            return False, reason

        # 3. News sentiment — ONLY for deep checks (saves API quota)
        if deep_check and self._client.is_configured:
            score = self.sentiment.get_news_sentiment_score(symbol)
            if score < -0.3:
                reason = f"negative_news_sentiment:{score:.2f}"
                log.info("fundamental_blocked", symbol=symbol, reason=reason)
                return False, reason

        return True, "fundamental_ok"

    def should_reduce_size(self) -> float:
        """
        Returns a position size multiplier based on market sentiment.

        1.0 = full size (neutral or greedy market)
        0.75 = 3/4 size (cautious — Fear & Greed 25-40)
        0.5 = half size (fearful — Fear & Greed 15-25)

        Called by the position sizer to dynamically adjust risk.
        """
        fg = self.sentiment.get_fear_greed_index()
        if fg < 25:
            log.info("size_reduced_fear", fear_greed=fg, multiplier=0.5)
            return 0.5
        elif fg < 40:
            log.info("size_reduced_caution", fear_greed=fg, multiplier=0.75)
            return 0.75
        else:
            return 1.0
