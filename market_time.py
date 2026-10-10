"""Market-aware candle timestamp normalization for US equity bars.

Longbridge `timestamp` is the candle open-time label. For US daily candles,
the label is US Eastern midnight represented as a UTC timestamp. It must not be
interpreted as a close time or advanced by 24h/7d. This module derives the
actual scheduled NYSE close for daily/weekly bars and clips intraday bars to
the regular-session close. Only completed bars are returned to the strategy.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

import pandas as pd

MARKET_TZ = "America/New_York"
_TF_SECONDS = {"1h": 3600, "4h": 4 * 3600}


def _timestamp_utc(value) -> pd.Timestamp:
    if isinstance(value, (int, float)):
        return pd.to_datetime(value, unit="s", utc=True)
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _calendar(calendar=None):
    if calendar is not None:
        return calendar
    import exchange_calendars as xc
    return xc.get_calendar("XNYS")


def _session_close(cal, session_date: date) -> Optional[pd.Timestamp]:
    label = pd.Timestamp(session_date).normalize()
    try:
        if not cal.is_session(label):
            return None
        close = pd.Timestamp(cal.session_close(label))
        return close.tz_localize("UTC") if close.tzinfo is None else close.tz_convert("UTC")
    except (KeyError, ValueError, TypeError):
        return None


def _bar_close_time(start_utc: pd.Timestamp, timeframe: str, calendar=None) -> Optional[pd.Timestamp]:
    """Return actual exchange close time for a bar, or None for non-session bars."""
    tf = timeframe.lower()
    cal = _calendar(calendar)

    if tf in ("1d", "1w"):
        # Daily/weekly timestamp labels are session dates; never add 24h/7d.
        label_date = start_utc.tz_convert(MARKET_TZ).date()
        if tf == "1d":
            return _session_close(cal, label_date)

        monday = label_date - pd.Timedelta(days=label_date.weekday())
        sunday = monday + pd.Timedelta(days=6)
        try:
            sessions = cal.sessions_in_range(pd.Timestamp(monday), pd.Timestamp(sunday))
            if len(sessions) == 0:
                return None
            last_session = pd.Timestamp(sessions[-1]).normalize()
            close = pd.Timestamp(cal.session_close(last_session))
            return close.tz_localize("UTC") if close.tzinfo is None else close.tz_convert("UTC")
        except (KeyError, ValueError, TypeError):
            return None

    if tf in _TF_SECONDS:
        local_start = start_utc.tz_convert(MARKET_TZ)
        session_date = local_start.date()
        try:
            label = pd.Timestamp(session_date).normalize()
            if not cal.is_session(label):
                return None
            open_time = pd.Timestamp(cal.session_open(label))
            close_time = pd.Timestamp(cal.session_close(label))
            open_time = open_time.tz_localize("UTC") if open_time.tzinfo is None else open_time.tz_convert("UTC")
            close_time = close_time.tz_localize("UTC") if close_time.tzinfo is None else close_time.tz_convert("UTC")
        except (KeyError, ValueError, TypeError):
            return None
        if start_utc < open_time or start_utc >= close_time:
            return None
        return min(start_utc + pd.Timedelta(seconds=_TF_SECONDS[tf]), close_time)

    return None


def candles_to_df(candles: Iterable, timeframe: str, now=None, calendar=None) -> pd.DataFrame:
    """Convert Longbridge candles and keep only completed regular-session bars."""
    rows = []
    for candle in candles:
        try:
            rows.append({
                "time": _timestamp_utc(candle.timestamp),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": int(candle.volume),
            })
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue

    columns = ["time", "open", "high", "low", "close", "volume", "close_time"]
    if not rows:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(rows)
    now_utc = _timestamp_utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
    close_times = [
        _bar_close_time(ts, timeframe, calendar=calendar)
        for ts in df["time"]
    ]
    df["close_time"] = pd.to_datetime(close_times, utc=True)
    df = df.dropna(subset=["close_time"])
    df = df[df["close_time"] <= now_utc]
    df = df.sort_values("close_time", kind="stable").drop_duplicates("close_time", keep="last")
    return df[columns].reset_index(drop=True)
