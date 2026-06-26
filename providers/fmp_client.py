"""FMP (Financial Modeling Prep) REST client.

Live market data for swing-trading-agent: split/dividend-adjusted daily bars,
news headlines, the S&P 500 universe + GICS sectors, an earnings calendar, and
quotes. ThetaData handles intraday bars (``thetadata_client``); FMP handles
everything else. Needs ``FMP_API_KEY`` (settings or env).

Built on ``requests`` + ``tenacity`` (both existing deps). Adapted from
~/orb-paper-trade/src/orbpaper/data/fmp_live.py.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Iterable

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/api/v3"


class FMPError(RuntimeError):
    pass


def _resolve_key(api_key: str | None) -> str:
    if api_key:
        return api_key
    try:
        from config.settings import get_settings
        key = getattr(get_settings(), "fmp_api_key", None)
        if key:
            return key
    except Exception:
        pass
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise FMPError("FMP_API_KEY not set (settings.fmp_api_key or env)")
    return key


class FMPClient:
    """Thin FMP REST client. One instance per provider; reuses a session."""

    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self._key = _resolve_key(api_key)
        self._timeout = timeout
        self._session = requests.Session()
        self._sp500_cache: list[dict] | None = None

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((requests.RequestException, FMPError)),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None):
        p = dict(params or {})
        p["apikey"] = self._key
        r = self._session.get(f"{FMP_BASE}{path}", params=p, timeout=self._timeout)
        if r.status_code == 429:
            raise FMPError(f"Rate limited on {path}")
        if r.status_code >= 400:
            raise FMPError(f"HTTP {r.status_code} on {path}: {r.text[:200]}")
        data = r.json()
        if isinstance(data, dict) and data.get("Error Message"):
            raise FMPError(data["Error Message"])
        return data

    # ---------- universe + sectors ----------

    def _sp500_rows(self) -> list[dict]:
        if self._sp500_cache is None:
            rows = self._get("/sp500_constituent")
            self._sp500_cache = rows if isinstance(rows, list) else []
        return self._sp500_cache

    def sp500_constituents(self) -> list[str]:
        return [r["symbol"] for r in self._sp500_rows() if r.get("symbol")]

    def sector_map(self) -> dict[str, str]:
        return {r["symbol"]: r.get("sector", "Unknown")
                for r in self._sp500_rows() if r.get("symbol")}

    # ---------- daily bars (split/dividend-adjusted) ----------

    def daily_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Adjusted daily OHLCV indexed by (tz-naive) date, columns
        open/high/low/close/volume. FMP's ``close`` is split-adjusted."""
        data = self._get(f"/historical-price-full/{symbol}",
                         params={"from": start, "to": end})
        rows = data.get("historical", []) if isinstance(data, dict) else []
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        df = df.set_index("date")[["open", "high", "low", "close", "volume"]].sort_index()
        df = df[df["close"] > 0]
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df

    # ---------- news (raw headlines for the LLM) ----------

    def news(self, tickers: Iterable[str], days_back: int = 2, limit: int = 1000) -> dict[str, list[dict]]:
        """Recent articles per ticker over the last ``days_back`` days.

        Returns dict[ticker] -> [ {title, text, publishedDate, url, site}, ... ]
        (newest first). yfinance had no pre-computed sentiment either — the
        research LLM interprets these headlines qualitatively.
        """
        tickers = list(tickers)
        if not tickers:
            return {}
        end = date.today()
        start = end - timedelta(days=days_back)
        rows = self._get("/stock_news", params={
            "tickers": ",".join(tickers),
            "from": start.isoformat(),
            "to": end.isoformat(),
            "limit": limit,
        })
        out: dict[str, list[dict]] = {t: [] for t in tickers}
        if not isinstance(rows, list):
            return out
        for a in rows:
            sym = a.get("symbol")
            if sym in out:
                out[sym].append({
                    "title": a.get("title", ""),
                    "text": a.get("text", ""),
                    "publishedDate": a.get("publishedDate", ""),
                    "url": a.get("url", ""),
                    "site": a.get("site", ""),
                })
        return out

    # ---------- earnings ----------

    def earnings_days(self, tickers: Iterable[str], horizon_days: int = 90) -> dict[str, int]:
        """Days-to-next-earnings per ticker (positive = upcoming, within horizon).

        One earning-calendar call covers all tickers. Tickers with no upcoming
        earnings in the horizon are omitted.
        """
        wanted = set(tickers)
        if not wanted:
            return {}
        today = date.today()
        rows = self._get("/earning_calendar", params={
            "from": today.isoformat(),
            "to": (today + timedelta(days=horizon_days)).isoformat(),
        })
        out: dict[str, int] = {}
        if not isinstance(rows, list):
            return out
        for r in rows:
            sym = r.get("symbol")
            if sym not in wanted or not r.get("date"):
                continue
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            days = (d - today).days
            if days < 0:
                continue
            if sym not in out or days < out[sym]:
                out[sym] = days
        return out

    def earnings_history(self, symbol: str, limit: int = 40) -> list[dict]:
        """Historical + upcoming earnings (newest first): [{date, eps, epsEstimated}].
        Reported quarters have ``eps`` populated; not-yet-reported rows have eps=None.
        """
        rows = self._get(f"/historical/earning_calendar/{symbol}")
        if not isinstance(rows, list):
            return []
        out = [{"date": r.get("date"), "eps": r.get("eps"), "epsEstimated": r.get("epsEstimated")}
               for r in rows if r.get("date")]
        return out[:limit]

    # ---------- quote ----------

    def quote(self, symbol: str) -> dict | None:
        rows = self._get(f"/quote/{symbol}")
        return rows[0] if isinstance(rows, list) and rows else None

    def quotes(self, symbols: Iterable[str]) -> dict[str, dict]:
        """Batch quotes — FMP accepts comma-separated symbols on /quote."""
        symbols = list(symbols)
        if not symbols:
            return {}
        rows = self._get(f"/quote/{','.join(symbols)}")
        return {r["symbol"]: r for r in rows if r.get("symbol")} if isinstance(rows, list) else {}
