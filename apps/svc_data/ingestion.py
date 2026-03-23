"""
apps/svc_data/ingestion.py
Full data ingestion pipeline for one or all symbols.

Pipeline per symbol:
  1. Determine date range: last stored date + 1 day → today
  2. Fetch OHLCV bars from Polygon.io
  3. Upsert bars into market_data_daily
  4. Load ALL historical bars for this symbol from DB
  5. Compute indicators (EMA50/200, RSI14, ATR14, volume MA20, 20d-high)
  6. Upsert indicators into indicators_cache

For initial ingestion, fetch LOOKBACK_YEARS of history to
warm up EMA200 (needs 200+ trading days ≈ 1 year of daily bars).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from packages.shared.db import db_session
from packages.shared.exceptions import DataUnavailableError
from packages.shared.logging_config import get_logger
from packages.shared.models.symbol import Symbol

from apps.svc_data.indicators import compute_indicators
from apps.svc_data.polygon_client import PolygonClient
from apps.svc_data.repository import (
    get_all_active_symbols,
    get_latest_date,
    get_market_data,
    get_symbol_by_ticker,
    upsert_daily_bars,
    upsert_indicators,
)

logger = get_logger(__name__)

# 3 years of history gives EMA200 a comfortable warmup period
LOOKBACK_YEARS = 3


@dataclass
class IngestionResult:
    symbol: str
    bars_upserted: int = 0
    indicators_upserted: int = 0
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""
    duration_sec: float = 0.0

    @property
    def success(self) -> bool:
        return not self.error and not self.skipped


@dataclass
class IngestionReport:
    started_at: str = ""
    ended_at: str = ""
    total_symbols: int = 0
    successful: int = 0
    skipped: int = 0
    failed: int = 0
    total_bars: int = 0
    results: list[IngestionResult] = field(default_factory=list)

    def add(self, r: IngestionResult) -> None:
        self.results.append(r)
        self.total_symbols += 1
        if r.error:
            self.failed += 1
        elif r.skipped:
            self.skipped += 1
        else:
            self.successful += 1
            self.total_bars += r.bars_upserted

    def summary(self) -> str:
        return (
            f"IngestionReport: {self.successful}/{self.total_symbols} ok, "
            f"{self.failed} failed, {self.skipped} skipped, "
            f"{self.total_bars} total bars"
        )


class DataIngestionService:
    """
    Orchestrates full ingestion pipeline.

    Usage (one-shot manual run):
        svc = DataIngestionService(polygon_api_key="...", paid_tier=False)
        report = svc.run_all()
        print(report.summary())

    Usage (single symbol):
        result = svc.run_symbol("SPY")
    """

    def __init__(self, polygon_api_key: str, paid_tier: bool = False) -> None:
        self._client = PolygonClient(api_key=polygon_api_key, paid_tier=paid_tier)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DataIngestionService":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def run_all(self, force_full: bool = False) -> IngestionReport:
        """
        Run ingestion for all active symbols.

        Args:
            force_full: If True, ignore last stored date and re-fetch LOOKBACK_YEARS.
        """
        from datetime import datetime, timezone
        report = IngestionReport(
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        with db_session() as session:
            symbols = get_all_active_symbols(session)

        logger.info("ingestion.start", total_symbols=len(symbols), force_full=force_full)

        for sym in symbols:
            result = self.run_symbol(sym.symbol, force_full=force_full)
            report.add(result)
            if result.error:
                logger.error(
                    "ingestion.symbol_failed",
                    symbol=sym.symbol,
                    error=result.error,
                )
            else:
                logger.info(
                    "ingestion.symbol_done",
                    symbol=sym.symbol,
                    bars=result.bars_upserted,
                    indicators=result.indicators_upserted,
                    duration_sec=round(result.duration_sec, 2),
                )

        from datetime import datetime, timezone
        report.ended_at = datetime.now(timezone.utc).isoformat()
        logger.info("ingestion.complete", **{
            "successful": report.successful,
            "failed": report.failed,
            "total_bars": report.total_bars,
        })
        return report

    def run_symbol(self, symbol: str, force_full: bool = False) -> IngestionResult:
        """Run the full ingestion pipeline for a single symbol."""
        t0 = time.monotonic()
        result = IngestionResult(symbol=symbol)

        try:
            with db_session() as session:
                sym_record = get_symbol_by_ticker(session, symbol)
                if sym_record is None:
                    result.skipped = True
                    result.skip_reason = f"Symbol {symbol!r} not found in DB"
                    return result

                # Determine fetch range
                from_date, to_date = self._date_range(session, symbol, force_full)

                if from_date > to_date:
                    result.skipped = True
                    result.skip_reason = f"Data already up to date (last={to_date})"
                    return result

                # Fetch from Polygon
                bars = self._client.get_daily_bars(symbol, from_date, to_date)
                if not bars:
                    result.skipped = True
                    result.skip_reason = f"No bars returned from Polygon for {from_date}–{to_date}"
                    return result

                # Upsert into DB
                bar_dicts = [
                    {
                        "date": b.date,
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                        "vwap": b.vwap,
                        "num_trades": b.num_trades,
                    }
                    for b in bars
                ]
                result.bars_upserted = upsert_daily_bars(
                    session, sym_record.id, symbol, bar_dicts
                )

                # Load full history for indicator computation
                full_df = get_market_data(session, symbol)
                if full_df.empty:
                    result.skipped = True
                    result.skip_reason = "No data in DB after upsert"
                    return result

                # Compute indicators
                df_with_indicators = compute_indicators(full_df)

                # Upsert indicators
                result.indicators_upserted = upsert_indicators(
                    session, sym_record.id, symbol, df_with_indicators
                )

        except DataUnavailableError as exc:
            result.error = str(exc)
        except Exception as exc:
            logger.exception("ingestion.unexpected_error", symbol=symbol, error=str(exc))
            result.error = f"Unexpected error: {exc}"

        result.duration_sec = time.monotonic() - t0
        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    def _date_range(
        self, session: object, symbol: str, force_full: bool
    ) -> tuple[date, date]:
        """
        Determine the (from_date, to_date) to fetch from Polygon.
        - force_full=True: from 3 years ago to yesterday
        - otherwise: from (last_stored_date + 1) to yesterday
        """
        today = date.today()
        to_date = today - timedelta(days=1)  # never fetch "today" (incomplete bar)

        if force_full:
            from_date = today.replace(year=today.year - LOOKBACK_YEARS)
        else:
            last = get_latest_date(session, symbol)  # type: ignore[arg-type]
            if last is None:
                from_date = today.replace(year=today.year - LOOKBACK_YEARS)
            else:
                from_date = last + timedelta(days=1)

        return from_date, to_date
