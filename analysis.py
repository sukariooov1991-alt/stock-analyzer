"""
analysis.py — Cascade Break & Retest
مساران: اختراق يومي/أسبوعي، أو اختراق 4H/يومي.
حالات: green (CALL) / red (PUT) / yellow (انتظار) / gray (لا إشارة)

⚠️ عقد البيانات:
- المستدعي (market_time.py) ينشئ time وclose_time وفق تقويم جلسات NYSE.
- المنطق يستخدم close_time حصراً.
- المستدعي يمرر الشموع المكتملة فقط.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

MIN_RVOL = 1.5
MIN_RR = 2.0
MAX_RETEST_CANDLES = 5
MAX_FIRST_CANDLE_WINDOW = 5
MAX_SECOND_CANDLE_WINDOW = 5
MAX_1H_CONFIRM_CANDLES = 5
BODY_RATIO_MIN = 0.6
ATR_PERIOD = 14
ATR_MULTIPLIER = 0.5
TOLERANCE_PCT = 0.003
TOLERANCE_ATR_MULT = 0.25
BREAKOUT_LOOKBACK = 10
RVOL_PERIOD = 20

MARKET_TZ = "America/New_York"

_REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}
_TIME_COLUMNS = {"time", "close_time"}


def _valid_df(df: Optional[pd.DataFrame]) -> bool:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    if not _REQUIRED_COLUMNS.issubset(df.columns):
        return False
    if not _TIME_COLUMNS.issubset(df.columns):
        return False
    return True


def _sorted_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"], errors="coerce", utc=True)
    out["close_time"] = pd.to_datetime(out["close_time"], errors="coerce", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["time", "close_time", "open", "high", "low", "close", "volume"])
    out = out[(out[["open", "high", "low", "close"]] > 0).all(axis=1) & (out["volume"] >= 0)]
    out = out[(out["high"] >= out[["open", "close", "low"]].max(axis=1)) &
              (out["low"] <= out[["open", "close", "high"]].min(axis=1))]
    out = out.sort_values("close_time", kind="stable")
    out = out.drop_duplicates(subset=["close_time"], keep="last")
    return out.reset_index(drop=True)


def _time_of(df, pos):
    if df is None or "close_time" not in df.columns or pos < 0 or pos >= len(df):
        return None
    return df.iloc[pos]["close_time"]


def _positions_after(df, timestamp, strict=True):
    if "close_time" not in df.columns or timestamp is None:
        return []
    ts = pd.to_datetime(timestamp, errors="coerce", utc=True)
    if pd.isna(ts):
        return []
    mask = df["close_time"] > ts if strict else df["close_time"] >= ts
    return np.flatnonzero(mask.to_numpy()).tolist()


def atr(df, n=ATR_PERIOD):
    if not _valid_df(df):
        return pd.Series(dtype=float)
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def rvol_series(df, n=RVOL_PERIOD):
    if not _valid_df(df):
        return pd.Series(dtype=float)
    avg = df["volume"].astype(float).shift(1).rolling(n, min_periods=n).mean()
    return df["volume"].astype(float) / avg.replace(0, np.nan)


def prev_candle_hl(df, idx=-2):
    if not _valid_df(df) or len(df) < abs(idx):
        return None, None
    row = df.iloc[idx]
    return float(row["high"]), float(row["low"])


def _body_ratio(row):
    rng = max(float(row["high"]) - float(row["low"]), 1e-8)
    return abs(float(row["close"]) - float(row["open"])) / rng


def _is_bullish(row):
    return float(row["close"]) > float(row["open"])


def _is_bearish(row):
    return float(row["close"]) < float(row["open"])


def compute_tolerance(level, atr_val):
    if not np.isfinite(level) or not np.isfinite(atr_val) or atr_val <= 0:
        return 0.0
    return min(abs(level) * TOLERANCE_PCT, atr_val * TOLERANCE_ATR_MULT)


def _now_market():
    return pd.Timestamp.now(tz=MARKET_TZ)


def _completed_rows_as_of(df, now_ny):
    if df is None or df.empty or "close_time" not in df.columns:
        return pd.DataFrame()
    data = _sorted_df(df)
    now = _now_market() if now_ny is None else pd.Timestamp(now_ny)
    if now.tzinfo is None:
        now = now.tz_localize(MARKET_TZ)
    now_utc = now.tz_convert("UTC")
    return data[data["close_time"] <= now_utc]


def get_prev_week_levels(df_weekly, now_ny=None):
    sub = _completed_rows_as_of(df_weekly, now_ny)
    if sub.empty:
        return None, None
    row = sub.iloc[-1]
    return float(row["high"]), float(row["low"])


def get_prev_day_levels(df_daily, now_ny=None):
    sub = _completed_rows_as_of(df_daily, now_ny)
    if sub.empty:
        return None, None
    now = _now_market() if now_ny is None else pd.Timestamp(now_ny)
    if now.tzinfo is None:
        now = now.tz_localize(MARKET_TZ)
    day_start_utc = now.normalize().tz_convert("UTC")
    sub = sub[sub["close_time"] < day_start_utc]
    if sub.empty:
        return None, None
    row = sub.iloc[-1]
    return float(row["high"]), float(row["low"])


def _reference_levels_before(reference_df, timestamp):
    if not _valid_df(reference_df) or timestamp is None:
        return None, None
    ts = pd.to_datetime(timestamp, errors="coerce", utc=True)
    if pd.isna(ts):
        return None, None
    ref = _sorted_df(reference_df)
    prior = ref[ref["close_time"] < ts]
    if prior.empty:
        return None, None
    row = prior.iloc[-1]
    return float(row["high"]), float(row["low"])


def check_breakout_dynamic(df, reference_df, direction, lookback=BREAKOUT_LOOKBACK):
    if not _valid_df(df) or not _valid_df(reference_df):
        return {"broken": False, "reason": "no_data"}
    data = _sorted_df(df)
    refs = _sorted_df(reference_df)
    if len(data) < RVOL_PERIOD + 2:
        return {"broken": False, "reason": "insufficient_data"}
    rv_series = rvol_series(data, RVOL_PERIOD)
    start = max(1, len(data) - max(1, int(lookback)))
    crossing = None
    for i in range(len(data) - 1, start - 1, -1):
        level_high, level_low = _reference_levels_before(refs, data.iloc[i]["close_time"])
        level = level_high if direction == "up" else level_low
        if level is None:
            continue
        prev_close = float(data.iloc[i - 1]["close"])
        close = float(data.iloc[i]["close"])
        crossed = (prev_close <= level and close > level) if direction == "up" else (prev_close >= level and close < level)
        if crossed:
            crossing = (i, float(level))
            break
    if crossing is None:
        return {"broken": False, "reason": "no_fresh_crossing"}
    crossing_idx, level = crossing
    row = data.iloc[crossing_idx]
    rv = rv_series.iloc[crossing_idx]
    body = _body_ratio(row)
    if pd.isna(rv) or float(rv) < MIN_RVOL:
        return {"broken": False, "reason": "latest_crossing_rvol_below_min",
                "break_index": crossing_idx, "level": level,
                "rvol": None if pd.isna(rv) else float(rv)}
    if body < BODY_RATIO_MIN:
        return {"broken": False, "reason": "breakout_body_below_min",
                "break_index": crossing_idx, "level": level,
                "body_ratio": round(body, 3)}
    if direction == "up" and not _is_bullish(row):
        return {"broken": False, "reason": "breakout_candle_not_bullish",
                "break_index": crossing_idx, "level": level}
    if direction == "down" and not _is_bearish(row):
        return {"broken": False, "reason": "breakout_candle_not_bearish",
                "break_index": crossing_idx, "level": level}
    return {"broken": True, "type": direction, "level": level,
            "break_index": crossing_idx, "break_time": _time_of(data, crossing_idx),
            "break_close": float(row["close"]), "rvol": round(float(rv), 2),
            "body_ratio": round(body, 2)}


def find_retest(df, level, direction, break_time=None, atr_val=None,
                max_candles=MAX_RETEST_CANDLES):
    if not _valid_df(df) or len(df) < 5 or level is None:
        return {"retested": False, "status": "no_data"}
    data = _sorted_df(df)
    atr_values = atr(data, ATR_PERIOD)
    if break_time is not None:
        after = _positions_after(data, break_time, strict=True)
    else:
        after = list(range(max(0, len(data) - int(max_candles)), len(data)))
    if not after:
        return {"retested": False, "status": "no_candles_after"}
    positions = after[:int(max_candles)]
    for offset, i in enumerate(positions, start=1):
        row = data.iloc[i]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        if direction == "up" and close < float(level):
            return {"retested": False, "status": "invalidated_before_retest",
                    "violation_idx": i, "violation_time": _time_of(data, i)}
        if direction == "down" and close > float(level):
            return {"retested": False, "status": "invalidated_before_retest",
                    "violation_idx": i, "violation_time": _time_of(data, i)}
        av = atr_val
        if av is None or not np.isfinite(av) or av <= 0:
            av = float(atr_values.iloc[i]) if len(atr_values) > i and pd.notna(atr_values.iloc[i]) else np.nan
        if not np.isfinite(av) or av <= 0:
            continue
        tol = compute_tolerance(float(level), float(av))
        if direction == "up" and float(level) - tol <= low <= float(level) + tol and close > float(level):
            return {"retested": True, "status": "ok", "type": "up", "level": float(level),
                    "retest_idx": i, "retest_time": _time_of(data, i),
                    "retest_high": high, "retest_low": low,
                    "retest_open": float(row["open"]), "retest_close": close,
                    "atr_at_retest": float(av), "tolerance": round(tol, 6),
                    "candles_since_break": offset}
        if direction == "down" and float(level) - tol <= high <= float(level) + tol and close < float(level):
            return {"retested": True, "status": "ok", "type": "down", "level": float(level),
                    "retest_idx": i, "retest_time": _time_of(data, i),
                    "retest_high": high, "retest_low": low,
                    "retest_open": float(row["open"]), "retest_close": close,
                    "atr_at_retest": float(av), "tolerance": round(tol, 6),
                    "candles_since_break": offset}
    return {"retested": False, "status": "no_retest_in_window"}


def check_stability(df, level, direction, from_idx, until_idx=None):
    if not _valid_df(df):
        return {"stable": False, "reason": "no_data"}
    data = _sorted_df(df)
    start = int(from_idx)
    end = len(data) - 1 if until_idx is None else min(int(until_idx), len(data) - 1)
    if start < 0 or start >= len(data):
        return {"stable": False, "reason": "invalid_start_index"}
    if start >= end:
        return {"stable": True, "reason": "no_candles_after"}
    for i in range(start + 1, end + 1):
        close = float(data.iloc[i]["close"])
        if direction == "up" and close < level:
            return {"stable": False, "reason": f"close_below_at_{i}", "violation_idx": i}
        if direction == "down" and close > level:
            return {"stable": False, "reason": f"close_above_at_{i}", "violation_idx": i}
    return {"stable": True, "reason": "no_violation"}


def find_second_candle(df, level, direction, retest_idx,
                       max_window=MAX_SECOND_CANDLE_WINDOW,
                       max_first_window=MAX_FIRST_CANDLE_WINDOW):
    data = _sorted_df(df) if _valid_df(df) else pd.DataFrame()
    if data.empty or retest_idx < 0 or retest_idx >= len(data):
        return {"found": False, "reason": "invalid_retest_index"}
    retest_idx = int(retest_idx)
    first_end = min(len(data) - 1, retest_idx + int(max_first_window))
    first_idx = None
    for i in range(retest_idx + 1, first_end + 1):
        row = data.iloc[i]
        close = float(row["close"])
        if direction == "up" and close < level:
            return {"found": False, "reason": "invalidated_before_first", "violation_idx": i}
        if direction == "down" and close > level:
            return {"found": False, "reason": "invalidated_before_first", "violation_idx": i}
        directional = (_is_bullish(row) and close > level) if direction == "up" else (_is_bearish(row) and close < level)
        if directional and _body_ratio(row) >= BODY_RATIO_MIN:
            first_idx = i
            break
    if first_idx is None:
        return {"found": False, "reason": "no_first_in_window"}
    second_idx = first_idx + 1
    if second_idx >= len(data) or second_idx > first_idx + int(max_window):
        return {"found": False, "reason": "no_second_in_window"}
    row = data.iloc[second_idx]
    close = float(row["close"])
    if direction == "up" and close < level:
        return {"found": False, "reason": "invalidated_before_second", "violation_idx": second_idx}
    if direction == "down" and close > level:
        return {"found": False, "reason": "invalidated_before_second", "violation_idx": second_idx}
    directional = (_is_bullish(row) and close > level) if direction == "up" else (_is_bearish(row) and close < level)
    if not directional or _body_ratio(row) < BODY_RATIO_MIN:
        return {"found": False, "reason": "second_candle_not_confirmed", "second_idx": second_idx}
    return {"found": True, "first_idx": first_idx, "second_idx": second_idx,
            "entry": close, "entry_time": _time_of(data, second_idx),
            "body_ratio": round(_body_ratio(row), 2),
            "candles_after_first": 1}


def find_confirmation_1h(df_1h, retest_info, direction):
    if not _valid_df(df_1h) or not retest_info:
        return {"confirmed": False, "reason": "no_data"}
    data = _sorted_df(df_1h)
    retest_high = retest_info.get("retest_high")
    retest_low = retest_info.get("retest_low")
    retest_time = retest_info.get("retest_time")
    if retest_time is None:
        return {"confirmed": False, "reason": "missing_retest_time"}
    candidates = _positions_after(data, retest_time, strict=True)[:MAX_1H_CONFIRM_CANDLES]
    if not candidates:
        return {"confirmed": False, "reason": "no_1h_after_retest"}
    for i in candidates:
        row = data.iloc[i]
        close = float(row["close"])
        body = _body_ratio(row)
        if direction == "up" and retest_high is not None:
            if close > float(retest_high) and _is_bullish(row) and body >= BODY_RATIO_MIN:
                return {"confirmed": True, "confirm_idx": i, "entry": close,
                        "confirm_time": _time_of(data, i), "body_ratio": round(body, 2)}
        if direction == "down" and retest_low is not None:
            if close < float(retest_low) and _is_bearish(row) and body >= BODY_RATIO_MIN:
                return {"confirmed": True, "confirm_idx": i, "entry": close,
                        "confirm_time": _time_of(data, i), "body_ratio": round(body, 2)}
    return {"confirmed": False, "reason": "no_confirmation"}


def detect_pattern(df, idx, direction):
    if not _valid_df(df) or idx < 1 or idx >= len(df):
        return "none"
    data = _sorted_df(df)
    cur, prev = data.iloc[idx], data.iloc[idx - 1]
    cur_o, cur_c = float(cur["open"]), float(cur["close"])
    cur_h, cur_l = float(cur["high"]), float(cur["low"])
    prev_o, prev_c = float(prev["open"]), float(prev["close"])
    br = _body_ratio(cur)
    if direction == "up":
        if _is_bearish(prev) and _is_bullish(cur) and cur_o <= prev_c and cur_c >= prev_o:
            return "engulfing"
        rng = cur_h - cur_l
        if rng > 0 and (min(cur_o, cur_c) - cur_l) >= 2 * br * rng and br < 0.4:
            return "hammer"
    else:
        if _is_bullish(prev) and _is_bearish(cur) and cur_o >= prev_c and cur_c <= prev_o:
            return "engulfing"
        rng = cur_h - cur_l
        if rng > 0 and (cur_h - max(cur_o, cur_c)) >= 2 * br * rng and br < 0.4:
            return "shooting_star"
    return "none"


def _build_result(direction, stage, entry, target1, target2, stop,
                  level, retest_info, break_info, sr_dict, tfs, pattern=""):
    if any(v is None or not np.isfinite(float(v)) for v in (entry, target1, stop)):
        return None
    entry, target1, stop = float(entry), float(target1), float(stop)
    if direction == "up" and stop >= entry:
        return None
    if direction == "down" and stop <= entry:
        return None
    risk = (entry - stop) if direction == "up" else (stop - entry)
    reward = (target1 - entry) if direction == "up" else (entry - target1)
    if risk <= 0 or reward <= 0:
        return None
    rr = round(reward / risk, 2)
    if rr < MIN_RR:
        return None
    levels = {"entry": round(entry, 2), "stop": round(stop, 2),
              "target1": round(target1, 2), "rr": rr,
              "level_broken": round(float(level), 2), "pattern": pattern}
    if target2 is not None and np.isfinite(float(target2)):
        target2 = float(target2)
        t2a = target2 > entry if direction == "up" else target2 < entry
        t2b = target2 > target1 if direction == "up" else target2 < target1
        if t2a and t2b:
            levels["target2"] = round(target2, 2)
    return {"color": "green" if direction == "up" else "red",
            "label": "تأكيد CALL" if direction == "up" else "تأكيد PUT",
            "status": "confirmed",
            "direction": "call" if direction == "up" else "put",
            "breakout_stage": stage, "levels": levels,
            "break_info": break_info, "retest_info": retest_info,
            "support_resistance": sr_dict, "timeframes": tfs}


def _tf_snapshot(df, label):
    if not _valid_df(df) or len(df) < 5:
        return {"label": label, "support": None, "resistance": None,
                "broke_resistance": False, "broke_support": False}
    data = _sorted_df(df)
    res, sup = prev_candle_hl(data, -2)
    price = float(data.iloc[-1]["close"])
    return {"label": label,
            "support": round(sup, 2) if sup is not None else None,
            "resistance": round(res, 2) if res is not None else None,
            "broke_resistance": res is not None and price > res,
            "broke_support": sup is not None and price < sup}


def _empty_result(sr_dict=None, tfs=None):
    return {"color": "gray", "label": "لا إشارة", "status": "none",
            "direction": None, "breakout_stage": None, "levels": {},
            "timeframes": tfs or [], "support_resistance": sr_dict or {}}


def _waiting_result(label, direction, stage, level, sr_dict, tfs):
    return {"color": "yellow", "label": label, "status": "waiting",
            "direction": "call" if direction == "up" else "put",
            "breakout_stage": stage,
            "levels": {"level_broken": round(float(level), 2)},
            "timeframes": tfs, "support_resistance": sr_dict}


def scan_setup(df_weekly, df_daily, df_4h, df_1h):
    frames = [df_weekly, df_daily, df_4h, df_1h]
    if any(not _valid_df(d) or len(d) < 25 for d in frames):
        return _empty_result()

    df_weekly, df_daily, df_4h, df_1h = [_sorted_df(d) for d in frames]

    now_ny = _now_market()
    display_w_high, display_w_low = get_prev_week_levels(df_weekly, now_ny)
    display_d_high, display_d_low = get_prev_day_levels(df_daily, now_ny)

    sr_dict = {"weekly": {"support": round(display_w_low, 2) if display_w_low is not None else None,
                          "resistance": round(display_w_high, 2) if display_w_high is not None else None},
               "daily": {"support": round(display_d_low, 2) if display_d_low is not None else None,
                         "resistance": round(display_d_high, 2) if display_d_high is not None else None}}
    tfs = [_tf_snapshot(df_weekly, "1W"), _tf_snapshot(df_daily, "1D"),
           _tf_snapshot(df_4h, "4H"), _tf_snapshot(df_1h, "1H")]

    atr4s, atr1s = atr(df_4h), atr(df_1h)
    if len(atr4s) == 0 or len(atr1s) == 0:
        return _empty_result(sr_dict, tfs)

    # Track best partial state
    # priority: 2 = retest done, 1 = breakout only
    partial = None

    # ── المسار الأول: اليومي اخترق الأسبوع
    for direction in ("up", "down"):
        brk = check_breakout_dynamic(df_daily, df_weekly, direction)
        if not brk.get("broken"):
            continue
        level = float(brk["level"])
        if partial is None:
            partial = {"label": "اختراق، بانتظار Retest", "direction": direction,
                       "stage": "daily_break_weekly", "level": level, "priority": 1}
        retest = find_retest(df_4h, level, direction, brk["break_time"])
        if not retest.get("retested"):
            continue
        partial = {"label": "Retest، بانتظار التأكيد", "direction": direction,
                   "stage": "daily_break_weekly", "level": level, "priority": 2}
        second = find_second_candle(df_4h, level, direction, retest["retest_idx"])
        if not second.get("found"):
            continue
        if int(second["second_idx"]) != len(df_4h) - 1:
            continue
        atr_4h = float(atr4s.iloc[int(second["second_idx"])]) if pd.notna(atr4s.iloc[int(second["second_idx"])]) else np.nan
        if not np.isfinite(atr_4h) or atr_4h <= 0:
            continue
        stab = check_stability(df_4h, level, direction, retest["retest_idx"], second["second_idx"])
        if not stab["stable"]:
            continue
        entry = second["entry"]
        entry_time = pd.to_datetime(second["entry_time"], utc=True)
        daily_break_row = df_daily.iloc[int(brk["break_index"])]
        target1 = float(daily_break_row["high"] if direction == "up" else daily_break_row["low"])
        start_time = pd.to_datetime(brk["break_time"], utc=True)
        prior_rows = df_4h[(df_4h["close_time"] < entry_time) & (df_4h["close_time"] >= start_time)]
        if not prior_rows.empty:
            cont = float(prior_rows["high"].max() if direction == "up" else prior_rows["low"].min())
            target1 = max(target1, cont) if direction == "up" else min(target1, cont)
        if target1 is None:
            continue
        stop = (retest["retest_low"] - ATR_MULTIPLIER * atr_4h if direction == "up"
                else retest["retest_high"] + ATR_MULTIPLIER * atr_4h)
        result = _build_result(direction, "daily_break_weekly", entry, target1, None,
                               stop, level, retest, brk, sr_dict, tfs,
                               detect_pattern(df_4h, second["second_idx"], direction))
        if result:
            return result

    # ── المسار الثاني: 4H اخترق اليوم
    last_daily_row = df_daily.iloc[-1]
    pw_h, pw_l = _reference_levels_before(df_weekly, last_daily_row["close_time"])
    p2_dirs = []
    if pw_h is not None and float(last_daily_row["close"]) <= pw_h:
        p2_dirs.append("up")
    if pw_l is not None and float(last_daily_row["close"]) >= pw_l:
        p2_dirs.append("down")
    for direction in p2_dirs:
        brk = check_breakout_dynamic(df_4h, df_daily, direction)
        if not brk.get("broken"):
            continue
        level = float(brk["level"])
        if partial is None or partial.get("priority", 0) < 1:
            partial = {"label": "اختراق، بانتظار Retest", "direction": direction,
                       "stage": "4h_break_daily", "level": level, "priority": 1}
        retest = find_retest(df_4h, level, direction, brk["break_time"])
        if not retest.get("retested"):
            continue
        partial = {"label": "Retest، بانتظار التأكيد", "direction": direction,
                   "stage": "4h_break_daily", "level": level, "priority": 2}
        confirm = find_confirmation_1h(df_1h, retest, direction)
        if not confirm.get("confirmed"):
            continue
        if int(confirm["confirm_idx"]) != len(df_1h) - 1:
            continue
        atr_1h = float(atr1s.iloc[int(confirm["confirm_idx"])]) if pd.notna(atr1s.iloc[int(confirm["confirm_idx"])]) else np.nan
        if not np.isfinite(atr_1h) or atr_1h <= 0:
            continue
        confirm_time = confirm.get("confirm_time")
        if confirm_time is None:
            continue
        eligible = np.flatnonzero((df_4h["close_time"] <= pd.to_datetime(confirm_time, utc=True)).to_numpy())
        stability_end = int(eligible[-1]) if len(eligible) else int(retest["retest_idx"])
        stab = check_stability(df_4h, level, direction, retest["retest_idx"], stability_end)
        if not stab["stable"]:
            continue
        entry = confirm["entry"]
        confirm_ts = pd.to_datetime(confirm_time, utc=True)
        start_ts = pd.to_datetime(brk["break_time"], utc=True)
        prior_rows = df_4h[(df_4h["close_time"] < confirm_ts) & (df_4h["close_time"] >= start_ts)]
        if prior_rows.empty:
            continue
        target1 = (float(prior_rows["high"].max()) if direction == "up"
                   else float(prior_rows["low"].min()))
        t2h, t2l = _reference_levels_before(df_weekly, brk["break_time"])
        target2 = t2h if direction == "up" else t2l
        stop = (retest["retest_low"] - ATR_MULTIPLIER * atr_1h if direction == "up"
                else retest["retest_high"] + ATR_MULTIPLIER * atr_1h)
        result = _build_result(direction, "4h_break_daily", entry, target1, target2,
                               stop, level, retest, brk, sr_dict, tfs,
                               detect_pattern(df_1h, confirm["confirm_idx"], direction))
        if result:
            return result

    # No complete signal
    if partial is not None:
        return _waiting_result(partial["label"], partial["direction"],
                               partial["stage"], partial["level"], sr_dict, tfs)
    return _empty_result(sr_dict, tfs)


def last_price(df):
    if not _valid_df(df):
        return None
    data = _sorted_df(df)
    return float(data.iloc[-1]["close"]) if not data.empty else None
