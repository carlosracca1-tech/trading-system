"""
apps/svc_fundamental/alpha_vantage.py
Alpha Vantage API client with aggressive caching.

Free tier: 25 requests/day — cache everything possible.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from packages.shared.logging_config import get_logger

log = get_logger(__name__)

CACHE_DIR = Path(os.getenv("FUNDAMENTAL_CACHE_DIR", "cache/fundamental"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class AlphaVantageClient:
    """
    Thin wrapper around the Alpha Vantage REST API.
    All responses are cached to disk to stay within the 25 req/day free limit.
    """
    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY", "")
        if not self.api_key:
            log.warning("alpha_vantage_no_api_key",
                        msg="ALPHA_VANTAGE_API_KEY not set — fundamental checks disabled")

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_json(self, function: str, **params) -> dict:
        """Raw GET → JSON dict."""
        params["function"] = function
        params["apikey"] = self.api_key
        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.error("alpha_vantage_request_failed", function=function, error=str(exc))
            return {}

    def _cached_get(self, cache_key: str, ttl_hours: float, function: str, **params) -> dict:
        """GET with disk cache."""
        cache_file = CACHE_DIR / f"{cache_key}.json"
        if cache_file.exists():
            age_s = time.time() - cache_file.stat().st_mtime
            if age_s < ttl_hours * 3600:
                try:
                    return json.loads(cache_file.read_text())
                except Exception:
                    pass  # stale/corrupt cache — re-fetch

        data = self._get_json(function, **params)
        if data:
            try:
                cache_file.write_text(json.dumps(data))
            except Exception:
                pass
        return data

    # ── Public API methods ───────────────────────────────────────────────────

    def get_earnings_calendar(self, horizon: str = "3month") -> list[dict]:
        """
        Download CSV earnings calendar.
        Returns list of dicts with keys: symbol, name, reportDate, etc.
        Cached for 12 hours.
        """
        cache_file = CACHE_DIR / "earnings_calendar.json"
        if cache_file.exists():
            age_s = time.time() - cache_file.stat().st_mtime
            if age_s < 12 * 3600:
                try:
                    return json.loads(cache_file.read_text())
                except Exception:
                    pass

        if not self.is_configured:
            return []

        try:
            params = {
                "function": "EARNINGS_CALENDAR",
                "horizon": horizon,
                "apikey": self.api_key,
            }
            resp = requests.get(self.BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            entries = list(reader)
            cache_file.write_text(json.dumps(entries))
            log.info("earnings_calendar_fetched", entries=len(entries))
            return entries
        except Exception as exc:
            log.error("earnings_calendar_failed", error=str(exc))
            return []

    def get_news_sentiment(self, tickers: str, limit: int = 10) -> dict:
        """
        News sentiment for one or more tickers (comma-separated).
        Cached for 4 hours per ticker set.
        """
        if not self.is_configured:
            return {}
        cache_key = f"news_{tickers.replace('/', '_').replace(',', '_')}"
        return self._cached_get(cache_key, 4.0, "NEWS_SENTIMENT",
                                tickers=tickers, limit=str(limit))

    def get_company_overview(self, symbol: str) -> dict:
        """
        Fundamentals overview for a symbol.
        Cached for 7 days (168 hours) — doesn't change often.
        """
        if not self.is_configured:
            return {}
        cache_key = f"overview_{symbol}"
        return self._cached_get(cache_key, 168.0, "OVERVIEW", symbol=symbol)
