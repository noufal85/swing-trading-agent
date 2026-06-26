"""
providers/live_provider.py — DataProvider backed by live APIs.

Market data: ThetaData (intraday bars) + FMP (daily bars, quotes, news,
earnings). Alpaca is used only for order execution (see live_broker.py).

Daily bars are cached locally (parquet under .cache/bars) to avoid re-fetching
full history every cycle; subsequent runs fetch only recent days incrementally.
get_universe/get_sector_map use the S&P 500 Wikipedia data (via the screener) —
unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd

from providers.data_provider import DataProvider
from providers import thetadata_client as theta

logger = logging.getLogger(__name__)


class LiveProvider(DataProvider):
    """DataProvider backed by ThetaData (intraday) + FMP (daily/news/earnings/quotes)."""

    _DAILY_ALIASES = {"day", "1d", "daily"}
    _HOURLY_ALIASES = {"hour", "1h", "hourly"}
    _CACHE_DIR = Path(".cache/bars")

    def __init__(self, settings) -> None:
        self._settings = settings
        self._fmp_client = None  # lazy

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    @property
    def _fmp(self):
        if self._fmp_client is None:
            from providers.fmp_client import FMPClient
            key = getattr(self._settings, "fmp_api_key", None)
            self._fmp_client = FMPClient(api_key=key)
        return self._fmp_client

    # ------------------------------------------------------------------
    # Local bar cache (parquet, combined (symbol, date) MultiIndex)
    # ------------------------------------------------------------------

    def _cache_file(self, interval: str) -> Path:
        return self._CACHE_DIR / f"bars_{interval}.parquet"

    def _load_cache(self, interval: str) -> Dict[str, pd.DataFrame]:
        path = self._cache_file(interval)
        if not path.exists():
            return {}
        try:
            df = pd.read_parquet(path)
            if df.empty:
                return {}
            result: Dict[str, pd.DataFrame] = {}
            for sym in df.index.get_level_values("symbol").unique():
                sym_df = df.xs(sym, level="symbol")
                if not sym_df.empty:
                    result[sym] = sym_df
            logger.info("bar cache loaded: %d tickers (%s)", len(result), path)
            return result
        except Exception as exc:
            logger.warning("bar cache load failed: %s", exc)
            return {}

    def _save_cache(self, interval: str, bars: Dict[str, pd.DataFrame]) -> None:
        if not bars:
            return
        try:
            frames = []
            for sym, df in bars.items():
                tagged = df.copy()
                tagged.index.name = "date"
                tagged["symbol"] = sym
                tagged = tagged.set_index("symbol", append=True).reorder_levels(["symbol", "date"])
                frames.append(tagged)
            combined = pd.concat(frames)
            self._CACHE_DIR.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(self._cache_file(interval), index=True)
            logger.info("bar cache saved: %d tickers (%s)", len(bars), self._cache_file(interval))
        except Exception as exc:
            logger.warning("bar cache save failed: %s", exc)

    # ------------------------------------------------------------------
    # DataProvider: bars  (daily -> FMP, hourly -> ThetaData)
    # ------------------------------------------------------------------

    def get_bars(self, symbols: List[str], timeframe: str = "day", end=None) -> dict[str, pd.DataFrame]:
        """OHLCV bars per symbol. Daily from FMP (adjusted), hourly from ThetaData.

        Uses a local parquet cache with incremental fetch: cached symbols fetch
        only recent days; uncached symbols fetch full history.
        """
        if not symbols:
            return {}

        tf = timeframe.lower()
        if tf in self._DAILY_ALIASES:
            interval, period_days = "1d", 730
        elif tf in self._HOURLY_ALIASES:
            interval, period_days = "1h", 180
        else:
            logger.warning("Unknown timeframe '%s', defaulting to daily", timeframe)
            interval, period_days = "1d", 730

        if end is not None:
            end_dt = datetime.strptime(end, "%Y-%m-%d") if isinstance(end, str) else (
                end.replace(tzinfo=None) if getattr(end, "tzinfo", None) else end)
        else:
            end_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        full_start_dt = end_dt - timedelta(days=period_days)

        cached_bars = self._load_cache(interval)
        min_bars_required = 200  # need enough history for the 200MA
        cached_symbols, uncached_symbols = [], []
        for sym in symbols:
            cdf = cached_bars.get(sym)
            (cached_symbols if (cdf is not None and len(cdf) >= min_bars_required) else uncached_symbols).append(sym)

        fetch_end_str = end_dt.strftime("%Y-%m-%d")
        fetched: Dict[str, pd.DataFrame] = {}

        if cached_symbols:
            cache_end = max(cached_bars[s].index[-1] for s in cached_symbols)
            inc_start = (cache_end - timedelta(days=2))
            logger.info("Incremental fetch: %d cached tickers (up to %s)", len(cached_symbols), cache_end.date())
            fetched.update(self._fetch_bars(cached_symbols, inc_start.strftime("%Y-%m-%d"), fetch_end_str, interval))

        if uncached_symbols:
            logger.info("Full fetch: %d uncached tickers (%d days)", len(uncached_symbols), period_days)
            fetched.update(self._fetch_bars(uncached_symbols, full_start_dt.strftime("%Y-%m-%d"), fetch_end_str, interval))

        result: Dict[str, pd.DataFrame] = {}
        for sym in set(symbols):
            cdf, fdf = cached_bars.get(sym), fetched.get(sym)
            if cdf is not None and fdf is not None:
                combined = pd.concat([cdf, fdf])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                result[sym] = combined[combined.index >= pd.Timestamp(full_start_dt)]
            elif fdf is not None:
                result[sym] = fdf
            elif cdf is not None:
                result[sym] = cdf

        if fetched:
            self._save_cache(interval, result)
        logger.info("get_bars: %d/%d symbols with data", len(result), len(symbols))
        return result

    def _fetch_bars(self, symbols: List[str], start_str: str, end_str: str, interval: str) -> Dict[str, pd.DataFrame]:
        """Fetch bars per symbol: daily -> FMP, hourly -> ThetaData (60m)."""
        result: Dict[str, pd.DataFrame] = {}
        if interval == "1d":
            for sym in symbols:
                try:
                    df = self._fmp.daily_bars(sym, start_str, end_str)
                    if not df.empty:
                        result[sym] = df
                except Exception as exc:
                    logger.debug("FMP daily fetch failed for %s: %s", sym, exc)
        else:
            for sym in symbols:
                try:
                    df = theta.get_intraday(sym, start_str, end_str, interval="60m")
                    if not df.empty:
                        result[sym] = df[["open", "high", "low", "close", "volume"]]
                except Exception as exc:
                    logger.debug("ThetaData intraday fetch failed for %s: %s", sym, exc)
        return result

    # ------------------------------------------------------------------
    # DataProvider: quotes / snapshots  (FMP /quote)
    # ------------------------------------------------------------------

    def get_quotes(self, symbols: List[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        ts = datetime.now(timezone.utc).isoformat()
        out: Dict[str, dict] = {}
        try:
            quotes = self._fmp.quotes(symbols)
        except Exception as exc:
            logger.warning("FMP quotes failed: %s", exc)
            return {}
        for sym, q in quotes.items():
            price = q.get("price")
            if price is None:
                continue
            price = float(price)
            out[sym] = {
                "ask_price": price, "bid_price": price, "mid_price": price,
                "timestamp": ts, "prev_close": float(q.get("previousClose") or price),
            }
        return out

    def get_snapshots(self, symbols: List[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        out: Dict[str, dict] = {}
        try:
            quotes = self._fmp.quotes(symbols)
        except Exception as exc:
            logger.warning("FMP snapshots failed: %s", exc)
            return {}
        for sym, q in quotes.items():
            price = q.get("price")
            if price is None:
                continue
            price = float(price)
            out[sym] = {
                "latest_price": price,
                "today_open": float(q.get("open") or price),
                "today_high": float(q.get("dayHigh") or price),
                "today_low": float(q.get("dayLow") or price),
                "today_close": price,
                "today_volume": float(q.get("volume") or 0.0),
                "prev_close": float(q.get("previousClose") or price),
                "prev_volume": float(q.get("avgVolume") or 0.0),
                "ask_price": price, "bid_price": price, "mid_price": price,
            }
        return out

    # ------------------------------------------------------------------
    # DataProvider: news  (FMP via tools.sentiment.news)
    # ------------------------------------------------------------------

    def get_news(self, tickers: List[str], hours_back: int = 24) -> dict:
        from tools.sentiment.news import fetch_and_score_news, clear_article_cache
        clear_article_cache()
        if not tickers:
            return {}
        try:
            return fetch_and_score_news(tickers, hours_back=hours_back)
        except Exception as exc:
            logger.warning("LiveProvider.get_news failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # DataProvider: earnings  (FMP earnings calendar)
    # ------------------------------------------------------------------

    def get_earnings(self, tickers: List[str]) -> dict[str, int]:
        if not tickers:
            return {}
        try:
            return self._fmp.earnings_days(tickers)
        except Exception as exc:
            logger.warning("LiveProvider.get_earnings failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # DataProvider: universe / sectors  (S&P 500 Wikipedia — unchanged)
    # ------------------------------------------------------------------

    def get_universe(self) -> List[str]:
        try:
            from tools.data.screener import get_sp500_tickers
            return get_sp500_tickers()
        except Exception as exc:
            logger.warning("LiveProvider.get_universe failed: %s", exc)
            return []

    def get_sector_map(self) -> dict[str, str]:
        try:
            from tools.data.screener import get_sp500_sector_map
            return get_sp500_sector_map()
        except Exception as exc:
            logger.warning("LiveProvider.get_sector_map failed: %s", exc)
            return {}
