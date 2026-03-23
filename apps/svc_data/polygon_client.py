"""
apps/svc_data/polygon_client.py
Polygon.io REST client — daily OHLCV bars.

Docs: https://polygon.io/docs/stocks/get_v2_aggs_ticker__stocksticker__range__multiplier__timespan__from__to

Rate limits:
  Free tier:  5 req/min  — adds 12s sleep between calls
  Paid tier:  unlimited  — no sleep (POLYGON_PAID_TIER=true in .env)

Usage:
    client = PolygonClient(api_key="...", paid_tier=False)
    bars = client.get_daily_bars("SPY", date(2023, 1, 1), date(2024, 1, 1))
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from packages.shared.exceptions import DataUnavailableError, DataValidationError
from packages.shared.logging_config import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://api.polygon.io"
_FREE_TIER_SLEEP_SEC = 12.5  # 5 req/min = 12s apart
_MAX_RETRIES = 3
_RETRY_BACKOFF = [2, 5, 15]  # seconds between retries


@dataclass
class DailyBar:
    """Normalized daily OHLCV bar from Polygon."""
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
    num_trades: int | None = None


class PolygonClient:
    """
    Synchronous Polygon.io client.
    Uses httpx in sync mode — consistent with the rest of the system (no asyncio).
    """

    def __init__(self, api_key: str, paid_tier: bool = False, timeout: float = 30.0) -> None:
        if not api_key:
            raise DataUnavailableError(
                "POLYGON_API_KEY is not set. Cannot fetch market data.",
                source="polygon",
            )
        self._api_key = api_key
        self._paid_tier = paid_tier
        self._client = httpx.Client(
            base_url=_BASE_URL,
            timeout=timeout,
            headers={"User-Agent": "trading-system/0.1.0"},
        )
        self._last_request_at: float = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PolygonClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_daily_bars(
        self,
        symbol: str,
        from_date: date,
        to_date: date,
        adjusted: bool = True,
    ) -> list[DailyBar]:
        """
        Fetch daily OHLCV bars for one symbol over a date range.
        Returns bars sorted ascending by date.
        Handles pagination automatically (limit=50000 per request).
        """
        self._rate_limit()

        url = (
            f"/v2/aggs/ticker/{symbol}/range/1/day"
            f"/{from_date.isoformat()}/{to_date.isoformat()}"
        )
        params: dict[str, Any] = {
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": 50000,
            "apiKey": self._api_key,
        }

        bars: list[DailyBar] = []
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.get(url, params=params)
                self._last_request_at = time.monotonic()

                if response.status_code == 429:
                    wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    logger.warning(
                        "polygon.rate_limited",
                        symbol=symbol,
                        attempt=attempt + 1,
                        wait_sec=wait,
                    )
                    time.sleep(wait)
                    continue

                if response.status_code == 403:
                    raise DataUnavailableError(
                        f"Polygon API key invalid or expired (403)",
                        symbol=symbol,
                        source="polygon",
                    )

                response.raise_for_status()
                data = response.json()
                bars = self._parse_bars(symbol, data)

                logger.info(
                    "polygon.fetch_ok",
                    symbol=symbol,
                    from_date=from_date.isoformat(),
                    to_date=to_date.isoformat(),
                    bars_count=len(bars),
                )
                return bars

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                logger.warning(
                    "polygon.network_error",
                    symbol=symbol,
                    attempt=attempt + 1,
                    error=str(exc),
                    retry_in=wait,
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(wait)
                else:
                    raise DataUnavailableError(
                        f"Polygon network error after {_MAX_RETRIES} attempts: {exc}",
                        symbol=symbol,
                        source="polygon",
                    ) from exc

            except httpx.HTTPStatusError as exc:
                raise DataUnavailableError(
                    f"Polygon HTTP error {exc.response.status_code}",
                    symbol=symbol,
                    source="polygon",
                ) from exc

        return bars  # empty if all retries exhausted without success

    # ── Private helpers ───────────────────────────────────────────────────────

    def _rate_limit(self) -> None:
        """Enforce free-tier rate limit: sleep until 12.5s since last request."""
        if self._paid_tier:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _FREE_TIER_SLEEP_SEC:
            sleep_for = _FREE_TIER_SLEEP_SEC - elapsed
            logger.debug("polygon.rate_limit_sleep", sleep_sec=round(sleep_for, 2))
            time.sleep(sleep_for)

    @staticmethod
    def _parse_bars(symbol: str, data: dict[str, Any]) -> list[DailyBar]:
        """Parse Polygon v2/aggs response into DailyBar list."""
        status = data.get("status", "")
        if status not in ("OK", "DELAYED"):
            results_count = data.get("resultsCount", 0)
            if results_count == 0:
                return []  # no data for this range (weekend, holiday, new ETF)
            raise DataValidationError(
                f"Polygon response status={status!r}",
                symbol=symbol,
                source="polygon",
            )

        results = data.get("results", [])
        bars: list[DailyBar] = []
        for r in results:
            try:
                # Polygon timestamp is milliseconds since epoch (UTC midnight)
                ts_ms: int = r["t"]
                bar_date = date.fromtimestamp(ts_ms / 1000)
                bars.append(
                    DailyBar(
                        symbol=symbol,
                        date=bar_date,
                        open=float(r["o"]),
                        high=float(r["h"]),
                        low=float(r["l"]),
                        close=float(r["c"]),
                        volume=int(r["v"]),
                        vwap=float(r["vw"]) if "vw" in r else None,
                        num_trades=int(r["n"]) if "n" in r else None,
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                raise DataValidationError(
                    f"Failed to parse bar: {exc}",
                    symbol=symbol,
                    raw=str(r),
                ) from exc

        return bars
