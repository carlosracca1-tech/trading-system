"""
apps/svc_fundamental/earnings.py
Earnings calendar checker — prevents entries near earnings dates.

Rule: Do not enter a position within 2 days BEFORE or 1 day AFTER
an earnings report. Earnings cause unpredictable gaps that can blow
through stop losses.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from apps.svc_fundamental.alpha_vantage import AlphaVantageClient
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


class EarningsChecker:
    """Check if a symbol has upcoming earnings that should block entry."""

    def __init__(self, client: Optional[AlphaVantageClient] = None):
        self.client = client or AlphaVantageClient()
        self._calendar: list[dict] = []
        self._calendar_date: Optional[date] = None

    def _load_calendar(self) -> list[dict]:
        """Lazy-load earnings calendar, refreshed once per day."""
        today = date.today()
        if self._calendar_date != today:
            self._calendar = self.client.get_earnings_calendar()
            self._calendar_date = today
        return self._calendar

    def has_earnings_within(self, symbol: str, days_before: int = 2, days_after: int = 1) -> bool:
        """
        True if the symbol has an earnings report within the exclusion window.

        For ETFs this always returns False (ETFs don't report earnings).
        For crypto symbols (contains '/') this also returns False.
        """
        # ETFs and crypto don't have earnings
        if "/" in symbol:
            return False  # crypto pair like SOL/USD

        calendar = self._load_calendar()
        if not calendar:
            return False  # no data = assume safe (fail-open)

        today = date.today()
        window_start = today - timedelta(days=days_after)
        window_end = today + timedelta(days=days_before)

        base_symbol = symbol.upper()

        for entry in calendar:
            entry_symbol = entry.get("symbol", "").upper()
            if entry_symbol == base_symbol:
                try:
                    report_date = date.fromisoformat(entry.get("reportDate", ""))
                    if window_start <= report_date <= window_end:
                        log.info("earnings_blocked", symbol=symbol,
                                 report_date=str(report_date))
                        return True
                except (ValueError, TypeError):
                    continue

        return False
