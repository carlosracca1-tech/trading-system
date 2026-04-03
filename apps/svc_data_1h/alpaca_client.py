"""
apps/svc_data_1h/alpaca_client.py
Alpaca REST client — 1-hour OHLCV bars for stocks and crypto.

Uses Alpaca's Market Data API v2:
  - Stocks:  https://data.alpaca.markets/v2/stocks/{symbol}/bars
  - Crypto:  https://data.alpaca.markets/v1beta3/crypto/us/bars

Rate limits: generous for paid accounts (200 req/min)

Usage:
    client = AlpacaDataClient(api_key="...", secret_key="...")
    bars = client.get_1h_bars("BTC/USD", hours_back=200)
    bars = client.get_1h_bars("SPY", hours_back=200)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pandas as pd

from packages.shared.logging_config import get_logger

log = get_logger(__name__)

_STOCK_DATA_URL = "https://data.alpaca.markets"
_CRYPTO_DATA_URL = "https://data.alpaca.markets"
_MAX_RETRIES = 3
_RETRY_BACKOFF = [2, 5, 15]


@dataclass
class HourlyBar:
    """Normalized 1-hour OHLCV bar."""
    symbol: str
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None


class AlpacaDataClient:
    """
    Synchronous Alpaca Market Data client for 1H bars.
    Handles both stocks and crypto through the appropriate endpoints.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        timeout: float = 30.0,
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")

        self._api_key = api_key
        self._secret_key = secret_key
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
                "User-Agent": "trading-system-mrev/0.1.0",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AlpacaDataClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_1h_bars(
        self,
        symbol: str,
        hours_back: int = 200,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """
        Fetch 1-hour OHLCV bars for a single symbol.

        Args:
            symbol:     ticker (e.g., "SPY", "BTC/USD")
            hours_back: number of hourly bars to fetch
            end:        end datetime (defaults to now UTC)

        Returns:
            DataFrame with columns: [datetime, symbol, open, high, low, close, volume]
        """
        is_crypto = "/" in symbol
        end = end or datetime.now(tz=timezone.utc)
        start = end - timedelta(hours=hours_back)

        if is_crypto:
            bars = self._fetch_crypto_bars(symbol, start, end)
        else:
            bars = self._fetch_stock_bars(symbol, start, end)

        if not bars:
            log.warning("alpaca_no_bars", symbol=symbol, hours_back=hours_back)
            return pd.DataFrame(columns=["datetime", "symbol", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame([
            {
                "datetime": b.datetime,
                "symbol": b.symbol,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ])

        df = df.sort_values("datetime").reset_index(drop=True)
        log.info("alpaca_bars_fetched", symbol=symbol, count=len(df))
        return df

    def get_multi_symbol_1h(
        self,
        symbols: list[str],
        hours_back: int = 200,
    ) -> dict[str, pd.DataFrame]:
        """
        Fetch 1H bars for multiple symbols.

        Returns:
            {symbol: DataFrame} dict
        """
        result = {}
        for sym in symbols:
            try:
                df = self.get_1h_bars(sym, hours_back=hours_back)
                if not df.empty:
                    result[sym] = df
            except Exception as exc:
                log.error("alpaca_fetch_error", symbol=sym, error=str(exc))
        return result

    def get_account_info(self) -> dict:
        """Fetch account info from the trading API (paper or live)."""
        # Use the paper trading endpoint
        url = "https://paper-api.alpaca.markets/v2/account"
        response = self._client.get(url)
        response.raise_for_status()
        return response.json()

    def get_latest_quote(self, symbol: str) -> float | None:
        """Get the latest quote price for a symbol."""
        is_crypto = "/" in symbol
        try:
            if is_crypto:
                # Crypto latest quote
                alpaca_sym = symbol.replace("/", "")  # BTC/USD → BTCUSD
                url = f"{_CRYPTO_DATA_URL}/v1beta3/crypto/us/latest/quotes"
                response = self._client.get(url, params={"symbols": alpaca_sym})
                response.raise_for_status()
                data = response.json()
                quotes = data.get("quotes", {})
                quote = quotes.get(alpaca_sym, {})
                return float(quote.get("ap", 0)) or None  # ask price
            else:
                url = f"{_STOCK_DATA_URL}/v2/stocks/{symbol}/quotes/latest"
                response = self._client.get(url)
                response.raise_for_status()
                data = response.json()
                quote = data.get("quote", {})
                return float(quote.get("ap", 0)) or None
        except Exception as exc:
            log.error("alpaca_quote_error", symbol=symbol, error=str(exc))
            return None

    # ── Private: Stock bars ──────────────────────────────────────────────────

    def _fetch_stock_bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[HourlyBar]:
        """Fetch 1H bars from Alpaca Stock API v2."""
        url = f"{_STOCK_DATA_URL}/v2/stocks/{symbol}/bars"
        params: dict[str, Any] = {
            "timeframe": "1Hour",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
            "adjustment": "split",
            "feed": "iex",  # free tier; use "sip" if you have a paid plan
            "sort": "asc",
        }

        return self._fetch_with_retry(url, params, symbol, is_crypto=False)

    # ── Private: Crypto bars ─────────────────────────────────────────────────

    def _fetch_crypto_bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[HourlyBar]:
        """Fetch 1H bars from Alpaca Crypto API v1beta3."""
        # Alpaca crypto uses symbols without slash: BTC/USD → BTCUSD
        alpaca_symbol = symbol.replace("/", "")

        url = f"{_CRYPTO_DATA_URL}/v1beta3/crypto/us/bars"
        params: dict[str, Any] = {
            "symbols": alpaca_symbol,
            "timeframe": "1Hour",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
            "sort": "asc",
        }

        return self._fetch_with_retry(url, params, symbol, is_crypto=True)

    # ── Private: Fetch with retry ────────────────────────────────────────────

    def _fetch_with_retry(
        self,
        url: str,
        params: dict,
        symbol: str,
        is_crypto: bool,
    ) -> list[HourlyBar]:
        """Fetch bars with retry logic."""
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.get(url, params=params)

                if response.status_code == 429:
                    wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    log.warning("alpaca_rate_limited", symbol=symbol, wait=wait)
                    time.sleep(wait)
                    continue

                if response.status_code == 403:
                    raise ValueError(f"Alpaca API key invalid or expired (403) for {symbol}")

                response.raise_for_status()
                data = response.json()

                if is_crypto:
                    return self._parse_crypto_bars(symbol, data)
                else:
                    return self._parse_stock_bars(symbol, data)

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                log.warning("alpaca_network_error", symbol=symbol, attempt=attempt + 1, error=str(exc))
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(wait)
                else:
                    raise

        return []

    # ── Private: Parsers ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_stock_bars(symbol: str, data: dict) -> list[HourlyBar]:
        """Parse Alpaca stock bars response."""
        bars_data = data.get("bars", [])
        bars = []
        for b in bars_data:
            try:
                dt = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                bars.append(HourlyBar(
                    symbol=symbol,
                    datetime=dt,
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=float(b["v"]),
                    trade_count=int(b.get("n", 0)),
                    vwap=float(b["vw"]) if "vw" in b else None,
                ))
            except (KeyError, ValueError) as exc:
                log.warning("alpaca_parse_error", symbol=symbol, bar=str(b), error=str(exc))
        return bars

    @staticmethod
    def _parse_crypto_bars(symbol: str, data: dict) -> list[HourlyBar]:
        """Parse Alpaca crypto bars response (v1beta3 multi-symbol format)."""
        alpaca_sym = symbol.replace("/", "")
        bars_data = data.get("bars", {}).get(alpaca_sym, [])
        bars = []
        for b in bars_data:
            try:
                dt = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                bars.append(HourlyBar(
                    symbol=symbol,
                    datetime=dt,
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=float(b["v"]),
                    trade_count=int(b.get("n", 0)),
                    vwap=float(b["vw"]) if "vw" in b else None,
                ))
            except (KeyError, ValueError) as exc:
                log.warning("alpaca_crypto_parse_error", symbol=symbol, bar=str(b), error=str(exc))
        return bars
