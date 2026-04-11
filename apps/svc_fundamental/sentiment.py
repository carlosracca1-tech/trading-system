"""
apps/svc_fundamental/sentiment.py
Market sentiment analysis — Fear & Greed Index + news sentiment.

Uses:
  - Alternative.me Crypto Fear & Greed Index (free, no key needed)
  - Alpha Vantage NEWS_SENTIMENT endpoint (needs API key)

Rules:
  - Extreme Fear (< 15): block ALL new entries
  - Fear (15-25): reduce position size to 50%
  - Caution (25-40): reduce position size to 75%
  - Neutral+ (40+): full position size
"""
from __future__ import annotations

import time
from typing import Optional

import requests

from apps.svc_fundamental.alpha_vantage import AlphaVantageClient
from packages.shared.logging_config import get_logger

log = get_logger(__name__)


class SentimentAnalyzer:
    """Analyze market and per-symbol sentiment."""

    def __init__(self, client: Optional[AlphaVantageClient] = None):
        self.client = client or AlphaVantageClient()
        self._fear_greed_cache: Optional[int] = None
        self._fear_greed_time: float = 0.0

    def get_news_sentiment_score(self, symbol: str) -> float:
        """
        Returns aggregate sentiment score for a symbol: -1.0 to 1.0.
        0.0 = neutral (also returned on error / missing data).
        """
        try:
            # Normalize symbol for Alpha Vantage (remove /USD suffix)
            av_ticker = symbol.replace("/", "")
            data = self.client.get_news_sentiment(av_ticker)
            feeds = data.get("feed", [])
            if not feeds:
                return 0.0

            scores = []
            for item in feeds[:10]:
                for ticker_data in item.get("ticker_sentiment", []):
                    if ticker_data.get("ticker", "").upper() == av_ticker.upper():
                        try:
                            scores.append(float(ticker_data["ticker_sentiment_score"]))
                        except (KeyError, ValueError):
                            continue

            if scores:
                avg = sum(scores) / len(scores)
                log.debug("news_sentiment", symbol=symbol, score=round(avg, 3),
                          articles=len(scores))
                return avg
            return 0.0

        except Exception as exc:
            log.warning("news_sentiment_failed", symbol=symbol, error=str(exc))
            return 0.0  # fail-safe: neutral

    def get_fear_greed_index(self) -> int:
        """
        Crypto Fear & Greed Index (0-100).
        Cached for 6 hours. Returns 50 (neutral) on failure.

        Source: Alternative.me (free, no API key needed)
        """
        now = time.time()
        if self._fear_greed_cache is not None and (now - self._fear_greed_time) < 21600:
            return self._fear_greed_cache

        try:
            resp = requests.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=5,
            )
            data = resp.json()
            value = int(data["data"][0]["value"])
            self._fear_greed_cache = value
            self._fear_greed_time = now
            log.info("fear_greed_index", value=value,
                     classification=data["data"][0].get("value_classification", ""))
            return value
        except Exception as exc:
            log.warning("fear_greed_failed", error=str(exc))
            return 50  # fail-safe: neutral
