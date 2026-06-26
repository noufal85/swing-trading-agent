"""ThetaData Terminal v3 REST client — intraday OHLCV bars for live trading.

Reaches the ONE shared ThetaData terminal (do not start a second one — it
steals the account session and 478s every other app). The base URL is
env-configurable so the same code runs on the host (default 127.0.0.1) and
inside a container reaching the host terminal via host.docker.internal:

    THETADATA_HOST=host.docker.internal   (in the agent/api compose service)
    THETADATA_HOST=192.168.68.105         (Mac dev / backtests over the LAN)

Daily bars come from FMP (split/dividend-adjusted); ThetaData serves the
intraday side here. Prices are raw/unadjusted — for the rare case where an
intraday range spans a split, ``get_eod`` applies the standard back-adjustment.
Adapted from ~/vwap-strategies/vwap/thetadata.py (per the thetadata skill:
copy, don't rewrite).
"""
from __future__ import annotations

import io
import logging
import os
import time

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

_HOST = os.environ.get("THETADATA_HOST", "127.0.0.1")
_PORT = os.environ.get("THETADATA_PORT", "25503")
BASE_URL = f"http://{_HOST}:{_PORT}/v3"


def _get_csv(path: str, params: dict) -> pd.DataFrame:
    """GET a CSV endpoint with retries for transient terminal hiccups
    (timeouts/disconnects under concurrent load). HTTP errors (4xx, e.g. 472
    pre-listing) raise immediately — they are not transient."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=120)
            resp.raise_for_status()
            text = resp.text
            if text.startswith("No data found"):
                return pd.DataFrame()
            return pd.read_csv(io.StringIO(text))
        except requests.HTTPError:
            raise
        except (requests.RequestException, pd.errors.ParserError) as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


# Common forward split ratios (n:1) and reverse (1:n). 3:2 is excluded: a -33%
# earnings crash is indistinguishable from a 3:2 split by ratio alone.
_COMMON = [2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50]
_SPLIT_RATIOS = _COMMON + [1 / x for x in _COMMON]


def _adjust_splits(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """Back-adjust raw ThetaData prices for splits (overnight close/open ratio
    snapping to a common split ratio). Prices before the split divide by the
    ratio, volume multiplies, so returns/VWAP stay continuous."""
    if df.empty or "open" not in df.columns:
        return df
    ratio = (df["close"].shift(1) / df["open"]).to_numpy()
    intraday = (df["close"] / df["open"] - 1).abs().to_numpy()
    px_factor = np.ones(len(df))
    vol_factor = np.ones(len(df))
    for i in np.where((ratio > 1.8) | (ratio < 1 / 1.8))[0]:
        snapped = min(_SPLIT_RATIOS, key=lambda x: abs(x - ratio[i]))
        if abs(snapped - ratio[i]) / snapped > 0.035:
            continue  # large move but not a clean ratio -> real price action
        if intraday[i] > 0.15:
            continue  # split days trade normally; crash days keep moving
        logger.info("thetadata: %s %s split %g:1 back-adjusted", symbol, df.index[i].date(), snapped)
        px_factor[:i] /= snapped
        vol_factor[:i] *= snapped
    if (px_factor == 1).all():
        return df
    df = df.copy()
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float) * px_factor
    df["volume"] = df["volume"].astype(float) * vol_factor
    return df


def get_eod(symbol: str, start: str, end: str, adjust: bool = True) -> pd.DataFrame:
    """Daily OHLCV bars indexed by (tz-naive) date, columns open/high/low/close/volume.

    Fetched in <=360-day chunks (API caps at 365). Off-session/zero-price bars
    are dropped; split adjustment applied unless ``adjust=False``. Dividends are
    NOT adjusted. Used for the SPY benchmark and as a daily fallback.
    """
    edges = list(pd.date_range(start, end, freq="360D")) + [pd.Timestamp(end)]
    chunks = []
    for a, b in zip(edges[:-1], edges[1:]):
        try:
            chunks.append(_get_csv("/stock/history/eod", {
                "symbol": symbol,
                "start_date": str(a.date()),
                "end_date": str(b.date()),
            }))
        except requests.HTTPError:
            continue  # pre-listing/rename chunk (472) -> skip
    chunks = [c for c in chunks if not c.empty]
    if not chunks:
        return pd.DataFrame()
    df = pd.concat(chunks)
    df["date"] = pd.to_datetime(df["created"]).dt.normalize()
    df = df[~df["date"].duplicated(keep="last")]
    df = df.set_index("date")[["open", "high", "low", "close", "volume"]].sort_index()
    df = df[(df["close"] > 0) & (df.index.dayofweek < 5)]
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    if adjust:
        df = _adjust_splits(df, symbol)
    return df.loc[start:end]


def get_intraday(symbol: str, start: str, end: str, interval: str = "1m") -> pd.DataFrame:
    """Intraday OHLCV bars indexed by (tz-naive) timestamp, columns
    open/high/low/close/volume (+ native session ``vwap`` when present).

    Fetched fresh (no cache) so live bars are current. ``interval`` e.g.
    ``'1m'``, ``'60m'``. Spans are fetched per-month to stay under the 365-day cap.
    """
    months = pd.period_range(start, end, freq="M")
    frames = []
    for m in months:
        try:
            df = _get_csv("/stock/history/ohlc", {
                "symbol": symbol,
                "start_date": str(m.start_time.date()),
                "end_date": str(min(m.end_time, pd.Timestamp(end)).date()),
                "interval": interval,
            })
        except requests.HTTPError:
            continue
        if df.empty:
            continue
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames)
    keep = [c for c in ["open", "high", "low", "close", "volume", "vwap"] if c in out.columns]
    out = out[keep]
    out = out[out["close"] > 0]
    return out.loc[start:f"{end} 23:59"]
