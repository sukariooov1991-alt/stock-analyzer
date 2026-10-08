"""
analysis.py — الاستراتيجية: RSI + MACD Swing (CALL + PUT)
مع فلاتر: ADX + Volume
"""
import numpy as np
import pandas as pd


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = g / l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def adx(df, n=14):
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)


def rvol(df, n=20):
    if len(df) < n + 1:
        return 1.0
    avg = df["volume"].iloc[-n-1:-1].mean()
    return float(df["volume"].iloc[-1] / avg) if avg > 0 else 1.0


def macd(s, fast=12, slow=26, signal=9):
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


# ============================================================
# ✅ الاستراتيجية الجديدة: RSI + MACD Swing
# ============================================================
def swing_signal(df_weekly, df_daily):
    """
    CALL: Weekly MACD > 0 AND Daily RSI crosses UP through 45
    PUT:  Weekly MACD < 0 AND Daily RSI crosses DOWN through 55
    الفلاتر: ADX > 22 + Volume >= 1.5 × SMA(20)
    """
    # MACD أسبوعي
    w_macd, _, _ = macd(df_weekly["close"])
    weekly_macd = float(w_macd.iloc[-1])

    # RSI يومي
    d_rsi = rsi(df_daily["close"], 14)
    rsi_now  = float(d_rsi.iloc[-1])
    rsi_prev = float(d_rsi.iloc[-2]) if len(d_rsi) > 1 else rsi_now

    # ADX يومي
    d_adx = adx(df_daily, 14)
    adx_now = float(d_adx.iloc[-1])
    adx_ok = adx_now > 22

    # Volume يومي
    vol_now = float(df_daily["volume"].iloc[-1])
    vol_avg = float(df_daily["volume"].iloc[-21:-1].mean()) if len(df_daily) >= 21 else vol_now
    volume_ok = vol_now >= 1.5 * vol_avg if vol_avg > 0 else False

    # ATR يومي
    d_atr = atr(df_daily, 14)
    atr_now = float(d_atr.iloc[-1])

    # ===== تحديد الإشارة =====
    signal = "none"
    direction = None

    if weekly_macd > 0:
        direction = "bullish"
        if rsi_prev < 45 <= rsi_now:
            signal = "call"
        elif rsi_now >= 45:
            signal = "wait_call"
    elif weekly_macd < 0:
        direction = "bearish"
        if rsi_prev > 55 >= rsi_now:
            signal = "put"
        elif rsi_now <= 55:
            signal = "wait_put"

    filters_ok = adx_ok and volume_ok

    # ===== تحديد اللون =====
    if signal in ("call", "put") and filters_ok:
        color = "green" if signal == "call" else "red"
        label = "تأكيد CALL" if signal == "call" else "تأكيد PUT"
        status = signal
    elif signal in ("call", "put") and not filters_ok:
        # الإشارة تحققت لكن الفلاتر لا
        color = "yellow"
        label = "انتظار"
        status = "wait"
    elif signal in ("wait_call", "wait_put"):
        color = "yellow"
        label = "انتظار"
        status = "wait"
    else:
        color = "gray"
        label = "لم تجتز"
        status = "gray"

    # ===== نظام النقاط (100) =====
    score = 0
    if direction:                                   # 40 (MACD في الاتجاه)
        score += 40
    if signal in ("call", "put"):                   # 30 (RSI عبر)
        score += 30
    elif signal in ("wait_call", "wait_put"):       # 15 (الاتجاه موجود، RSI ينتظر)
        score += 15
    if adx_ok:                                      # 10
        score += 10
    if volume_ok:                                   # 10
        score += 10
    if signal in ("call", "put") and filters_ok:    # 10 (كل شيء مكتمل)
        score += 10
    score = min(score, 100)

    return {
        "signal": signal,
        "direction": direction,
        "color": color,
        "label": label,
        "status": status,
        "score": score,
        "weekly_macd": round(weekly_macd, 3),
        "daily_rsi": round(rsi_now, 1),
        "rsi_prev": round(rsi_prev, 1),
        "adx": round(adx_now, 1),
        "adx_ok": adx_ok,
        "volume_ok": volume_ok,
        "atr": round(atr_now, 2),
    }


def compute_swing_levels(entry, atr_val, direction):
    """مستويات السوينغ الأسبوعي"""
    if direction == "bearish":
        stop = entry + 0.5 * atr_val
        risk = stop - entry
        t1 = entry - risk
        t2 = entry - 2 * risk
    else:
        stop = entry - 0.5 * atr_val
        risk = entry - stop
        t1 = entry + risk
        t2 = entry + 2 * risk
    return {
        "entry":   round(entry, 2),
        "stop":    round(stop, 2),
        "target1": round(t1, 2),
        "target2": round(t2, 2),
    }
